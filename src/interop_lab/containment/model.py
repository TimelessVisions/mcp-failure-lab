"""Typed containment: how a target is executed, and what that actually isolated.

The distinction this module exists to protect is between *requesting* isolation
and *having* it. A target definition asks for a container; the container runtime
grants some of it, silently ignores parts of it on some platforms, and cannot
grant others at all. Recording only the request would let an artifact claim
containment that never happened.

So every field here appears twice in the record: requested, and observed. A
mismatch is reported, never reconciled.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, model_validator

from interop_lab.containment.imagesource import (
    ImageSource,
    ImageSourceKind,
    LocalBuildImageSource,
    RegistryImageSource,
    reject_legacy_image_fields,
)
from interop_lab.containment.resolution import ResolvedImage


class ExecutionMode(StrEnum):
    """Where a target runs.

    There is deliberately no ``auto``. A mode that silently degrades to host
    execution when a container is unavailable produces artifacts that cannot be
    read: the same run, on two machines, would mean two different things.
    """

    #: An ordinary local subprocess. No isolation beyond process-group cleanup.
    HOST = "host"
    #: A container. If the runtime is unavailable the run fails; it never falls
    #: back to the host.
    CONTAINER = "container"


class Provenance(StrEnum):
    """Who wrote the code being executed.

    This decides whether host execution is defensible. A fixture written in this
    repository is code we control; a third-party server is not, and the harness
    refuses to run it uncontained.
    """

    #: Written in this repository, under `fixtures/`. Conformance fully controlled.
    LOCAL_FIXTURE = "local_fixture"
    #: An official SDK installed here at a pinned version, used as a reference.
    PINNED_DEPENDENCY = "pinned_dependency"
    #: Third-party code. Containment is mandatory.
    EXTERNAL = "external"


class NetworkMode(StrEnum):
    """What the contained target can talk to.

    Neither value is "sandboxed" in a security sense, and the difference between
    them is not cosmetic — see `docs/containment.md` for what each one actually
    denies.
    """

    #: `--network none`. No interfaces but loopback. The correct choice for
    #: stdio, where the harness needs no socket to the target at all.
    NONE = "none"
    #: A user-defined bridge with IP masquerading disabled, and a single port
    #: published to 127.0.0.1. Denies Internet egress by removing NAT, but does
    #: **not** deny reaching services on the container host: a target under this
    #: mode can still connect to the Docker gateway.
    #:
    #: Retained for the stdio-adjacent case where the harness itself must open
    #: the socket. For HTTP targets prefer ISOLATED_SIDECAR, which is strictly
    #: stronger.
    PUBLISHED_LOOPBACK = "published_loopback"
    #: A Docker `--internal` network with **no port published to the host at
    #: all**. The target is reachable only from a probe sidecar on the same
    #: network; the harness never opens a socket to it.
    #:
    #: Measured, not assumed: a container here has no default route, so it
    #: reaches neither the Internet nor the container host. That closes the
    #: gateway-reachability hole that published_loopback leaves open, which is
    #: why it is the required mode for untrusted HTTP targets.
    ISOLATED_SIDECAR = "isolated_sidecar"


class ContainmentOutcome(StrEnum):
    """What isolation the run actually had. Never inferred from the request."""

    #: A container was requested and every requested control was observed.
    CONTAINED = "contained"
    #: Host execution was requested by a target allowed to use it.
    NOT_CONTAINED_BY_DESIGN = "not_contained_by_design"
    #: A container was requested and the runtime is not usable here. This is an
    #: environment failure. It is never a PASS and never a target verdict.
    CONTAINMENT_UNAVAILABLE = "containment_unavailable"
    #: The container runtime was available but the container could not be started.
    CONTAINMENT_FAILED = "containment_failed"
    #: The container started, but a requested control is not present on the
    #: running container. The isolation claim is false and the run is not usable
    #: as evidence about the target.
    CONTAINMENT_WEAKER_THAN_REQUESTED = "containment_weaker_than_requested"


#: Outcomes in which nothing may be concluded about the target.
UNUSABLE_CONTAINMENT = frozenset(
    {
        ContainmentOutcome.CONTAINMENT_UNAVAILABLE,
        ContainmentOutcome.CONTAINMENT_FAILED,
        ContainmentOutcome.CONTAINMENT_WEAKER_THAN_REQUESTED,
    }
)


#: Writable scratch inside the container, since the root filesystem is read-only.
#: Named rather than inlined so the literal path is declared once.
SCRATCH_MOUNT = "/tmp"  # noqa: S108 - a path inside a container, not a host tempfile

#: Where repository-relative mounts appear inside the container.
MOUNT_ROOT = "/opt/interop"


class ContainerSpec(BaseModel):
    """The isolation a target definition asks for.

    Defaults are the strict end of every axis. Weakening one is an explicit edit
    to a pinned target file, which shows up in the target fingerprint and so
    invalidates any baseline recorded under the stronger setting.
    """

    model_config = ConfigDict(extra="forbid")

    #: Where the bits come from, and therefore how their identity is established.
    #: A registry source resolves to ``repository@digest`` and needs nothing
    #: built; a local-build source carries no digest at all and is resolved
    #: against a build-resolution manifest at execution time.
    image_source: ImageSource

    network: NetworkMode = NetworkMode.NONE
    memory_mb: int = 512
    cpus: float = 1.0
    pids_limit: int = 128
    read_only_root: bool = True
    #: Numeric uid:gid. A name would have to be resolved inside the image, which
    #: means trusting the image's passwd file to describe an unprivileged user.
    user: str = "65534:65534"
    drop_all_capabilities: bool = True
    no_new_privileges: bool = True
    #: Writable scratch, since the root filesystem is read-only.
    tmpfs_paths: list[str] = Field(default_factory=lambda: [SCRATCH_MOUNT])
    #: Repository-relative directories mounted read-only at /opt/<basename>.
    #: Relative by construction: an absolute host path in a target file would
    #: travel into every artifact and stop resolving on any other machine.
    repo_mounts: list[str] = Field(default_factory=list)
    #: Port the server listens on inside the container. HTTP transports only.
    container_port: int | None = None
    startup_timeout_s: float = 30.0

    @model_validator(mode="before")
    @classmethod
    def _refuse_the_pre_v3_image_model(cls, values: object) -> object:
        reject_legacy_image_fields(values)
        return values

    @model_validator(mode="after")
    def _mounts_stay_inside_the_repository(self) -> ContainerSpec:
        for mount in self.repo_mounts:
            if PurePosixPath(mount).is_absolute() or ".." in PurePosixPath(mount).parts:
                raise ValueError(
                    f"repo_mount {mount!r} must be a repository-relative path without "
                    "'..'; mounting an arbitrary host directory into an untrusted "
                    "container is exactly what containment is for"
                )
        return self

    @model_validator(mode="after")
    def _http_needs_a_reachable_port(self) -> ContainerSpec:
        if self.network is NetworkMode.ISOLATED_SIDECAR and self.container_port is None:
            raise ValueError(
                "network=isolated_sidecar requires container_port: the probe "
                "sidecar has to know which port to connect to"
            )
        if self.network is NetworkMode.PUBLISHED_LOOPBACK and self.container_port is None:
            raise ValueError(
                "network=published_loopback requires container_port: the harness has "
                "to know which port to publish"
            )
        if self.network is NetworkMode.NONE and self.container_port is not None:
            raise ValueError(
                "container_port is set but network=none, so nothing can reach it; "
                "use published_loopback or drop the port"
            )
        return self

    def mount_target(self, repo_mount: str) -> str:
        """Where a repo mount appears inside the container."""
        return f"{MOUNT_ROOT}/{PurePosixPath(repo_mount).name}"

    @property
    def source_kind(self) -> ImageSourceKind:
        """Registry or local build. Decides how identity is established."""
        return self.image_source.kind

    @property
    def registry_source(self) -> RegistryImageSource | None:
        """The registry source, or ``None`` for a locally built image."""
        source = self.image_source
        return source if isinstance(source, RegistryImageSource) else None

    @property
    def local_build_source(self) -> LocalBuildImageSource | None:
        """The local-build source, or ``None`` for a registry image."""
        source = self.image_source
        return source if isinstance(source, LocalBuildImageSource) else None

    @property
    def image_name(self) -> str:
        """Repository of the image, without tag or digest. For display only."""
        source = self.image_source
        if isinstance(source, RegistryImageSource):
            return source.repository
        return source.image_name


class ContainerRuntimeFingerprint(BaseModel):
    """What the runtime reported about the container that actually ran.

    Read back from the running container rather than assumed from the arguments
    the harness passed, because those are two different claims.
    """

    model_config = ConfigDict(extra="forbid")

    runtime: str = "docker"
    runtime_version: str | None = None
    server_version: str | None = None

    # Four distinct claims about image identity, kept apart because conflating
    # them is how a "verified" image turns out to be a different one:
    #
    #   requested_repo_digest  what the target file pinned
    #   observed_repo_digests  every `repo@digest` the engine reports, verbatim
    #   observed_repo_digest   the harness's reading of which one is provenance
    #   observed_image_id      the local content digest of the config
    #   build_identity         how a locally built image came to exist
    #
    # A locally built image has an image ID and a build identity but no registry
    # digest until it is pushed; a pulled image has a registry digest whose ID
    # differs from it. Recording one field for all of that would make a
    # comparison either always fail or always pass.
    #
    # The plural field is the raw fact and the singular one is an interpretation
    # of it, and they are separate because the interpretation is driver-specific
    # while the fact is not. Under the containerd snapshotter an image ID *is*
    # the manifest digest, so a pulled image's ID equals its own repository
    # digest — the tell that used to distinguish "pulled" from "locally built"
    # simply does not hold there. Verification therefore reads the list.
    requested_repo_digest: str | None = None
    observed_repo_digests: list[str] = Field(default_factory=list)
    observed_repo_digest: str | None = None
    observed_image_id: str | None = None
    build_identity: str | None = None
    image_reference: str | None = None
    #: Which driver produced the local identifier above. Recorded because it is
    #: what decides whether two image IDs are even comparable.
    storage_driver: str | None = None
    container_id: str | None = None
    network_mode: str | None = None
    memory_limit_bytes: int | None = None
    pids_limit: int | None = None
    nano_cpus: int | None = None
    read_only_root: bool | None = None
    user: str | None = None
    cap_drop: list[str] = Field(default_factory=list)
    security_opt: list[str] = Field(default_factory=list)
    published_host_port: int | None = None


class ContainmentRecord(BaseModel):
    """The containment half of a run's evidence."""

    model_config = ConfigDict(extra="forbid")

    mode: ExecutionMode
    provenance: Provenance
    outcome: ContainmentOutcome
    requested: ContainerSpec | None = None
    observed: ContainerRuntimeFingerprint | None = None
    #: Which image the run resolved, how, and how far that was verified. Carried
    #: on the record rather than derived from the spec, because for a local
    #: build the spec deliberately does not know: the identity comes from a
    #: build-resolution manifest and only exists once something has been built.
    resolved_image: ResolvedImage | None = None
    #: Each entry names one requested control that the running container did not
    #: have. Non-empty means the containment claim is false.
    weaknesses: list[str] = Field(default_factory=list)
    #: Why the runtime could not be used, when it could not be.
    unavailable_reason: str | None = None
    #: What this containment does *not* protect against. Always populated for a
    #: contained run, because "contained" invites a stronger reading than is
    #: warranted.
    stated_limits: list[str] = Field(default_factory=list)

    @property
    def is_usable_evidence(self) -> bool:
        """Whether anything may be concluded about the target from this run."""
        return self.outcome not in UNUSABLE_CONTAINMENT


#: What a container of this shape genuinely does not protect against. Attached to
#: every contained run so a reader cannot mistake it for a security boundary.
CONTAINMENT_LIMITS: tuple[str, ...] = (
    "Docker is not a security boundary against a kernel exploit; a container "
    "escape defeats every control listed here.",
    "The image is trusted. Containment isolates the target's behaviour at "
    "runtime; it says nothing about what the image contains.",
    "Under published_loopback the target can still reach services listening on "
    "the container host, because egress is denied by removing NAT rather than by "
    "a firewall rule.",
    "Wall-clock and CPU limits bound resource use, not information disclosure.",
)
