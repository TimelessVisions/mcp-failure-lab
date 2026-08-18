"""Sidecar logic that holds without a container runtime.

The isolation itself is tested by attacking a real container
(`tests/integration/test_sidecar_containment.py`). These are the parts that
decide *what will be requested* and *how the probe's output is read back* —
they must be correct on a machine with no Docker, because a rule enforced only
where Docker happens to be installed is not enforced.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from conftest import registry_resolution
from interop_lab.containment import docker
from interop_lab.containment.docker import ContainmentFailedError
from interop_lab.containment.model import ContainerSpec, NetworkMode
from interop_lab.containment.sidecar import (
    DEFAULT_PROBE_DIGEST,
    DEFAULT_PROBE_IMAGE,
    TARGET_ALIAS,
    CleanupReport,
    SidecarResult,
    SidecarRun,
    build_probe_argv,
    build_target_argv,
    create_isolated_network,
    read_events,
    read_probe_result,
)
from interop_lab.models.core import Classification
from interop_lab.runner.contained_http import process_outcome_for
from interop_lab.runner.executor import classify

IMAGE = "mirror.gcr.io/library/python"
DIGEST = "sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36"


def make_spec(**overrides: object) -> ContainerSpec:
    base: dict[str, object] = {
        "image_source": {
            "kind": "registry",
            "repository": IMAGE,
            "repository_digest": DIGEST,
        },
        "network": NetworkMode.ISOLATED_SIDECAR,
        "container_port": 8000,
    }
    base.update(overrides)
    return ContainerSpec.model_validate(base)


def make_run(spec: ContainerSpec | None = None, **overrides: object) -> SidecarRun:
    base: dict[str, object] = {
        "run_id": "r",
        "launch_command": [],
        "env": {},
        "repo_root": Path(),
        # Resolved by the caller, never here: a second resolver inside this
        # class is a second place the resolution could differ from the one the
        # rest of the run was configured with.
        "resolved_target": registry_resolution(repository=IMAGE, display_tag=None),
    }
    base.update(overrides)
    return SidecarRun(spec if spec is not None else make_spec(), **base)  # type: ignore[arg-type]


# ------------------------------------------------------------ construction


def test_sidecar_refuses_a_spec_that_is_not_isolated() -> None:
    """Running a published-port spec through the sidecar would weaken it silently."""
    spec = make_spec(network=NetworkMode.PUBLISHED_LOOPBACK)
    with pytest.raises(ContainmentFailedError, match="requires network=isolated_sidecar"):
        make_run(spec)


def test_resource_names_are_derived_from_the_run_and_stay_legal() -> None:
    """Docker rejects names over 63 characters, and a rejected name is a failed run."""
    run = make_run(run_id="x" * 120)
    for name in (run.network_name, run.target_name, run.probe_name):
        assert len(name) <= 63, name
        assert name.startswith("interop-lab-")
    # Distinct, so cleanup cannot remove the wrong resource.
    assert len({run.network_name, run.target_name, run.probe_name}) == 3


def test_the_probe_image_is_pinned_by_digest() -> None:
    run = make_run()
    assert run.probe_reference == f"{DEFAULT_PROBE_IMAGE}@{DEFAULT_PROBE_DIGEST}"
    assert "@sha256:" in run.probe_reference


# ------------------------------------------------------------------- argv


def test_the_target_is_reachable_only_by_alias() -> None:
    argv = build_target_argv(
        make_spec(),
        image_reference=registry_resolution(repository=IMAGE).launch_reference,
        container_name="c",
        network="net",
        command=["${PYTHON}", "fixtures/http_server.py"],
        env={},
        repo_root="/repo",
    )
    assert "--network-alias" in argv
    assert argv[argv.index("--network-alias") + 1] == TARGET_ALIAS
    assert "--publish" not in argv
    # Detached: the harness controls it through docker, not through a pipe.
    assert "--detach" in argv


def test_the_target_keeps_every_ordinary_containment_control() -> None:
    """Sidecar mode changes the network, not the rest of the isolation."""
    argv = build_target_argv(
        make_spec(memory_mb=256, pids_limit=64),
        image_reference=registry_resolution(repository=IMAGE).launch_reference,
        container_name="c",
        network="net",
        command=["${PYTHON}", "s.py"],
        env={},
        repo_root="/repo",
    )
    joined = " ".join(argv)
    assert "--read-only" in joined
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert "--memory 256m" in joined and "--memory-swap 256m" in joined
    assert "--pids-limit 64" in joined
    assert "--user 65534:65534" in joined


def test_the_probe_writes_to_exactly_one_place() -> None:
    argv = build_probe_argv(
        container_name="p",
        network="net",
        image_reference=f"{IMAGE}@{DIGEST}",
        probe_dir="/w/probe",
        plan_dir="/w/plan",
        out_dir="/w/out",
    )
    mounts = [argv[i + 1] for i, part in enumerate(argv) if part == "--volume"]
    writable = [m for m in mounts if not m.endswith(":ro")]
    assert writable == ["/w/out:/out"], writable


def test_the_probe_runs_the_projects_own_script() -> None:
    argv = build_probe_argv(
        container_name="p",
        network="net",
        image_reference=f"{IMAGE}@{DIGEST}",
        probe_dir="/w/probe",
        plan_dir="/w/plan",
        out_dir="/w/out",
    )
    assert argv[-4:] == ["--plan", "/plan/plan.json", "--out", "/out"]
    assert "/opt/probe/http_probe.py" in argv


# ------------------------------------------------------- reading probe output


def test_events_are_read_back_line_by_line(tmp_path: Path) -> None:
    path = tmp_path / "wire.jsonl"
    path.write_text('{"seq": 1}\n{"seq": 2}\n', encoding="utf-8")
    assert read_events(path) == [{"seq": 1}, {"seq": 2}]


def test_a_truncated_event_line_does_not_discard_the_rest(tmp_path: Path) -> None:
    """A killed probe can leave a half-written final line.

    The events before it are still real observations, and throwing them away
    would turn a partial capture into no capture.
    """
    path = tmp_path / "wire.jsonl"
    path.write_text('{"seq": 1}\n{"seq": 2}\n{"seq": 3, "trunc', encoding="utf-8")
    assert read_events(path) == [{"seq": 1}, {"seq": 2}]


def test_missing_probe_output_reads_as_empty_not_as_an_error(tmp_path: Path) -> None:
    """A probe that never wrote is reported by the caller, not raised here."""
    assert read_events(tmp_path / "absent.jsonl") == []
    assert read_probe_result(tmp_path / "absent.json") == {}


def test_non_object_probe_result_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert read_probe_result(path) == {}


def test_unparseable_probe_result_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_probe_result(path) == {}


def test_a_valid_probe_result_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text(json.dumps({"outcome": "completed", "event_count": 3}), encoding="utf-8")
    assert read_probe_result(path)["outcome"] == "completed"


# --------------------------------------------------------------- the result


def test_only_a_completed_run_counts_as_success() -> None:
    assert SidecarResult(outcome="completed", detail="").succeeded
    for outcome in ("probe_failed", "target_unreachable", "probe_error", "unknown"):
        assert not SidecarResult(outcome=outcome, detail="").succeeded, outcome


# ------------------------------------------------------------------ cleanup
#
# Cleanup is the one part of the sidecar that is only exercised on the failure
# path, which is exactly the part that rots unnoticed. These simulate each
# reclaim failing, with no daemon involved, so they run everywhere and run fast.


class FakeDocker:
    """A docker layer that fails to remove whatever it is told to keep."""

    def __init__(self, *, survives: set[str]) -> None:
        self.survives = survives
        self.removals: list[str] = []

    async def remove_container(self, name: str) -> None:
        self.removals.append(name)

    async def inspect_state(self, name: str) -> tuple[int, str, str]:
        return (0, "cafebabe\n", "") if name in self.survives else (0, "", "")

    async def run_docker(self, *argv: str, timeout_s: float = 15.0) -> tuple[int, str, str]:
        if argv[:3] == ("docker", "network", "inspect"):
            name = argv[3]
            return (0, '[{"Name": "n"}]', "") if name in self.survives else (1, "", "not found")
        if argv[:3] == ("docker", "network", "rm"):
            return 0, "", ""
        if argv[:2] == ("docker", "rm"):
            return 0, "", ""
        return 0, "", ""


def _run_with(monkeypatch: pytest.MonkeyPatch, fake: FakeDocker) -> SidecarRun:
    monkeypatch.setattr(docker, "remove_container", fake.remove_container)
    monkeypatch.setattr(docker, "inspect_state", fake.inspect_state)
    monkeypatch.setattr(docker, "run_docker", fake.run_docker)
    run = make_run(
        run_id="cleanup-test",
        launch_command=["${PYTHON}", "fixtures/http_server.py"],
        repo_root=Path.cwd(),
    )
    run.network_created = True
    return run


def test_a_surviving_target_container_is_reported_as_a_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Resource names carry a random nonce, so the run has to exist before the
    # fake can be told which of its resources refuses to die.
    fake = FakeDocker(survives=set())
    run = _run_with(monkeypatch, fake)
    fake.survives = {run.target_name}

    report = asyncio.run(run.cleanup())
    assert not report.clean
    assert any(run.target_name in entry and "survived" in entry for entry in report.leaked)
    assert "leaked:" in report.detail()


def test_a_surviving_probe_container_is_reported_as_a_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeDocker(survives=set())
    run = _run_with(monkeypatch, fake)
    fake.survives = {run.probe_name}

    report = asyncio.run(run.cleanup())
    assert not report.clean
    assert any(run.probe_name in entry for entry in report.leaked), report.leaked


def test_a_surviving_network_is_reported_as_a_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDocker(survives=set())
    run = _run_with(monkeypatch, fake)
    fake.survives = {run.network_name}

    report = asyncio.run(run.cleanup())
    assert not report.clean
    assert any(run.network_name in entry for entry in report.leaked), report.leaked


def test_a_clean_cleanup_records_what_it_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run_with(monkeypatch, FakeDocker(survives=set()))
    report = asyncio.run(run.cleanup())

    assert report.clean
    assert report.leaked == []
    # Three resources, each verified by name rather than assumed gone.
    assert len(report.verified) == 3
    assert report.detail() == ""


def test_a_leaked_run_is_classified_as_a_cleanup_failure() -> None:
    """A leak must change the verdict, not just appear in a log.

    This is the join between the sidecar and the classifier: a run that left a
    container behind leaves the machine in a state the next run inherits, so its
    own evidence cannot be trusted either.
    """
    leaked = SidecarResult(
        outcome="completed",
        detail="",
        cleanup=CleanupReport(leaked=["container interop-lab-tgt-x survived removal"]),
    )
    outcome = process_outcome_for(leaked)
    assert outcome.cleanup_ok is False
    assert "survived removal" in (outcome.cleanup_detail or "")

    classification, _ = classify(
        [],
        timed_out=False,
        timeout_cause=None,
        process_alive_at_timeout=False,
        exit_code=0,
        killed_by_harness=False,
        cleanup_ok=outcome.cleanup_ok,
        expected_timeout=False,
    )
    assert classification is Classification.CLEANUP_FAILURE


def test_a_stale_network_collision_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-existing network with our name fails the run rather than being adopted."""

    async def _inspect_finds_one(*argv: str, timeout_s: float = 15.0) -> tuple[int, str, str]:
        if argv[:3] == ("docker", "network", "inspect"):
            return 0, '[{"Name": "interop-lab-net-x", "Internal": false}]', ""
        raise AssertionError(f"a colliding network must stop the run before {argv}")

    monkeypatch.setattr(docker, "run_docker", _inspect_finds_one)
    with pytest.raises(ContainmentFailedError) as caught:
        asyncio.run(create_isolated_network("interop-lab-net-x", run_id="r"))
    assert "already exists" in str(caught.value)
