"""Contained Streamable HTTP: target and probe on an isolated network.

    HARNESS (host)
        │  docker control only — never an HTTP socket to the target
        ▼
    PROBE SIDECAR  ◄──── isolated internal network ────►  UNTRUSTED TARGET
        │                                                  (no published port)
        ▼
    output directory (probe writes; harness reads after the probe exits)

The property that makes this worth building: the target publishes **no port to
the host at all**, and an ``--internal`` Docker network leaves it with no default
route. Measured, not assumed — a container there reaches neither the Internet nor
the container host, which closes the gateway-reachability hole that the
published-port model leaves open.

The harness never opens a socket to the target. Everything crosses through the
probe, which speaks no MCP semantics: it sends the bytes it is given and records
what came back. Interpretation stays on the host, where the assertion engine and
its tests live.

**Both containers are observed, not assumed.** The probe handles bytes from an
untrusted target, so treating it as trusted infrastructure would move the
boundary rather than enforce it. It is created, inspected while it exists,
started, inspected again while running, and only then trusted to have run under
the isolation the artifact claims. Requesting a flag and having a control are
different facts and are recorded as such.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from interop_lab.containment import docker
from interop_lab.containment.docker import ContainmentFailedError
from interop_lab.containment.imagesource import ImageSourceKind, RegistryImageSource
from interop_lab.containment.model import ContainerRuntimeFingerprint, ContainerSpec, NetworkMode
from interop_lab.containment.resolution import (
    ImageVerificationStatus,
    ResolvedImage,
    resolve_registry_source,
)
from interop_lab.naming import HARNESS_VERSION
from interop_lab.probe.plan import PLAN_SCHEMA_VERSION, PROBE_VERSION

#: Network alias the probe uses to reach the target. Fixed so a plan never
#: contains a host-specific address.
TARGET_ALIAS = "target"

#: Where the probe script and its output live inside the sidecar.
PROBE_SCRIPT = "/opt/probe/http_probe.py"
PROBE_OUT = "/out"
PROBE_PLAN = "/plan/plan.json"

#: The sidecar runs this project's own code, so it needs no third-party image.
#: Reusing the target's base keeps one image to pin rather than two.
DEFAULT_PROBE_IMAGE = "mirror.gcr.io/library/python"
DEFAULT_PROBE_DIGEST = "sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36"

#: The probe's own containment, at the same strength as an untrusted target's.
#: Numeric uid:gid rather than a name: resolving a name would mean trusting the
#: image's passwd file to describe an unprivileged user.
PROBE_USER = "65534:65534"
PROBE_MEMORY_MB = 256
PROBE_CPUS = 0.5
PROBE_PIDS_LIMIT = 64

#: Labels applied to every resource, so a leak is identifiable as ours and can
#: be attributed to the run that created it.
LABEL_OWNER = "interop-lab.owner"
LABEL_RUN = "interop-lab.run-id"
LABEL_RESOURCE = "interop-lab.resource"
LABEL_VERSION = "interop-lab.harness-version"


@dataclass
class ProbeIdentity:
    """Which probe produced this evidence. Part of the evidence, not metadata."""

    image_repository: str
    requested_digest: str
    #: Every `repository@digest` the engine reported, verbatim, beside the
    #: harness's reading of which one is provenance. The reading is
    #: driver-specific; the list is not.
    observed_repo_digests: list[str] = field(default_factory=list)
    observed_repo_digest: str | None = None
    observed_image_id: str | None = None
    probe_version: str = PROBE_VERSION
    script_sha256: str = ""
    plan_schema_version: str = PLAN_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "image_repository": self.image_repository,
            "requested_digest": self.requested_digest,
            "observed_repo_digests": list(self.observed_repo_digests),
            "observed_repo_digest": self.observed_repo_digest,
            "observed_image_id": self.observed_image_id,
            "probe_version": self.probe_version,
            "script_sha256": self.script_sha256,
            "plan_schema_version": self.plan_schema_version,
        }


@dataclass
class CleanupReport:
    """What survived the run. Never discarded, and never merely logged."""

    leaked: list[str] = field(default_factory=list)
    remediated: list[str] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.leaked

    def detail(self) -> str:
        parts: list[str] = []
        if self.leaked:
            parts.append("leaked: " + "; ".join(self.leaked))
        if self.remediated:
            parts.append("remediated: " + "; ".join(self.remediated))
        return " | ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "leaked": self.leaked,
            "remediated": self.remediated,
            "verified": self.verified,
        }


@dataclass
class SidecarResult:
    """What the probe reported, plus how both containments were verified."""

    outcome: str
    detail: str
    events: list[dict[str, Any]] = field(default_factory=list)
    probe_result: dict[str, Any] = field(default_factory=dict)
    target_weaknesses: list[str] = field(default_factory=list)
    probe_weaknesses: list[str] = field(default_factory=list)
    target_observed: ContainerRuntimeFingerprint | None = None
    probe_observed: ContainerRuntimeFingerprint | None = None
    probe_identity: ProbeIdentity | None = None
    #: Which image each container was resolved to, and how far that was
    #: verified. Carried on the result so the run record does not have to
    #: reconstruct it from the observation.
    resolved_target: ResolvedImage | None = None
    resolved_probe: ResolvedImage | None = None
    network_observed: dict[str, Any] = field(default_factory=dict)
    cleanup: CleanupReport = field(default_factory=CleanupReport)
    #: The exact plan the probe was given, persisted with the run.
    resolved_plan: dict[str, Any] = field(default_factory=dict)
    #: How the one writable path back to the harness was bounded.
    output_channel: dict[str, Any] = field(default_factory=dict)
    #: Set when the harness itself failed, as opposed to the target behaving badly.
    harness_error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome == "completed"


def _labels(run_id: str, resource: str) -> list[str]:
    return [
        "--label",
        f"{LABEL_OWNER}=true",
        "--label",
        f"{LABEL_RUN}={run_id}",
        "--label",
        f"{LABEL_RESOURCE}={resource}",
        "--label",
        f"{LABEL_VERSION}={HARNESS_VERSION}",
    ]


async def create_isolated_network(name: str, *, run_id: str) -> dict[str, Any]:
    """Create a fresh ``--internal`` network and verify it is what was asked for.

    Two things this deliberately does not do:

    * **Reuse a network because the name matches.** A name is not ownership. An
      existing network with our name was created by something else — a stale
      run, a concurrent job, or something that wants our target reachable — and
      attaching an untrusted target to it would put that target on a network
      with unknown properties and unknown neighbours.
    * **Delete the colliding network.** It may not be ours, and reclaiming
      another process's resource to make our own run proceed is how a test
      harness becomes an outage.

    Either way the run fails and the evidence is preserved.
    """
    code, out, _ = await docker.run_docker("docker", "network", "inspect", name)
    if code == 0 and out.strip():
        raise ContainmentFailedError(
            f"network {name} already exists. It was not created by this run, so its "
            "properties and its attached containers are unknown. Refusing to attach an "
            "untrusted target to it, and refusing to delete it in case it is not ours."
        )

    code, _, err = await docker.run_docker(
        "docker",
        "network",
        "create",
        "--internal",
        "--driver",
        "bridge",
        *_labels(run_id, "network"),
        name,
    )
    if code != 0:
        raise ContainmentFailedError(f"could not create isolated network {name}: {err.strip()}")

    observed = await inspect_network(name)
    problems = verify_network(observed, name=name, run_id=run_id)
    if problems:
        raise ContainmentFailedError(
            f"network {name} is not the network that was requested: {'; '.join(problems)}"
        )
    return observed


async def inspect_network(name: str) -> dict[str, Any]:
    code, out, err = await docker.run_docker("docker", "network", "inspect", name)
    if code != 0:
        raise ContainmentFailedError(f"could not inspect network {name}: {err.strip()}")
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ContainmentFailedError(f"network inspect returned non-JSON: {exc}") from exc
    if not isinstance(payload, list) or not payload:
        raise ContainmentFailedError(f"network inspect returned nothing for {name}")
    info: dict[str, Any] = payload[0]
    return {
        "name": str(info.get("Name") or ""),
        "id": str(info.get("Id") or "")[:12],
        "internal": bool(info.get("Internal")),
        "driver": str(info.get("Driver") or ""),
        "labels": {str(k): str(v) for k, v in (info.get("Labels") or {}).items()},
        "options": {str(k): str(v) for k, v in (info.get("Options") or {}).items()},
        "attached": sorted(
            str((container or {}).get("Name") or key)
            for key, container in (info.get("Containers") or {}).items()
        ),
    }


def verify_network(
    observed: dict[str, Any], *, name: str, run_id: str, expected_attached: list[str] | None = None
) -> list[str]:
    """Every way the observed network could differ from the one requested."""
    problems: list[str] = []
    if observed.get("name") != name:
        problems.append(f"name is {observed.get('name')!r}, expected {name!r}")
    if not observed.get("internal"):
        problems.append(
            "Internal is false, so the network has a default route. The whole point "
            "of this mode is that it does not."
        )
    if observed.get("driver") != "bridge":
        problems.append(f"driver is {observed.get('driver')!r}, expected 'bridge'")

    labels = observed.get("labels") or {}
    if labels.get(LABEL_OWNER) != "true":
        problems.append(f"{LABEL_OWNER} label is missing; this network is not provably ours")
    if labels.get(LABEL_RUN) != run_id:
        problems.append(f"{LABEL_RUN} is {labels.get(LABEL_RUN)!r}, expected {run_id!r}")

    options = observed.get("options") or {}
    if options:
        problems.append(f"unexpected driver options {sorted(options)}; none were requested")

    attached = list(observed.get("attached") or [])
    allowed = set(expected_attached or [])
    unexpected = [name for name in attached if name not in allowed]
    if unexpected:
        problems.append(
            f"unexpected containers attached: {unexpected}. An untrusted target must "
            "share its network with the probe and nothing else."
        )
    return problems


def build_target_argv(
    spec: ContainerSpec,
    *,
    image_reference: str,
    container_name: str,
    network: str,
    command: list[str],
    env: dict[str, str],
    repo_root: str,
    run_id: str = "",
) -> list[str]:
    """`docker run` argv for the untrusted target.

    Differs from the stdio form in exactly two ways, both deliberate: it joins
    the isolated network under a fixed alias, and it publishes **nothing**.
    """
    argv = docker.build_run_argv(
        spec,
        image_reference=image_reference,
        container_name=container_name,
        command=command,
        env=env,
        repo_root=repo_root,
        network_name=network,
        detach=True,
    )
    # The alias is what lets the plan name the target without knowing an address.
    insert_at = argv.index("--network") + 2
    argv = [*argv[:insert_at], "--network-alias", TARGET_ALIAS, *argv[insert_at:]]
    if run_id:
        insert_at = argv.index("run") + 1
        argv = [*argv[:insert_at], *_labels(run_id, "target-container"), *argv[insert_at:]]
    return argv


def build_probe_argv(
    *,
    container_name: str,
    network: str,
    image_reference: str,
    probe_dir: str,
    plan_dir: str,
    out_dir: str,
    memory_mb: int = PROBE_MEMORY_MB,
    cpus: float = PROBE_CPUS,
    pids_limit: int = PROBE_PIDS_LIMIT,
    user: str = PROBE_USER,
    run_id: str = "",
    create_only: bool = True,
) -> list[str]:
    """`docker create` (or `run`) argv for the probe.

    ``create_only`` is the default because a container that is created but not
    yet started can be inspected before a single byte reaches it. Validating
    containment after the fact would mean the evidence was already produced
    under whatever isolation actually applied.

    ``--rm`` is deliberately absent: the container has to outlive its own process
    long enough for its exit code to be read and its configuration inspected,
    and its removal has to be something the harness verifies rather than
    something the daemon may or may not have done.
    """
    argv = ["docker", "create" if create_only else "run", "--name", container_name]
    if run_id:
        argv += _labels(run_id, "probe-container")
    argv += [
        "--network",
        network,
        "--user",
        user,
        "--read-only",
        "--tmpfs",
        "/tmp",  # noqa: S108 - inside the container
        "--memory",
        f"{memory_mb}m",
        "--memory-swap",
        f"{memory_mb}m",
        "--cpus",
        str(cpus),
        "--pids-limit",
        str(pids_limit),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        # Read-only: the probe must not be able to rewrite its own script or plan.
        "--volume",
        f"{probe_dir}:/opt/probe:ro",
        "--volume",
        f"{plan_dir}:/plan:ro",
        # The single writable path, and the only channel back to the harness.
        "--volume",
        f"{out_dir}:{PROBE_OUT}",
        image_reference,
        "python",
        PROBE_SCRIPT,
        "--plan",
        PROBE_PLAN,
        "--out",
        PROBE_OUT,
    ]
    return argv


@dataclass
class OutputChannel:
    """How the probe's one writable path is bounded on the host."""

    #: "owned" — chowned to the probe uid and mode 0700.
    #: "parent_restricted" — mode 0777, reachable only through an owner-only parent.
    mode: str
    description: str
    #: True only when the *parent* fails to restrict traversal. That is a real
    #: hole; a wide inner mode behind a closed parent is not.
    exposed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "description": self.description, "exposed": self.exposed}


def prepare_output_channel(out_dir: Path, *, user: str = PROBE_USER) -> OutputChannel:
    """Bound the probe's output directory, and report how it was bounded.

    The probe runs unprivileged inside the container, so the host directory has
    to be writable by that uid. Two configurations achieve that, and they are
    equally narrow in effect:

    * **owned** — the harness can ``chown`` (it is root, or owns uid 65534), so
      the directory is the probe's and mode 0700.
    * **parent_restricted** — the harness cannot ``chown``, so the directory is
      mode 0777. This is *not* world-writable in practice: it lives inside a
      ``mkdtemp`` parent at mode 0700, and Unix requires execute permission on
      every parent to traverse into a directory. The set of accounts that can
      reach it is the same either way — the harness user and root.

    The old code treated the second case as a containment weakness, which
    invalidated every run on an ordinary non-root CI runner. That was
    overstating: it says nothing about the isolation of the *target*, which is
    what "usable evidence" means, and a check that cries wolf is a check someone
    eventually turns off.

    What *is* a real hole is a parent that does not restrict traversal, so that
    is what gets verified rather than assumed.
    """
    parent = out_dir.parent
    parent_mode = parent.stat().st_mode & 0o777
    parent_open = bool(parent_mode & (stat.S_IXGRP | stat.S_IXOTH))

    uid_text, _, gid_text = user.partition(":")
    try:
        uid, gid = int(uid_text), int(gid_text or uid_text)
    except ValueError:
        uid = gid = -1

    if uid >= 0:
        try:
            os.chown(out_dir, uid, gid)
        except (OSError, AttributeError):
            pass
        else:
            # Owner-only: the probe writes, the harness reads as the owner of
            # the temporary tree, and nothing else on the host can do either.
            out_dir.chmod(0o700)
            return OutputChannel(
                mode="owned",
                description=f"output directory owned by {user}, mode 0700",
                exposed=False,
            )

    # Could not take ownership. Rootless Docker, Docker Desktop's VM-backed bind
    # mounts, and any non-root CI runner all land here.
    out_dir.chmod(0o777)
    return OutputChannel(
        mode="parent_restricted",
        description=(
            f"output directory is mode 0777 inside a parent at mode {parent_mode:04o}; "
            "ownership could not be changed (expected off root, and under rootless "
            "Docker and Docker Desktop). Traversal is denied by the parent, so the "
            "reachable set is unchanged."
        ),
        exposed=parent_open,
    )


def read_output(
    out_dir: Path, *, max_files: int = 8, max_total_bytes: int = 8 * 1024 * 1024
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Read the probe's output, bounded and without following links out of it.

    The probe is contained but not trusted: it processes bytes from a hostile
    target. Everything read here is treated as untrusted input, which means
    bounded size, bounded file count, and no symlink traversal — a symlink in
    the output directory pointing at a host path would turn the evidence channel
    into an arbitrary-read primitive.
    """
    problems: list[str] = []
    if not out_dir.is_dir():
        return [], {}, [f"probe output directory {out_dir} does not exist"]

    entries = sorted(out_dir.iterdir())
    if len(entries) > max_files:
        problems.append(f"probe wrote {len(entries)} files, more than the {max_files} expected")
        entries = entries[:max_files]

    total = 0
    for entry in entries:
        if entry.is_symlink():
            problems.append(f"probe output {entry.name} is a symlink; refusing to follow it")
            continue
        if not entry.is_file():
            problems.append(f"probe output {entry.name} is not a regular file")
            continue
        total += entry.stat().st_size
    if total > max_total_bytes:
        problems.append(f"probe output is {total} bytes, over the {max_total_bytes} byte cap")

    events = read_events(out_dir / "wire.jsonl") if _is_regular(out_dir / "wire.jsonl") else []
    result = (
        read_probe_result(out_dir / "result.json") if _is_regular(out_dir / "result.json") else {}
    )
    return events, result, problems


def _is_regular(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


class SidecarRun:
    """One contained HTTP scenario: start target, run probe, verify, clean up."""

    def __init__(
        self,
        spec: ContainerSpec,
        *,
        run_id: str,
        launch_command: list[str],
        env: dict[str, str],
        repo_root: Path,
        resolved_target: ResolvedImage,
        probe_image: str = DEFAULT_PROBE_IMAGE,
        probe_digest: str = DEFAULT_PROBE_DIGEST,
    ) -> None:
        if spec.network is not NetworkMode.ISOLATED_SIDECAR:
            raise ContainmentFailedError(
                f"sidecar execution requires network=isolated_sidecar, got {spec.network.value}"
            )
        self.spec = spec
        self.run_id = run_id
        self.launch_command = launch_command
        self.env = env
        self.repo_root = repo_root
        #: Resolved by the caller before anything is created here. Passed in
        #: rather than resolved here so this class cannot be the place where a
        #: second, differently-configured resolver appears.
        self.resolved_target = resolved_target
        self.probe_image = probe_image
        self.probe_digest = probe_digest
        # The probe is always a registry image pinned by digest, so it resolves
        # without a build. Through the parser, not string concatenation: the
        # default has no registry port today, but an operator overriding it with
        # one on a private mirror must not silently get a different image.
        self.probe_source = RegistryImageSource(
            repository=probe_image, repository_digest=probe_digest
        )
        self.resolved_probe = resolve_registry_source(self.probe_source)
        self.probe_reference = self.resolved_probe.launch_reference

        # Random rather than a truncated run id. Two runs of the same scenario
        # against the same target share a prefix, and a name collision here does
        # not fail cleanly — it makes one run adopt another's resources.
        nonce = secrets.token_hex(6)
        short = run_id[:24]
        self.network_name = f"{docker.RESOURCE_PREFIX}-net-{short}-{nonce}"[:63]
        self.target_name = f"{docker.RESOURCE_PREFIX}-tgt-{short}-{nonce}"[:63]
        self.probe_name = f"{docker.RESOURCE_PREFIX}-probe-{short}-{nonce}"[:63]
        self._workdir: Path | None = None
        #: Set once this run has created its own network, so cleanup never tries to
        #: remove one it did not make.
        self.network_created = False

    async def execute(self, plan: dict[str, Any]) -> SidecarResult:
        """Run one plan. Every failure path cleans up, and reports what it found."""
        workdir = Path(tempfile.mkdtemp(prefix="interop-sidecar-"))
        self._workdir = workdir
        result: SidecarResult
        try:
            result = await self._execute(plan, workdir)
        except ContainmentFailedError as exc:
            result = SidecarResult(outcome="containment_failed", detail=str(exc))
        except Exception as exc:
            result = SidecarResult(
                outcome="harness_error",
                detail=f"{type(exc).__name__}: {exc}",
                harness_error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            cleanup = await self.cleanup()
            shutil.rmtree(workdir, ignore_errors=True)
            # `Path.exists` is a blocking stat on a local temporary directory
            # that was just removed. Verifying the removal is the whole point of
            # this line, and deferring it to a thread would only add a hop.
            if workdir.exists():  # noqa: ASYNC240
                cleanup.leaked.append(f"temporary directory {workdir} survived removal")
            else:
                cleanup.verified.append("temporary plan and output directory removed")

        # The cleanup result is attached to the run, not logged and forgotten. A
        # leaked container or network means the next run starts from an unknown
        # state, so the evidence this one produced cannot be trusted either.
        result.cleanup = cleanup
        return result

    async def _execute(self, plan: dict[str, Any], workdir: Path) -> SidecarResult:
        plan_dir = workdir / "plan"
        out_dir = workdir / "out"
        probe_dir = workdir / "probe"
        for path in (plan_dir, out_dir, probe_dir):
            path.mkdir(parents=True, exist_ok=True)

        source = self.repo_root / "fixtures" / "http_probe.py"
        if not source.is_file():
            raise ContainmentFailedError(f"probe script not found at {source}")
        shutil.copy2(source, probe_dir / "http_probe.py")
        script_sha = _sha256_of(source)

        resolved_plan = {**plan, "base_url": f"http://{TARGET_ALIAS}:{self.spec.container_port}/"}
        (plan_dir / "plan.json").write_text(json.dumps(resolved_plan, indent=2), encoding="utf-8")

        channel = prepare_output_channel(out_dir)

        base = await docker.ensure_available()
        network_observed = await create_isolated_network(self.network_name, run_id=self.run_id)
        self.network_created = True

        # ------------------------------------------------------------ target
        target_argv = build_target_argv(
            self.spec,
            image_reference=self.resolved_target.launch_reference,
            container_name=self.target_name,
            network=self.network_name,
            command=self.launch_command,
            env=self.env,
            repo_root=str(self.repo_root),
            run_id=self.run_id,
        )
        code, _, err = await docker.run_docker(*target_argv, timeout_s=self.spec.startup_timeout_s)
        if code != 0:
            raise ContainmentFailedError(f"target container failed to start: {err.strip()}")

        target_observed = await docker.observe(self.target_name, base=base)

        # Image identity is settled here, before the probe exists — so before
        # anything could have sent the scenario plan to the target. Checked
        # ahead of the other controls rather than folded in with them because
        # the others describe how a container was confined, and this one decides
        # whether the right bits are in it at all. A weakness in the rest is
        # reported after the fact; this one stops the run.
        identity_problems = docker.compare_image_identity(
            target_observed, self.resolved_target, label="target"
        )
        self.resolved_target = self.resolved_target.model_copy(
            update={
                "observed_container_image_id": target_observed.observed_image_id,
                "verification": (
                    ImageVerificationStatus.MISMATCH
                    if identity_problems
                    else self._verified_running_status()
                ),
            }
        )
        if identity_problems:
            return SidecarResult(
                outcome="target_image_identity_mismatch",
                detail="; ".join(identity_problems),
                target_weaknesses=identity_problems,
                target_observed=target_observed,
                network_observed=network_observed,
                resolved_target=self.resolved_target,
                resolved_probe=self.resolved_probe,
                resolved_plan=resolved_plan,
                output_channel=channel.as_dict(),
            )

        target_weaknesses = docker.compare(
            self.spec, target_observed, resolved=self.resolved_target
        )
        if target_observed.network_mode != self.network_name:
            target_weaknesses.append(
                f"target joined network {target_observed.network_mode!r}, not the isolated "
                f"network {self.network_name!r} created for this run"
            )
        # Publishing is what this mode exists to avoid, so it is checked here
        # rather than left to the generic comparison.
        if target_observed.published_host_port is not None:
            target_weaknesses.append(
                f"target published port {target_observed.published_host_port} to the host; "
                "an isolated sidecar target must publish nothing"
            )

        # ------------------------------------------------------------- probe
        probe_argv = build_probe_argv(
            container_name=self.probe_name,
            network=self.network_name,
            image_reference=self.probe_reference,
            probe_dir=str(probe_dir),
            plan_dir=str(plan_dir),
            out_dir=str(out_dir),
            run_id=self.run_id,
            create_only=True,
        )
        code, _, err = await docker.run_docker(*probe_argv, timeout_s=60.0)
        if code != 0:
            raise ContainmentFailedError(f"probe container could not be created: {err.strip()}")

        # Inspected before it runs. A weakness found here means no traffic has
        # happened yet, so the run is abandoned rather than explained away.
        probe_created = await docker.observe(self.probe_name, base=base)
        probe_weaknesses = self._probe_weaknesses(probe_created)
        if channel.exposed:
            # Only a parent that permits traversal is a hole. A wide inner mode
            # behind an owner-only parent reaches exactly the same accounts.
            probe_weaknesses.append(f"output channel is reachable by others: {channel.description}")

        probe_identity = ProbeIdentity(
            image_repository=self.probe_image,
            requested_digest=self.probe_digest,
            observed_repo_digests=list(probe_created.observed_repo_digests),
            observed_repo_digest=probe_created.observed_repo_digest,
            observed_image_id=probe_created.observed_image_id,
            script_sha256=script_sha,
        )

        if probe_weaknesses:
            return SidecarResult(
                outcome="probe_containment_weaker_than_requested",
                detail="; ".join(probe_weaknesses),
                target_weaknesses=target_weaknesses,
                probe_weaknesses=probe_weaknesses,
                target_observed=target_observed,
                probe_observed=probe_created,
                probe_identity=probe_identity,
                resolved_target=self.resolved_target,
                resolved_probe=self.resolved_probe,
                network_observed=network_observed,
                resolved_plan=resolved_plan,
                output_channel=channel.as_dict(),
            )

        timeout_s = _probe_timeout(resolved_plan)
        code, _, err = await docker.run_docker("docker", "start", self.probe_name, timeout_s=30.0)
        if code != 0:
            raise ContainmentFailedError(f"probe container could not be started: {err.strip()}")

        # Inspected again while running. `docker create` records a request;
        # only a running container can show what the kernel actually applied.
        probe_observed = await docker.observe(self.probe_name, base=base)
        probe_weaknesses = self._probe_weaknesses(probe_observed)
        probe_identity.observed_repo_digests = list(probe_observed.observed_repo_digests)
        probe_identity.observed_repo_digest = probe_observed.observed_repo_digest
        probe_identity.observed_image_id = probe_observed.observed_image_id
        self.resolved_probe = self.resolved_probe.model_copy(
            update={"observed_container_image_id": probe_observed.observed_image_id}
        )

        exit_code, wait_detail = await self._await_probe(timeout_s)

        # The network is re-inspected with the probe still present, so
        # "no unexpected attachments" is a statement about the network as it was
        # used rather than as it was created.
        with contextlib.suppress(ContainmentFailedError):
            network_observed = await inspect_network(self.network_name)
        attachment_problems = verify_network(
            network_observed,
            name=self.network_name,
            run_id=self.run_id,
            expected_attached=[self.target_name, self.probe_name],
        )

        events, probe_result, output_problems = read_output(out_dir)

        if attachment_problems:
            target_weaknesses.extend(attachment_problems)
        if output_problems:
            probe_weaknesses.extend(output_problems)
        if channel.exposed:
            probe_weaknesses.append(f"output channel is reachable by others: {channel.description}")

        if exit_code != 0 and not probe_result:
            return SidecarResult(
                outcome="probe_failed",
                detail=f"probe exited {exit_code}: {wait_detail}"[:400],
                events=events,
                target_weaknesses=target_weaknesses,
                probe_weaknesses=probe_weaknesses,
                target_observed=target_observed,
                probe_observed=probe_observed,
                probe_identity=probe_identity,
                resolved_target=self.resolved_target,
                resolved_probe=self.resolved_probe,
                network_observed=network_observed,
                resolved_plan=resolved_plan,
                output_channel=channel.as_dict(),
            )

        return SidecarResult(
            outcome=str(probe_result.get("outcome", "unknown")),
            detail=str(probe_result.get("detail", "")),
            events=events,
            probe_result=probe_result,
            target_weaknesses=target_weaknesses,
            probe_weaknesses=probe_weaknesses,
            target_observed=target_observed,
            probe_observed=probe_observed,
            probe_identity=probe_identity,
            resolved_target=self.resolved_target,
            resolved_probe=self.resolved_probe,
            network_observed=network_observed,
            resolved_plan=resolved_plan,
            output_channel=channel.as_dict(),
        )

    def _verified_running_status(self) -> ImageVerificationStatus:
        """What a matching observation proves, which differs by source kind.

        A registry image stays ``REGISTRY_DIGEST_PINNED``: the digest is what
        the runtime resolved, and there is no local resolution to promote. A
        local build moves to ``VERIFIED_RUNNING``, which is a stronger claim
        than ``VERIFIED_PRESENT`` — the image is not merely in this engine, it
        is the one inside the container that started.
        """
        if self.resolved_target.source_kind is ImageSourceKind.REGISTRY:
            return ImageVerificationStatus.REGISTRY_DIGEST_PINNED
        return ImageVerificationStatus.VERIFIED_RUNNING

    def _probe_weaknesses(self, observed: ContainerRuntimeFingerprint) -> list[str]:
        """The probe is held to the same standard as an untrusted target."""
        weaknesses = docker.compare_controls(
            observed,
            label="probe",
            resolved=self.resolved_probe,
            memory_mb=PROBE_MEMORY_MB,
            cpus=PROBE_CPUS,
            pids_limit=PROBE_PIDS_LIMIT,
            read_only_root=True,
            user=PROBE_USER,
            drop_all_capabilities=True,
            no_new_privileges=True,
        )
        if observed.network_mode != self.network_name:
            weaknesses.append(
                f"probe joined network {observed.network_mode!r}, not the isolated network "
                f"{self.network_name!r} created for this run"
            )
        if observed.published_host_port is not None:
            weaknesses.append(
                f"probe published port {observed.published_host_port} to the host; it must "
                "publish nothing"
            )
        return weaknesses

    async def _await_probe(self, timeout_s: float) -> tuple[int, str]:
        code, out, err = await docker.run_docker(
            "docker", "wait", self.probe_name, timeout_s=timeout_s
        )
        if code != 0:
            return 1, f"docker wait failed: {(err or out).strip()}"
        try:
            exit_code = int(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return 1, f"docker wait returned {out.strip()!r}"

        _, logs_out, logs_err = await docker.run_docker(
            "docker", "logs", "--tail", "40", self.probe_name, timeout_s=30.0
        )
        return exit_code, (logs_err or logs_out).strip()

    async def cleanup(self) -> CleanupReport:
        """Remove both containers and the network, reporting anything left.

        Bounded remediation: one forced removal, then one re-check. A cleanup
        that retried indefinitely would turn a leak into a hang, and the leak is
        the thing worth reporting.
        """
        report = CleanupReport()

        for name, label in ((self.probe_name, "probe"), (self.target_name, "target")):
            await docker.remove_container(name)
            code, out, _ = await docker.inspect_state(name)
            if code == 0 and out.strip():
                # One bounded second attempt before it is called a leak.
                await docker.run_docker("docker", "rm", "--force", "--volumes", name)
                code, out, _ = await docker.inspect_state(name)
                if code == 0 and out.strip():
                    report.leaked.append(f"{label} container {name} survived removal")
                    continue
                report.remediated.append(f"{label} container {name} needed a forced second removal")
            report.verified.append(f"{label} container {name} removed")

        if self.network_created:
            await docker.run_docker("docker", "network", "rm", self.network_name)
            code, out, _ = await docker.run_docker(
                "docker", "network", "inspect", self.network_name
            )
            if code == 0 and out.strip():
                report.leaked.append(f"network {self.network_name} survived removal")
            else:
                report.verified.append(f"network {self.network_name} removed")

        return report


def _probe_timeout(plan: dict[str, Any]) -> float:
    """A ceiling generous enough for the plan, tight enough to bound a hang."""
    steps = (
        len(plan.get("setup_steps") or [])
        + len(plan.get("scenario_steps") or [])
        + len((plan.get("cleanup") or {}).get("steps") or [])
    )
    per_step = float(plan.get("timeout_s", 10.0))
    ready = float(plan.get("ready_timeout_s", 30.0))
    return per_step * max(steps, 1) + ready + 60.0


def _sha256_of(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def read_probe_result(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
