"""Docker execution, and verification that the container got what was asked for.

Two things here are load-bearing:

* ``ensure_available`` raises. It never returns a "containment unavailable, so
  running on the host instead" path, because a run that silently changed its own
  isolation is worse than a run that failed.
* ``observe`` reads the running container back through ``docker inspect`` and
  compares it against the request. Docker accepts several flags it cannot honour
  on every platform — ``--pids-limit`` needs the pids cgroup controller,
  ``--cpus`` needs cfs quota support — and reports success either way.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Any, NamedTuple

from pydantic import BaseModel, ConfigDict

from interop_lab.artifacts.portability import PYTHON_TOKEN
from interop_lab.containment.imagesource import ImageSourceKind
from interop_lab.containment.model import (
    CONTAINMENT_LIMITS,
    MOUNT_ROOT,
    ContainerRuntimeFingerprint,
    ContainerSpec,
    NetworkMode,
)
from interop_lab.containment.resolution import ResolvedImage

#: Prefix for every network and container the harness creates, so a leaked
#: resource is identifiable as ours and can be reclaimed.
RESOURCE_PREFIX = "interop-lab"

#: Name of the shared no-egress network. Created on demand, reused across runs.
NO_EGRESS_NETWORK = f"{RESOURCE_PREFIX}-no-egress"

_INSPECT_TIMEOUT_S = 15.0

#: The interpreter inside the image, as opposed to the one running the harness.
CONTAINER_PYTHON = "python"


class ContainmentUnavailableError(Exception):
    """The container runtime cannot be used, and there is no fallback."""


class ContainmentFailedError(Exception):
    """The runtime is present but the container could not be started."""


async def run_docker(*argv: str, timeout_s: float = _INSPECT_TIMEOUT_S) -> tuple[int, str, str]:
    """Run a docker command, returning (exit code, stdout, stderr)."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ContainmentUnavailableError(
            f"`{' '.join(argv)}` did not return within {timeout_s}s"
        ) from None
    return process.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def probe() -> tuple[bool, str, ContainerRuntimeFingerprint | None]:
    """Report whether Docker is usable here, without raising.

    Returns ``(available, reason, fingerprint)``. The reason is written for
    someone reading a failed CI log, so it says which check failed.
    """
    if shutil.which("docker") is None:
        return False, "docker is not on PATH", None

    code, out, err = await run_docker("docker", "version", "--format", "{{json .}}")
    if code != 0:
        detail = (err or out).strip().splitlines()
        reason = detail[-1] if detail else f"`docker version` exited {code}"
        return False, f"the docker daemon is not reachable: {reason}", None

    try:
        payload: dict[str, Any] = json.loads(out)
    except json.JSONDecodeError:
        return False, "`docker version` returned output that is not JSON", None

    client = payload.get("Client") or {}
    server = payload.get("Server") or {}
    if not server:
        return False, "the docker client is installed but no daemon is running", None

    # The storage driver is read here rather than left for a later caller
    # because it is what decides whether a local image ID from this engine is
    # comparable with one from another. A containment record that omits it
    # cannot be re-read later without guessing.
    code, driver_out, _ = await run_docker("docker", "info", "--format", "{{.Driver}}")
    storage_driver = driver_out.strip() if code == 0 else None

    return (
        True,
        "available",
        ContainerRuntimeFingerprint(
            runtime="docker",
            runtime_version=str(client.get("Version") or ""),
            server_version=str(server.get("Version") or ""),
            storage_driver=storage_driver or None,
        ),
    )


async def ensure_available() -> ContainerRuntimeFingerprint:
    """Return the runtime fingerprint, or raise. There is no third option."""
    available, reason, fingerprint = await probe()
    if not available or fingerprint is None:
        raise ContainmentUnavailableError(
            f"containment was requested but {reason}. The harness does not fall back "
            "to host execution: a result produced with different isolation than the "
            "one recorded is not the result that was asked for."
        )
    return fingerprint


async def ensure_no_egress_network() -> str:
    """Create (idempotently) the bridge network used for HTTP targets.

    IP masquerading is disabled, which removes the NAT rule that would let the
    container reach the Internet, while leaving inbound port publishing intact.
    ``--internal`` would be stronger but also breaks published ports, so the
    harness could not reach an HTTP target at all.
    """
    code, out, _ = await run_docker("docker", "network", "inspect", NO_EGRESS_NETWORK)
    if code == 0 and out.strip():
        return NO_EGRESS_NETWORK

    code, _, err = await run_docker(
        "docker",
        "network",
        "create",
        "-o",
        "com.docker.network.bridge.enable_ip_masquerade=false",
        NO_EGRESS_NETWORK,
    )
    if code != 0:
        # A concurrent run may have created it between the inspect and here.
        code2, out2, _ = await run_docker("docker", "network", "inspect", NO_EGRESS_NETWORK)
        if code2 != 0 or not out2.strip():
            raise ContainmentFailedError(f"could not create {NO_EGRESS_NETWORK}: {err.strip()}")
    return NO_EGRESS_NETWORK


def build_run_argv(
    spec: ContainerSpec,
    *,
    image_reference: str,
    container_name: str,
    command: list[str],
    env: dict[str, str],
    host_port: int | None = None,
    network_name: str | None = None,
    repo_root: str = ".",
    detach: bool = False,
) -> list[str]:
    """Assemble the `docker run` argv for a spec.

    Kept pure and separately tested: this is the code that decides what
    isolation is actually requested, and asserting on a list of strings is a
    far more direct check than inferring it from a running container.

    ``image_reference`` is passed in rather than read off the spec because for a
    locally built target the spec deliberately does not know it: the identity
    comes from a build-resolution manifest, and letting this function reach for
    a field on the spec is exactly how a tag would end up on the command line.
    """
    argv = ["docker", "run", "--rm", "--name", container_name]
    argv += ["--detach"] if detach else ["--interactive"]

    if spec.network is NetworkMode.NONE:
        argv += ["--network", "none"]
    else:
        argv += ["--network", network_name or NO_EGRESS_NETWORK]
        if host_port is not None and spec.container_port is not None:
            # Bound to loopback explicitly. Publishing to 0.0.0.0 would expose a
            # deliberately hostile target to the local network.
            argv += ["--publish", f"127.0.0.1:{host_port}:{spec.container_port}"]

    argv += ["--memory", f"{spec.memory_mb}m", "--memory-swap", f"{spec.memory_mb}m"]
    argv += ["--cpus", str(spec.cpus)]
    argv += ["--pids-limit", str(spec.pids_limit)]
    if spec.read_only_root:
        argv += ["--read-only"]
    for path in spec.tmpfs_paths:
        argv += ["--tmpfs", path]
    if spec.user:
        argv += ["--user", spec.user]
    if spec.drop_all_capabilities:
        argv += ["--cap-drop", "ALL"]
    if spec.no_new_privileges:
        argv += ["--security-opt", "no-new-privileges"]

    for mount in spec.repo_mounts:
        # `ro` is not decorative: the fixture source is mounted into a container
        # running code the harness is deliberately trying to make misbehave.
        argv += ["--volume", f"{repo_root}/{mount}:{spec.mount_target(mount)}:ro"]
        argv += ["--workdir", MOUNT_ROOT]

    for key, value in sorted(env.items()):
        argv += ["--env", f"{key}={value}"]

    argv += [image_reference, *containerised_command(spec, command)]
    return argv


def containerised_command(spec: ContainerSpec, command: list[str]) -> list[str]:
    """Rewrite a launch command for execution inside the container.

    ``${PYTHON}`` means "the interpreter running the harness" on the host, which
    is a host path and meaningless inside an image. Inside a container it means
    the image's own interpreter, so it resolves to a bare name on PATH.
    """
    rewritten: list[str] = []
    for part in command:
        if PYTHON_TOKEN in part:
            rewritten.append(part.replace(PYTHON_TOKEN, CONTAINER_PYTHON))
            continue
        for mount in spec.repo_mounts:
            if part == mount or part.startswith(f"{mount}/"):
                suffix = part[len(mount) :].lstrip("/")
                target = spec.mount_target(mount)
                rewritten.append(f"{target}/{suffix}" if suffix else target)
                break
        else:
            rewritten.append(part)
    return rewritten


class EngineFacts(BaseModel):
    """What the daemon says about itself, for the build-resolution record.

    The storage driver is the load-bearing one. It decides whether a local image
    ID is a config ID or a rewritten manifest digest, which is the difference
    between two identifiers that look identical in shape and are not comparable.
    """

    model_config = ConfigDict(extra="forbid")

    client_version: str | None = None
    server_version: str | None = None
    storage_driver: str | None = None
    architecture: str | None = None
    cgroup_version: str | None = None


async def engine_facts() -> EngineFacts:
    """Read the daemon's own description of itself. Raises if it cannot be reached."""
    code, out, err = await run_docker(
        "docker",
        "info",
        "--format",
        "{{.Driver}}\t{{.Architecture}}\t{{.ServerVersion}}\t{{.CgroupVersion}}",
    )
    if code != 0:
        raise ContainmentUnavailableError(f"`docker info` failed: {(err or out).strip()}")
    driver, _, rest = out.strip().partition("\t")
    architecture, _, rest = rest.partition("\t")
    server_version, _, cgroup = rest.partition("\t")

    client_version: str | None = None
    code, version_out, _ = await run_docker("docker", "version", "--format", "{{json .}}")
    if code == 0:
        try:
            payload = json.loads(version_out)
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            client_version = str((payload.get("Client") or {}).get("Version") or "") or None

    return EngineFacts(
        client_version=client_version,
        server_version=server_version.strip() or None,
        storage_driver=driver.strip() or None,
        architecture=architecture.strip() or None,
        cgroup_version=cgroup.strip() or None,
    )


async def image_id_of(reference: str) -> str | None:
    """The engine's own identifier for whatever ``reference`` resolves to.

    ``None`` means the engine has no such image. That is deliberately not an
    exception: several callers need to say "not built here" without treating it
    as a failure, and exactly one — the local-build resolver — turns it into one.
    """
    code, out, _ = await run_docker("docker", "image", "inspect", reference, "--format", "{{.Id}}")
    if code != 0:
        return None
    return out.strip() or None


class _ImageIdentity(NamedTuple):
    """What the engine says about one image, kept as separate claims."""

    image_id: str | None = None
    #: Every `repository@digest` the engine reports, verbatim. The raw fact.
    #: A tuple because a NamedTuple default is shared across every instance.
    repo_digests: tuple[str, ...] = ()
    #: The harness's reading of which digest is registry provenance. An
    #: interpretation of the list above, and driver-specific — see below.
    repo_digest: str | None = None
    build_identity: str | None = None


async def _image_identity(reference: str) -> _ImageIdentity:
    """Read image ID, registry digests and build identity as separate facts.

    A locally built image has an ID and a build identity but no registry digest
    until it is pushed. A pulled image has a registry digest whose value is not
    its ID. Collapsing these into one field makes a comparison either always
    fail or always pass, depending which one happened to be recorded.
    """
    if not reference:
        return _ImageIdentity()

    code, out, _ = await run_docker("docker", "image", "inspect", reference)
    if code != 0:
        return _ImageIdentity()
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        return _ImageIdentity()
    if not isinstance(payload, list) or not payload:
        return _ImageIdentity()

    info: dict[str, Any] = payload[0]
    image_id = str(info.get("Id") or "") or None

    repo_digests = tuple(str(entry) for entry in info.get("RepoDigests") or [] if "@" in str(entry))
    first_digest = repo_digests[0].split("@", 1)[1] if repo_digests else None

    # How the image came to exist. The tell is not whether a RepoDigest exists —
    # under the classic storage driver a locally built, tagged image can get one
    # whose value is simply its own config digest. What usually distinguishes a
    # pulled image is that its registry digest differs from its image ID.
    #
    # "Usually" is doing real work in that sentence, which is why this value no
    # longer decides anything. Under the containerd snapshotter an image ID *is*
    # the manifest digest, so a pulled image's ID equals its own repository
    # digest and this heuristic reads it as locally built. It is kept as a
    # human-readable summary; ``compare_image_identity`` verifies against the
    # list above, which is a fact rather than a reading of one.
    created = str(info.get("Created") or "") or None
    looks_local = first_digest is None or first_digest == image_id
    build_identity = (
        f"locally-built created={created} id={image_id}"
        if looks_local
        else f"pulled created={created} manifest={first_digest}"
    )
    return _ImageIdentity(
        image_id=image_id,
        repo_digests=repo_digests,
        # Not reported as registry provenance when it is only a restatement of
        # the local ID: that would overstate what is known.
        repo_digest=None if looks_local else first_digest,
        build_identity=build_identity,
    )


async def observe(
    container_name: str, *, base: ContainerRuntimeFingerprint | None = None
) -> ContainerRuntimeFingerprint:
    """Read the running container's actual configuration back from the runtime."""
    code, out, err = await run_docker("docker", "inspect", container_name)
    if code != 0:
        raise ContainmentFailedError(f"could not inspect {container_name}: {err.strip()}")
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ContainmentFailedError(f"docker inspect returned non-JSON: {exc}") from exc
    if not isinstance(payload, list) or not payload:
        raise ContainmentFailedError(f"docker inspect returned nothing for {container_name}")

    info: dict[str, Any] = payload[0]
    host_config: dict[str, Any] = info.get("HostConfig") or {}
    config: dict[str, Any] = info.get("Config") or {}

    published: int | None = None
    ports = (info.get("NetworkSettings") or {}).get("Ports") or {}
    for bindings in ports.values():
        if isinstance(bindings, list) and bindings:
            with_port = bindings[0]
            if isinstance(with_port, dict) and with_port.get("HostPort"):
                published = int(with_port["HostPort"])
                break

    # The container inspect names the image it was started from; the image
    # inspect is what carries digests and build identity. They are read
    # separately because they answer different questions.
    started_from = str(config.get("Image") or "") or None
    identity = await _image_identity(started_from or str(info.get("Image") or ""))

    return ContainerRuntimeFingerprint(
        runtime="docker",
        runtime_version=base.runtime_version if base else None,
        server_version=base.server_version if base else None,
        requested_repo_digest=base.requested_repo_digest if base else None,
        observed_repo_digests=list(identity.repo_digests),
        observed_repo_digest=identity.repo_digest,
        observed_image_id=identity.image_id,
        build_identity=identity.build_identity,
        image_reference=started_from,
        storage_driver=base.storage_driver if base else None,
        container_id=str(info.get("Id") or "")[:12] or None,
        network_mode=str(host_config.get("NetworkMode") or "") or None,
        memory_limit_bytes=int(host_config.get("Memory") or 0) or None,
        pids_limit=int(host_config.get("PidsLimit") or 0) or None,
        nano_cpus=int(host_config.get("NanoCpus") or 0) or None,
        read_only_root=bool(host_config.get("ReadonlyRootfs")),
        user=str(config.get("User") or "") or None,
        cap_drop=[str(c) for c in (host_config.get("CapDrop") or [])],
        security_opt=[str(o) for o in (host_config.get("SecurityOpt") or [])],
        published_host_port=published,
    )


def compare_image_identity(
    observed: ContainerRuntimeFingerprint,
    resolved: ResolvedImage,
    *,
    label: str,
) -> list[str]:
    """Whether the container is running the bits that were resolved for it.

    Which field answers that depends on the source, and the two must not be
    compared against each other:

    * a **registry** image is identified by the repository digest the registry
      serves it under, which is *not* its local image ID;
    * a **local build** has no registry digest at all, and is identified by the
      engine's own image ID — the value the build-resolution manifest records.

    The earlier single-field comparison fell back from one to the other when a
    value was missing, which meant a locally built image whose engine happened to
    report a self-referential ``RepoDigest`` was checked against a digest it
    could never equal, and a registry image with no readable digest silently
    passed on its ID instead.
    """
    problems: list[str] = []

    if resolved.source_kind is ImageSourceKind.REGISTRY:
        expected = resolved.repository_digest
        if expected is None:  # pragma: no cover - the model requires it
            return [f"{label} registry source carries no repository digest to verify against"]

        # Matched against every digest the engine reports, rather than against
        # the harness's reading of which one counts as provenance. That reading
        # is driver-specific and this check must not be: under the containerd
        # snapshotter an image ID *is* the manifest digest, so a pulled image's
        # ID equals its own repository digest, the "pulled" heuristic reads it as
        # locally built, and a correctly pinned image was reported as unverifiable
        # containment — turning every contained HTTP run on such a daemon into a
        # HARNESS_ERROR.
        #
        # Nothing is loosened. The claim being verified is "the container ran the
        # bits the pinned digest names", and a `repository@digest` entry from the
        # engine states exactly that. The previous form verified it only on
        # daemons where a side effect of the storage driver happened to agree.
        observed_digests = {
            entry.split("@", 1)[1] for entry in observed.observed_repo_digests if "@" in entry
        }
        if observed.observed_repo_digest is not None:
            observed_digests.add(observed.observed_repo_digest)

        if not observed_digests:
            problems.append(
                f"no {label} repository digest could be read back, so the registry image "
                f"that ran cannot be verified against the pinned {expected}"
            )
        elif expected not in observed_digests:
            problems.append(
                f"{label} repository digest requested {expected}, container ran "
                f"{sorted(observed_digests)}"
            )
        return problems

    expected_id = resolved.expected_local_image_id
    if expected_id is None:  # pragma: no cover - the resolver requires it
        return [f"{label} local build produced no expected image ID to verify against"]
    if observed.observed_image_id is None:
        problems.append(
            f"no {label} image ID could be read back, so the locally built image that "
            f"ran cannot be verified against the resolved {expected_id}"
        )
    elif observed.observed_image_id != expected_id:
        problems.append(
            f"{label} resolved image ID {expected_id}, container ran "
            f"{observed.observed_image_id}. The build resolution and the running "
            "container disagree about which bits these are."
        )
    return problems


def compare_controls(
    observed: ContainerRuntimeFingerprint,
    *,
    label: str,
    resolved: ResolvedImage,
    memory_mb: int,
    cpus: float,
    pids_limit: int,
    read_only_root: bool,
    user: str,
    drop_all_capabilities: bool,
    no_new_privileges: bool,
) -> list[str]:
    """Compare the per-container controls that mean the same thing everywhere.

    Shared by the target and the probe. The probe is contained too, and a second
    copy of these comparisons would be the obvious place for the two to drift —
    with the probe's copy being the weaker one, since nothing routinely attacks
    it.
    """
    # Image identity first. Every other control is about how the container was
    # confined; this one is about whether the right bits ran at all, and a
    # perfectly confined wrong image is not the measurement anyone approved.
    weaknesses = compare_image_identity(observed, resolved, label=label)

    expected_memory = memory_mb * 1024 * 1024
    if observed.memory_limit_bytes != expected_memory:
        weaknesses.append(
            f"{label} memory limit requested {expected_memory} bytes, container has "
            f"{observed.memory_limit_bytes}"
        )

    expected_cpus = int(cpus * 1_000_000_000)
    if observed.nano_cpus != expected_cpus:
        weaknesses.append(
            f"{label} cpu limit requested {cpus} cpus, container has "
            f"{(observed.nano_cpus or 0) / 1_000_000_000}"
        )

    if observed.pids_limit != pids_limit:
        weaknesses.append(
            f"{label} pids limit requested {pids_limit}, container has {observed.pids_limit}. "
            "The pids cgroup controller may not be available on this host."
        )

    if read_only_root and not observed.read_only_root:
        weaknesses.append(
            f"{label} read-only root filesystem requested, container root is writable"
        )

    if user and observed.user != user:
        weaknesses.append(f"{label} user requested {user!r}, container runs as {observed.user!r}")

    if drop_all_capabilities and "ALL" not in observed.cap_drop:
        weaknesses.append(f"{label} cap-drop ALL requested, container dropped {observed.cap_drop}")

    if no_new_privileges and not any(
        "no-new-privileges" in option for option in observed.security_opt
    ):
        weaknesses.append(
            f"{label} no-new-privileges requested, container security options are "
            f"{observed.security_opt}"
        )

    return weaknesses


def compare(
    spec: ContainerSpec, observed: ContainerRuntimeFingerprint, *, resolved: ResolvedImage
) -> list[str]:
    """Name every requested control the running container does not have.

    A non-empty result means the containment claim in the artifact would be
    false. It is reported as a weakness rather than corrected, because the run
    already happened under the weaker isolation.
    """
    weaknesses = compare_controls(
        observed,
        label="target",
        resolved=resolved,
        memory_mb=spec.memory_mb,
        cpus=spec.cpus,
        pids_limit=spec.pids_limit,
        read_only_root=spec.read_only_root,
        user=spec.user,
        drop_all_capabilities=spec.drop_all_capabilities,
        no_new_privileges=spec.no_new_privileges,
    )

    if spec.network is NetworkMode.NONE and observed.network_mode != "none":
        weaknesses.append(
            f"network none requested, container network mode is {observed.network_mode!r}"
        )
    if spec.network is NetworkMode.PUBLISHED_LOOPBACK:
        if observed.network_mode != NO_EGRESS_NETWORK:
            weaknesses.append(
                f"no-egress network requested, container network mode is {observed.network_mode!r}"
            )
        if observed.published_host_port is None:
            weaknesses.append("a published loopback port was requested but none is bound")

    return weaknesses


def stated_limits(spec: ContainerSpec) -> list[str]:
    """What this containment does not protect against, for the run record."""
    limits = list(CONTAINMENT_LIMITS)
    if spec.network is NetworkMode.NONE:
        # The host-reachability caveat is specific to published_loopback.
        limits = [limit for limit in limits if "published_loopback" not in limit]
    return limits


async def remove_container(container_name: str) -> None:
    """Best-effort reclaim. A leaked container is a harness fault, not a finding."""
    await run_docker("docker", "rm", "--force", container_name, timeout_s=_INSPECT_TIMEOUT_S)


async def inspect_state(container_name: str) -> tuple[int, str, str]:
    """Ask whether a container still exists. Used to verify cleanup actually worked."""
    return await run_docker(
        "docker", "ps", "--all", "--quiet", "--filter", f"name=^{container_name}$"
    )
