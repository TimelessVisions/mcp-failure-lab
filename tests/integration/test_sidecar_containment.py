"""Sidecar containment, tested by attacking it.

The architecture:

    harness (host) ──docker control──> probe sidecar <──internal net──> target

The claim worth testing is not "the flags were passed" but "the target could not
do the things containment exists to prevent". Each test below attempts one of
them from inside the target container.

Every attempt is bounded and runs against a container this project started, on a
network this project created, with no port reachable from anywhere else.
"""

from __future__ import annotations

import asyncio
import json
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

from interop_lab.containment import docker
from interop_lab.containment.docker import ContainmentFailedError
from interop_lab.containment.resolution import ResolvedImage, resolve_registry_source
from interop_lab.containment.sidecar import (
    PROBE_CPUS,
    PROBE_MEMORY_MB,
    PROBE_PIDS_LIMIT,
    PROBE_USER,
    TARGET_ALIAS,
    SidecarRun,
    build_probe_argv,
    build_target_argv,
    create_isolated_network,
    prepare_output_channel,
    read_output,
    verify_network,
)
from interop_lab.models.core import TargetDefinition
from interop_lab.probe.plan import ProbeBodyMode, ProbePhase, ProbePlan, ProbeStep
from interop_lab.runner.images import ExecutionContext

pytestmark = [pytest.mark.integration, pytest.mark.slow]

CONTAINED_HTTP = Path("targets/http/fixture-2026-contained.yaml")
IMAGE = (
    "mirror.gcr.io/library/python@sha256:"
    "229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36"
)


def _docker_ready() -> bool:
    available, _, _ = asyncio.run(docker.probe())
    if not available:
        return False
    result = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, check=False)
    return result.returncode == 0


requires_docker = pytest.mark.skipif(
    not _docker_ready(), reason="docker or the pinned base image is unavailable"
)


def load_target() -> TargetDefinition:
    return TargetDefinition.model_validate(yaml.safe_load(CONTAINED_HTTP.read_text()))


def run_in_isolated_network(script: str, *, network: str) -> str:
    """Execute a probe script inside a throwaway container on the network."""
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            network,
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--memory",
            "256m",
            "--pids-limit",
            "64",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            IMAGE,
            "python",
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return (result.stdout + result.stderr).strip()


# ------------------------------------------------------------ argv, purely


def test_the_target_publishes_no_port() -> None:
    """The property this whole design exists for."""
    target = load_target()
    assert target.container is not None
    argv = build_target_argv(
        target.container,
        image_reference=_resolved(target).launch_reference,
        container_name="c",
        network="net",
        command=target.launch_command,
        env=target.env,
        repo_root="/repo",
    )
    assert "--publish" not in argv, "an isolated-sidecar target must publish nothing"
    assert "-p" not in argv
    assert "--network-alias" in argv
    assert argv[argv.index("--network-alias") + 1] == TARGET_ALIAS


def test_the_probe_is_contained_too() -> None:
    """Treating the probe as trusted would move the boundary, not enforce it.

    Every control an untrusted target gets, the probe gets. It handles bytes
    from that target, so a weaker probe would simply move the boundary one hop
    outward rather than enforce it.
    """
    argv = build_probe_argv(
        container_name="p",
        network="net",
        image_reference=IMAGE,
        probe_dir="/w/probe",
        plan_dir="/w/plan",
        out_dir="/w/out",
    )
    joined = " ".join(argv)
    assert f"--user {PROBE_USER}" in joined, "the probe must not run as root"
    assert "--read-only" in joined
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert "--memory 256m" in joined
    assert "--memory-swap 256m" in joined
    assert "--cpus 0.5" in joined
    assert "--pids-limit 64" in joined
    assert "--publish" not in joined
    assert "--env" not in joined, "the probe inherits no environment"
    # Created rather than run, so containment can be inspected before any byte
    # of untrusted output reaches it.
    assert argv[1] == "create"


def test_the_probe_cannot_rewrite_its_own_script_or_plan() -> None:
    argv = build_probe_argv(
        container_name="p",
        network="net",
        image_reference=IMAGE,
        probe_dir="/w/probe",
        plan_dir="/w/plan",
        out_dir="/w/out",
    )
    mounts = [argv[i + 1] for i, part in enumerate(argv) if part == "--volume"]
    assert "/w/probe:/opt/probe:ro" in mounts
    assert "/w/plan:/plan:ro" in mounts
    # Exactly one writable mount, and it is the output channel.
    writable = [m for m in mounts if not m.endswith(":ro")]
    assert writable == ["/w/out:/out"]


def test_no_docker_socket_is_mounted_into_either_container() -> None:
    """A Docker socket inside a container is containment with a hole in it."""
    target = load_target()
    assert target.container is not None
    target_argv = build_target_argv(
        target.container,
        image_reference=_resolved(target).launch_reference,
        container_name="c",
        network="net",
        command=target.launch_command,
        env=target.env,
        repo_root="/repo",
    )
    probe_argv = build_probe_argv(
        container_name="p",
        network="net",
        image_reference=IMAGE,
        probe_dir="/w/probe",
        plan_dir="/w/plan",
        out_dir="/w/out",
    )
    for argv in (target_argv, probe_argv):
        joined = " ".join(argv)
        assert "docker.sock" not in joined
        assert "/var/run/docker" not in joined


def test_the_target_inherits_no_host_environment() -> None:
    """Only explicitly declared values cross the boundary."""
    target = load_target()
    assert target.container is not None
    argv = build_target_argv(
        target.container,
        image_reference=_resolved(target).launch_reference,
        container_name="c",
        network="net",
        command=target.launch_command,
        env={"INTEROP_FIXTURE_PROFILE": "2026-07-28"},
        repo_root="/repo",
    )
    passed = [argv[i + 1] for i, part in enumerate(argv) if part == "--env"]
    assert passed == ["INTEROP_FIXTURE_PROFILE=2026-07-28"]


# ------------------------------------------------- the isolation, attacked


@pytest.fixture
def isolated_network() -> Any:
    # A fresh name per test: `create_isolated_network` deliberately refuses a
    # name that already exists, and a shared fixture network would make these
    # tests order-dependent for no benefit.
    name = f"interop-lab-test-isolated-{uuid.uuid4().hex[:10]}"
    asyncio.run(create_isolated_network(name, run_id="pytest"))
    yield name
    asyncio.run(docker.run_docker("docker", "network", "rm", name))


@requires_docker
def test_a_contained_target_has_no_route_off_the_network(isolated_network: str) -> None:
    """The measured property that makes this stronger than a published port.

    An --internal network leaves the container with no default route at all, so
    it reaches neither the Internet nor the container host. The published-port
    model denies the Internet but leaves the host gateway reachable.
    """
    script = (
        "import socket\n"
        "with open('/proc/net/route') as fh: rows = fh.read().splitlines()[1:]\n"
        "gw = [r for r in rows if r.split()[1] == '00000000']\n"
        "print('default_routes', len(gw))\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=4)\n"
        "    print('EGRESS_REACHABLE')\n"
        "except OSError as exc:\n"
        "    print('egress_denied', type(exc).__name__)\n"
    )
    output = run_in_isolated_network(script, network=isolated_network)
    assert "default_routes 0" in output, output
    assert "EGRESS_REACHABLE" not in output, output
    assert "egress_denied" in output, output


@requires_docker
def test_a_contained_target_cannot_reach_ordinary_host_services(
    isolated_network: str,
) -> None:
    """With no default route there is no gateway address to try."""
    script = (
        "import socket\n"
        "reached = []\n"
        "for host in ('172.17.0.1', '10.0.0.1', 'host.docker.internal'):\n"
        "    for port in (22, 2375, 8000):\n"
        "        try:\n"
        "            socket.create_connection((host, port), timeout=2)\n"
        "            reached.append(f'{host}:{port}')\n"
        "        except OSError:\n"
        "            pass\n"
        "print('reached', reached)\n"
    )
    output = run_in_isolated_network(script, network=isolated_network)
    assert "reached []" in output, output


@requires_docker
def test_a_contained_target_cannot_write_outside_its_tmpfs(
    isolated_network: str,
) -> None:
    script = (
        "import os\n"
        "results = {}\n"
        "for path in ('/escape', '/etc/escape', '/usr/escape'):\n"
        "    try:\n"
        "        open(path, 'w').write('x'); os.unlink(path); results[path] = 'WROTE'\n"
        "    except OSError as exc:\n"
        "        results[path] = type(exc).__name__\n"
        "try:\n"
        "    open('/tmp/ok', 'w').write('x'); results['/tmp'] = 'writable'\n"
        "except OSError as exc:\n"
        "    results['/tmp'] = type(exc).__name__\n"
        "print(results)\n"
    )
    output = run_in_isolated_network(script, network=isolated_network)
    assert "WROTE" not in output, output
    assert "'/tmp': 'writable'" in output, output


@requires_docker
def test_a_contained_target_cannot_read_a_host_only_file(
    isolated_network: str, tmp_path: Path
) -> None:
    """A synthetic host secret must not be visible inside the container."""
    secret = tmp_path / "host-only-secret.txt"
    secret.write_text("INTEROP-HOST-ONLY-MARKER", encoding="utf-8")

    script = (
        "import pathlib\n"
        f"p = pathlib.Path({str(secret)!r})\n"
        "print('exists', p.exists())\n"
        "text = p.read_text() if p.exists() else ''\n"
        "print('marker_found', 'INTEROP-HOST-ONLY-MARKER' in text)\n"
    )
    output = run_in_isolated_network(script, network=isolated_network)
    assert "exists False" in output, output
    assert "marker_found False" in output, output


@requires_docker
def test_a_contained_target_does_not_inherit_a_synthetic_credential(
    isolated_network: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A credential in the harness's environment must not cross the boundary."""
    monkeypatch.setenv("INTEROP_SYNTHETIC_TOKEN", "super-secret-value")

    script = (
        "import os\n"
        "hits = {k: v for k, v in os.environ.items() if 'INTEROP_SYNTHETIC' in k}\n"
        "print('inherited', hits)\n"
        "print('value_present', 'super-secret-value' in repr(dict(os.environ)))\n"
    )
    output = run_in_isolated_network(script, network=isolated_network)
    assert "inherited {}" in output, output
    assert "value_present False" in output, output


@requires_docker
def test_a_contained_target_cannot_exceed_its_memory_limit(
    isolated_network: str,
) -> None:
    script = (
        "chunks = []\n"
        "mb = 0\n"
        "try:\n"
        "    while mb < 768:\n"
        "        chunks.append(bytearray(1024 * 1024)); mb += 1\n"
        "except MemoryError:\n"
        "    print('denied MemoryError at', mb)\n"
        "else:\n"
        "    print('ALLOWED allocated', mb)\n"
    )
    output = run_in_isolated_network(script, network=isolated_network)
    # An OOM kill produces no output at all, which is also containment.
    assert "ALLOWED allocated 768" not in output, output


@requires_docker
def test_a_contained_target_cannot_exceed_its_process_limit(
    isolated_network: str,
) -> None:
    script = (
        "import os\n"
        "kids = []\n"
        "try:\n"
        "    for _ in range(512):\n"
        "        pid = os.fork()\n"
        "        if pid == 0: os._exit(0)\n"
        "        kids.append(pid)\n"
        "except OSError as exc:\n"
        "    print('denied after', len(kids), type(exc).__name__)\n"
        "else:\n"
        "    print('ALLOWED forked', len(kids))\n"
    )
    output = run_in_isolated_network(script, network=isolated_network)
    assert "ALLOWED forked 512" not in output, output


# --------------------------------------------------- the full path, and cleanup


def minimal_plan(**overrides: Any) -> dict[str, Any]:
    """A plan the probe will accept, built through the typed contract.

    Built rather than hand-written so a change to the schema breaks these tests
    instead of leaving them asserting against a shape nothing produces.
    """
    plan = ProbePlan(
        run_id="pytest",
        protocol_profile="mcp-2026-07-28",
        timeout_s=10.0,
        ready_timeout_s=60.0,
        scenario_steps=[
            ProbeStep(
                step_id="list",
                phase=ProbePhase.SCENARIO,
                path="/mcp",
                headers={
                    "Content-Type": "application/json",
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": "tools/list",
                },
                body_mode=ProbeBodyMode.JSON,
                json_body={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientCapabilities": {},
                        }
                    },
                },
            )
        ],
    )
    return {**plan.to_json_dict(), **overrides}


def _resolved(target: TargetDefinition) -> ResolvedImage:
    """These fixtures all run a registry-pinned image, so nothing is built."""
    spec = target.container
    assert spec is not None
    source = spec.registry_source
    assert source is not None, "the HTTP fixtures run a pulled image, not a built one"
    return resolve_registry_source(source)


def sidecar_run(run_id: str) -> SidecarRun:
    target = load_target()
    assert target.container is not None
    return SidecarRun(
        target.container,
        run_id=run_id,
        launch_command=target.launch_command,
        env=target.env,
        repo_root=Path.cwd(),
        resolved_target=_resolved(target),
    )


@requires_docker
def test_a_scenario_runs_end_to_end_through_the_sidecar() -> None:
    """Target started, probe connected, response captured — with no host socket."""
    run = sidecar_run("pytest-sidecar-e2e")
    result = asyncio.run(run.execute(minimal_plan()))

    assert result.succeeded, f"{result.outcome}: {result.detail}"
    assert result.target_weaknesses == [], result.target_weaknesses
    assert result.probe_weaknesses == [], result.probe_weaknesses

    inbound = [e for e in result.events if e.get("direction") == "inbound"]
    assert inbound, "the probe captured no response"
    # The fixture answered. Which status it chose is the scenario's business;
    # what this test proves is that the path exists and was recorded.
    assert inbound[0].get("status") is not None
    assert inbound[0].get("first_byte_ms") is not None, "timing was not captured"


@requires_docker
def test_the_probe_containment_is_observed_not_assumed() -> None:
    """The probe's isolation is read back from the runtime, like the target's.

    Asserting on the argv only proves what was requested. Docker accepts
    `--pids-limit` on a host without the pids cgroup controller and reports
    success, so a record built from the flags would state a limit the kernel
    never applied — to the container handling untrusted output.
    """
    run = sidecar_run("pytest-probe-observed")
    result = asyncio.run(run.execute(minimal_plan()))

    observed = result.probe_observed
    assert observed is not None, "no probe containment was observed"
    assert observed.user == PROBE_USER, f"probe ran as {observed.user!r}"
    assert observed.user not in ("", "root", "0:0")
    assert observed.memory_limit_bytes == PROBE_MEMORY_MB * 1024 * 1024
    assert observed.pids_limit == PROBE_PIDS_LIMIT
    assert observed.nano_cpus == int(PROBE_CPUS * 1_000_000_000)
    assert observed.read_only_root is True
    assert "ALL" in observed.cap_drop
    assert any("no-new-privileges" in option for option in observed.security_opt)
    assert observed.published_host_port is None
    assert observed.network_mode == run.network_name
    assert result.probe_weaknesses == [], result.probe_weaknesses


@requires_docker
def test_the_probe_identity_is_part_of_the_evidence() -> None:
    """Which probe produced a result is not metadata; it is the result's provenance."""
    run = sidecar_run("pytest-probe-identity")
    result = asyncio.run(run.execute(minimal_plan()))

    identity = result.probe_identity
    assert identity is not None
    assert identity.image_repository
    assert identity.requested_digest.startswith("sha256:")
    assert identity.observed_repo_digest or identity.observed_image_id
    assert identity.probe_version
    assert len(identity.script_sha256) == 64
    assert identity.plan_schema_version


@requires_docker
def test_the_network_is_unique_internal_and_ours() -> None:
    """Created fresh, verified after creation, and owned by this run."""
    run = sidecar_run("pytest-network")
    result = asyncio.run(run.execute(minimal_plan()))

    observed = result.network_observed
    assert observed["internal"] is True, "the network has a default route"
    assert observed["driver"] == "bridge"
    assert observed["labels"]["interop-lab.owner"] == "true"
    assert observed["labels"]["interop-lab.run-id"] == "pytest-network"
    assert observed["options"] == {}, "no driver options were requested"
    # Every attached container belongs to this run.
    assert set(observed["attached"]) <= {run.target_name, run.probe_name}


@requires_docker
def test_a_colliding_network_is_refused_not_reused_or_deleted() -> None:
    """A name is not ownership.

    An existing network with our name was created by something else. Attaching
    an untrusted target to it would put that target on a network with unknown
    properties and unknown neighbours; deleting it would reclaim a resource that
    may belong to something running. Both are refused.
    """
    name = f"interop-lab-test-collision-{uuid.uuid4().hex[:10]}"
    # A plain bridge network — deliberately *not* internal, so reusing it would
    # be a real weakening rather than a cosmetic one.
    asyncio.run(docker.run_docker("docker", "network", "create", name))
    try:
        with pytest.raises(ContainmentFailedError) as caught:
            asyncio.run(create_isolated_network(name, run_id="pytest-collision"))
        assert "already exists" in str(caught.value)

        # And it is still there: the harness did not reclaim someone else's
        # resource to make its own run proceed.
        code, out, _ = asyncio.run(docker.run_docker("docker", "network", "inspect", name))
        assert code == 0 and out.strip(), "a colliding network was deleted"
    finally:
        asyncio.run(docker.run_docker("docker", "network", "rm", name))


@requires_docker
def test_nothing_survives_a_sidecar_run() -> None:
    """Containers and the network are reclaimed, and a leak is reported."""
    run = sidecar_run("pytest-sidecar-cleanup")
    result = asyncio.run(run.execute(minimal_plan(scenario_steps=[])))

    assert result.cleanup.clean, result.cleanup.leaked
    assert result.cleanup.verified, "cleanup reported nothing it verified"

    for name in (run.target_name, run.probe_name):
        code, out, _ = asyncio.run(docker.inspect_state(name))
        assert not (code == 0 and out.strip()), f"{name} survived the run"

    code, out, _ = asyncio.run(docker.run_docker("docker", "network", "inspect", run.network_name))
    assert not (code == 0 and out.strip()), "the isolated network survived the run"


@requires_docker
def test_the_probe_plan_never_contains_a_host_address() -> None:
    """The probe addresses the target by network alias, not by an address."""
    run = sidecar_run("pytest-sidecar-plan")
    result = asyncio.run(run.execute(minimal_plan(scenario_steps=[])))
    serialised = json.dumps(result.events) + json.dumps(result.resolved_plan)
    assert "127.0.0.1" not in serialised
    assert "172.1" not in serialised
    assert TARGET_ALIAS in json.dumps(result.resolved_plan)


@requires_docker
def test_the_probe_refuses_a_plan_field_it_does_not_understand() -> None:
    """Silently ignoring an unsupported member would fake a run nobody asked for."""
    run = sidecar_run("pytest-unknown-field")
    plan = minimal_plan()
    plan["a_member_from_a_newer_harness"] = True
    result = asyncio.run(run.execute(plan))

    assert result.outcome == "plan_rejected", f"{result.outcome}: {result.detail}"
    assert "a_member_from_a_newer_harness" in result.detail
    assert result.cleanup.clean, result.cleanup.leaked


@requires_docker
def test_the_probe_refuses_a_plan_schema_version_it_does_not_implement() -> None:
    run = sidecar_run("pytest-schema-version")
    result = asyncio.run(run.execute(minimal_plan(plan_schema_version="999")))

    assert result.outcome == "plan_rejected", f"{result.outcome}: {result.detail}"
    assert "999" in result.detail


def test_the_output_channel_is_bounded_either_way(tmp_path: Path) -> None:
    """Two configurations, equally narrow in effect, and neither is a weakness.

    Where ownership can be taken, the directory is the probe's and mode 0700.
    Where it cannot — off root, under rootless Docker, on an ordinary CI runner
    — it is mode 0777 inside an owner-only parent. Unix requires execute
    permission on every parent to traverse into a directory, so the set of
    accounts that can reach it is the same: the harness user and root.
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    tmp_path.chmod(0o700)
    channel = prepare_output_channel(out_dir)

    assert channel.mode in ("owned", "parent_restricted"), channel.mode
    assert not channel.exposed, channel.description
    if channel.mode == "owned":
        assert out_dir.stat().st_mode & 0o777 == 0o700
        assert out_dir.stat().st_uid == 65534
    else:
        # Wide inner mode, closed parent. That is the whole point.
        assert tmp_path.stat().st_mode & (stat.S_IXGRP | stat.S_IXOTH) == 0


def test_an_output_channel_behind_an_open_parent_is_a_real_weakness(tmp_path: Path) -> None:
    """The case that genuinely does expose the evidence channel.

    If the parent permits traversal, the mode on the output directory is the
    only thing standing between the harness's evidence and every account on the
    host. That is worth failing a run over; a closed parent is not.
    """
    parent = tmp_path / "open"
    parent.mkdir()
    parent.chmod(0o755)
    out_dir = parent / "out"
    out_dir.mkdir()

    channel = prepare_output_channel(out_dir, user="not-a-uid")
    assert channel.mode == "parent_restricted"
    assert channel.exposed, channel.description


def test_reading_probe_output_refuses_a_symlink(tmp_path: Path) -> None:
    """A symlink in the output directory would be an arbitrary host read."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    secret = tmp_path / "host-only.txt"
    secret.write_text("INTEROP-HOST-ONLY-MARKER", encoding="utf-8")
    (out_dir / "wire.jsonl").symlink_to(secret)

    events, result, problems = read_output(out_dir)
    assert events == []
    assert result == {}
    assert any("symlink" in problem for problem in problems), problems


def test_reading_probe_output_bounds_the_file_count(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    for index in range(12):
        (out_dir / f"junk-{index}.txt").write_text("x", encoding="utf-8")

    _, _, problems = read_output(out_dir, max_files=8)
    assert any("more than the 8 expected" in problem for problem in problems), problems


def test_verify_network_rejects_a_non_internal_network() -> None:
    problems = verify_network(
        {
            "name": "n",
            "internal": False,
            "driver": "bridge",
            "labels": {"interop-lab.owner": "true", "interop-lab.run-id": "r"},
            "options": {},
            "attached": [],
        },
        name="n",
        run_id="r",
    )
    assert any("Internal is false" in problem for problem in problems), problems


def test_verify_network_rejects_an_unexpected_attachment() -> None:
    problems = verify_network(
        {
            "name": "n",
            "internal": True,
            "driver": "bridge",
            "labels": {"interop-lab.owner": "true", "interop-lab.run-id": "r"},
            "options": {},
            "attached": ["someone-elses-container"],
        },
        name="n",
        run_id="r",
        expected_attached=["ours"],
    )
    assert any("unexpected containers" in problem for problem in problems), problems


@requires_docker
def test_the_trace_records_how_the_evidence_was_obtained() -> None:
    """Every lifecycle stage reaches the canonical trace.

    A contained run has a setup story that the traffic alone does not tell: a
    network was created and verified, two containers were started and inspected,
    everything was reclaimed. Without it a reader has the target's answers and no
    way to see under what conditions they were obtained — and a stage that
    silently stopped being emitted would leave no trace of its own absence.
    """
    import asyncio as _asyncio

    from interop_lab.profiles.registry import get_profile
    from interop_lab.runner.contained_http import LIFECYCLE_STAGES, ContainedHttpPlanStrategy
    from interop_lab.scenarios.library import get_scenario

    target = load_target()
    scenario = get_scenario("baseline.http_tools_list")
    outcome = _asyncio.run(
        ContainedHttpPlanStrategy().execute(
            scenario,
            target,
            run_id="pytest-lifecycle",
            profile=get_profile(target.protocol_profile),
            context=ExecutionContext.create(),
        )
    )

    notes = " | ".join(event.note or "" for event in outcome.trace.events)
    missing = [stage for stage in LIFECYCLE_STAGES if stage not in notes]
    assert missing == [], f"lifecycle stages missing from the trace: {missing}\n{notes}"

    # And the run itself was clean, so those stages describe a real success
    # rather than the failure path.
    assert outcome.containment is not None
    assert outcome.containment.is_usable_evidence
    assert outcome.process.cleanup_ok
