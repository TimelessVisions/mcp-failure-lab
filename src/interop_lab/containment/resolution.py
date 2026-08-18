"""Portable build identity, and the runtime resolution that is not portable.

Two facts about a locally built image are routinely confused, and keeping them
apart is the whole point of this module.

**What it was built from** is portable. The upstream archive hash, the
dependency lock hash, the Dockerfile hash, the base images' registry digests and
the committed build context are all values any machine can recompute and
compare. That is ``PortableBuildIdentity``, and it is what a target file, a
fingerprint and a reproduction package may legitimately carry.

**What came out** is not portable, and this repository measured why rather than
assuming it. A daemon using the containerd snapshotter exports a manifest digest
with ``rewrite-timestamp`` applied; one using the classic storage driver writes
a config ID and skips the rewrite silently::

    containerd snapshotter   exporting manifest sha256:1905b23d…
    classic storage driver   writing image     sha256:5c1f07c5…

No committed value can equal both. So the builder-local image ID lives in a
``BuildResolutionManifest`` written by the builder that produced it, next to the
facts that explain it — engine version, storage driver, architecture — and never
in a tracked target file, where it would read as a guarantee no second machine
could honour.

``ResolvedImage`` is what the two halves produce together: a launch reference,
the identity that reference must turn out to have, and a verification status
that is never assumed. A resolution that does not match is a ``HARNESS_ERROR``,
because the harness could not run what it was asked to run — it is never a
verdict about the target, and never a skipped test.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from interop_lab.containment.context import BuildContextManifest
from interop_lab.containment.imageref import DIGEST_PATTERN
from interop_lab.containment.imagesource import (
    ImageSourceKind,
    LocalBuildImageSource,
    RegistryImageSource,
)

#: Schema of the portable identity. Bump when the material below changes, since
#: a fingerprint computed under two schemes is two different numbers.
#:
#: v2 carries the build-context manifest and derives the context fingerprint from
#: it. v1's context fingerprint covered whatever regular files happened to be
#: under the directory, which is a different set from the committed one it
#: claimed to describe.
PORTABLE_BUILD_IDENTITY_SCHEMA_VERSION = "2"

#: Schema of the on-disk build-resolution manifest.
BUILD_RESOLUTION_SCHEMA_VERSION = "1"

#: Default location the build command writes to and the runtime reads from.
DEFAULT_RESOLUTION_PATH = Path("artifacts/external/build-resolution.json")


class ImageResolutionError(Exception):
    """The image that should run could not be established, so nothing may run.

    Raised rather than returning a degraded result. Every caller turns this into
    ``HARNESS_ERROR``: the harness failed to identify what it was about to
    execute, which says nothing whatsoever about the target.
    """


class BuildStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ImageVerificationStatus(StrEnum):
    """How far the identity of the image that will run has actually been checked."""

    #: A registry source pinned by digest. The runtime resolves the digest, so
    #: there is no local resolution to verify.
    REGISTRY_DIGEST_PINNED = "registry_digest_pinned"
    #: A local build matched to a valid resolution entry, not yet inspected.
    RESOLVED = "resolved"
    #: The engine was asked, and it reports exactly the resolved image ID.
    VERIFIED_PRESENT = "verified_present"
    #: The container that started is running the resolved image ID.
    VERIFIED_RUNNING = "verified_running"
    #: The engine resolved something other than the expected identity.
    MISMATCH = "mismatch"


def _canonical_fingerprint(material: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def is_local_image_id(value: str) -> bool:
    """Whether a value has the immutable form Docker uses for an image ID."""
    return bool(DIGEST_PATTERN.match(value))


class PortableBuildIdentity(BaseModel):
    """What a local build was built *from* — every field reproducible anywhere.

    Nothing here depends on the machine that ran the build. That is the property
    that makes it safe to fingerprint a target with, safe to commit, and safe to
    ship inside a reproduction package: a second machine can recompute all of it
    and say whether it is about to build the same thing.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = PORTABLE_BUILD_IDENTITY_SCHEMA_VERSION

    build_key: str
    #: Upstream project identity, as the target file declares it.
    source_repository: str | None = None
    source_version: str | None = None
    source_commit: str | None = None
    #: Upstream archive the Dockerfile verifies before unpacking, where there is
    #: one. Absent for an ecosystem whose closure ships as a lockfile instead.
    source_archive_sha256: str | None = None
    #: The dependency closure: package-lock.json, requirements.lock.txt, go.sum.
    lock_sha256: str | None = None
    #: The Dockerfile itself, hashed from the committed build context.
    dockerfile_sha256: str
    #: Base images the build pulls, pinned by registry digest.
    base_image_digests: list[str] = Field(default_factory=list)
    #: The builder invocation, minus anything machine-specific. Two builds whose
    #: flags differ are not the same build even from identical inputs — this
    #: repository measured `rewrite-timestamp` changing the output.
    build_command_fingerprint: str
    #: The effective committed Docker context, reduced to one value. A file added
    #: to the context changes the build.
    build_context_fingerprint: str
    #: The manifest that value was derived from: every path the builder receives,
    #: with its type, content hash and executable bit.
    #:
    #: Carried rather than left to be recomputed, so a reproduction package can
    #: say *which file* differs when a rebuild elsewhere produces a different
    #: fingerprint. A bare hash mismatch tells the reader that the context is not
    #: the same and gives them nothing to go on.
    build_context_manifest: BuildContextManifest | None = None
    #: Versions that change the output but live outside the context.
    toolchain_pins: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _hashes_are_hashes(self) -> PortableBuildIdentity:
        for field, value in (
            ("source_archive_sha256", self.source_archive_sha256),
            ("lock_sha256", self.lock_sha256),
            ("dockerfile_sha256", self.dockerfile_sha256),
        ):
            if value is not None and not DIGEST_PATTERN.match(value):
                raise ValueError(f"{field} {value!r} is not a sha256:… digest")
        return self

    @property
    def fingerprint(self) -> str:
        """One value standing for every portable build input.

        Deliberately excludes nothing machine-independent and includes nothing
        machine-specific: the builder's image ID, its storage driver and the
        wall clock are all absent, because a target built from identical inputs
        on two machines is the same target.
        """
        return _canonical_fingerprint(self.model_dump(mode="json"))


class BuildResolutionEntry(BaseModel):
    """What one builder produced for one target, on one machine, once."""

    model_config = ConfigDict(extra="forbid")

    target_id: str
    #: The portable target fingerprint at build time. A target file edited after
    #: the build no longer describes the image this entry names.
    target_fingerprint: str
    build_key: str
    portable_identity: PortableBuildIdentity
    #: Carried explicitly as well as derivable, so a manifest read by an older
    #: harness still says which inputs the builder believed it was using.
    portable_identity_fingerprint: str
    runtime_tag: str
    #: The builder-local image ID. This is the field that must never be
    #: committed to a target file, and the reason this manifest exists.
    observed_local_image_id: str
    #: Recorded only when the builder genuinely emits one. A daemon on the
    #: classic storage driver does not, and inventing a value would manufacture
    #: cross-driver stability that has not been established.
    oci_manifest_digest: str | None = None
    oci_archive_sha256: str | None = None
    build_duration_ms: float = 0.0
    build_status: BuildStatus = BuildStatus.SUCCEEDED
    #: What this entry does not establish. Always populated, because a bare
    #: image ID invites a stronger reading than it can support.
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _the_image_id_has_the_immutable_form(self) -> BuildResolutionEntry:
        if self.build_status is BuildStatus.SUCCEEDED and not is_local_image_id(
            self.observed_local_image_id
        ):
            raise ValueError(
                f"observed_local_image_id {self.observed_local_image_id!r} is not a "
                "sha256:… image ID. A tag here would let the resolution point at "
                "whatever that name resolves to at run time, which is the substitution "
                "this manifest exists to prevent."
            )
        if self.portable_identity_fingerprint != self.portable_identity.fingerprint:
            raise ValueError(
                f"{self.target_id}: portable_identity_fingerprint "
                f"{self.portable_identity_fingerprint} does not match the recorded "
                f"identity, which fingerprints to {self.portable_identity.fingerprint}"
            )
        if self.build_key != self.portable_identity.build_key:
            raise ValueError(
                f"{self.target_id}: entry build_key {self.build_key!r} disagrees with "
                f"its portable identity's {self.portable_identity.build_key!r}"
            )
        return self


class BuildResolutionManifest(BaseModel):
    """Every local image one builder produced, with the facts that explain them.

    The engine, storage driver and architecture are recorded because they are
    what decides which *kind* of identifier ``observed_local_image_id`` is. A
    reader comparing two manifests from different drivers must be able to see
    that the values are not comparable, rather than concluding the build drifted.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = BUILD_RESOLUTION_SCHEMA_VERSION
    harness_commit: str | None = None
    harness_version: str
    generated_at: str

    builder: str = "docker"
    builder_version: str | None = None
    docker_engine_version: str | None = None
    storage_driver: str | None = None
    architecture: str | None = None

    entries: list[BuildResolutionEntry] = Field(default_factory=list)
    limitations: list[str] = Field(
        default_factory=lambda: [
            "observed_local_image_id is this builder's identifier for these bits. It "
            "is not portable: the containerd snapshotter and the classic storage "
            "driver produce different identifiers for the same build.",
            "Cross-driver OCI manifest stability has not been established by this "
            "repository. An oci_manifest_digest is recorded only where the builder "
            "emitted one.",
        ]
    )

    @model_validator(mode="after")
    def _one_entry_per_target(self) -> BuildResolutionManifest:
        ids = [entry.target_id for entry in self.entries]
        if len(set(ids)) != len(ids):
            raise ValueError(f"build resolution has duplicate target entries: {sorted(ids)}")
        return self

    def entry_for(self, target_id: str) -> BuildResolutionEntry | None:
        for entry in self.entries:
            if entry.target_id == target_id:
                return entry
        return None


class ResolvedImage(BaseModel):
    """The image a run is about to execute, and how well that is established.

    ``launch_reference`` is what the runtime is given. For a registry source it
    is ``repository@digest``; for a local build it is the raw image ID, never
    the tag — a tag can be retargeted between resolution and launch, and running
    through one would make every check above it decorative.
    """

    model_config = ConfigDict(extra="forbid")

    source_kind: ImageSourceKind
    launch_reference: str

    #: Local-build only. What the run expects the engine to resolve.
    expected_local_image_id: str | None = None
    #: Read back from the created or running container before any protocol byte.
    observed_container_image_id: str | None = None
    #: Registry only. The digest the registry serves these bits under.
    repository_digest: str | None = None
    #: Informational in both cases.
    runtime_tag: str | None = None

    portable_identity: PortableBuildIdentity | None = None
    resolution_manifest_sha256: str | None = None
    #: Facts about the builder, carried into the run record so a reader can see
    #: which kind of identifier ``expected_local_image_id`` is.
    builder: str | None = None
    storage_driver: str | None = None

    verification: ImageVerificationStatus

    @property
    def is_verified(self) -> bool:
        return self.verification in {
            ImageVerificationStatus.REGISTRY_DIGEST_PINNED,
            ImageVerificationStatus.VERIFIED_PRESENT,
            ImageVerificationStatus.VERIFIED_RUNNING,
        }

    @property
    def expected_identity(self) -> str:
        """The identity a started container must report, whichever kind it is."""
        return self.expected_local_image_id or self.repository_digest or self.launch_reference


def resolve_registry_source(source: RegistryImageSource) -> ResolvedImage:
    """A registry source needs nothing built and nothing looked up."""
    return ResolvedImage(
        source_kind=ImageSourceKind.REGISTRY,
        launch_reference=source.reference,
        repository_digest=source.repository_digest,
        runtime_tag=source.display_tag,
        verification=ImageVerificationStatus.REGISTRY_DIGEST_PINNED,
    )


class BuildResolutionIndex:
    """A loaded resolution manifest, and the checks a local build must survive.

    Every check here is one that has a failure mode worth naming:

    * the target id, so an entry for another target cannot satisfy this one;
    * the target fingerprint, so a target file edited after the build is caught;
    * the build key, so two targets sharing a fingerprint cannot cross over;
    * the portable input fingerprint, so a rebuild from changed sources is
      caught even when the target file did not move;
    * the harness commit, where a caller demands it;
    * the shape of the image ID, so a tag cannot arrive where an identity
      belongs.

    None of them is sufficient alone, and the Docker-side checks in
    ``ImageResolver`` are the remaining half: an entry can be perfectly valid
    and name an image this engine has never seen.
    """

    def __init__(
        self,
        manifest: BuildResolutionManifest,
        *,
        manifest_sha256: str | None = None,
        source_path: Path | None = None,
        require_harness_commit: str | None = None,
    ) -> None:
        self.manifest = manifest
        self.manifest_sha256 = manifest_sha256
        self.source_path = source_path
        self.require_harness_commit = require_harness_commit

    @classmethod
    def load(cls, path: Path, *, require_harness_commit: str | None = None) -> BuildResolutionIndex:
        """Read and validate a manifest, or say exactly why it is unusable."""
        try:
            raw_bytes = path.read_bytes()
        except OSError as exc:
            raise ImageResolutionError(
                f"no build-resolution manifest at {path}: {exc}. A locally built target "
                "cannot be executed without one — the identity of the image to run is "
                "produced by the build and exists nowhere else. Run "
                "`mcp-failure-lab external-build` first."
            ) from exc

        try:
            payload = json.loads(raw_bytes)
        except json.JSONDecodeError as exc:
            raise ImageResolutionError(f"{path} is not valid JSON: {exc}") from exc

        version = payload.get("schema_version") if isinstance(payload, dict) else None
        if version != BUILD_RESOLUTION_SCHEMA_VERSION:
            raise ImageResolutionError(
                f"{path} declares build-resolution schema {version!r}, but this harness "
                f"reads {BUILD_RESOLUTION_SCHEMA_VERSION!r}. Regenerate it with "
                "`mcp-failure-lab external-build`; reinterpreting it would mean guessing "
                "what the missing fields would have said."
            )

        try:
            manifest = BuildResolutionManifest.model_validate(payload)
        except Exception as exc:
            raise ImageResolutionError(f"{path} is not a valid build resolution: {exc}") from exc

        return cls(
            manifest,
            manifest_sha256=hashlib.sha256(raw_bytes).hexdigest(),
            source_path=path,
            require_harness_commit=require_harness_commit,
        )

    def resolve_local_build(
        self,
        source: LocalBuildImageSource,
        *,
        target_id: str,
        target_fingerprint: str,
        portable_identity: PortableBuildIdentity,
    ) -> ResolvedImage:
        """Match a target to its entry, refusing every way they can disagree."""
        entry = self.manifest.entry_for(target_id)
        if entry is None:
            known = sorted(e.target_id for e in self.manifest.entries)
            raise ImageResolutionError(
                f"the build resolution has no entry for target {target_id!r} "
                f"(it has {known}). The image this target runs was never built here, "
                "so there is nothing to execute and nothing to verify."
            )

        if entry.build_status is not BuildStatus.SUCCEEDED:
            raise ImageResolutionError(
                f"{target_id}: the recorded build did not succeed "
                f"({entry.build_status.value}), so no image exists to run"
            )

        if entry.build_key != source.build_key:
            raise ImageResolutionError(
                f"{target_id}: the target builds {source.build_key!r} but the resolution "
                f"records {entry.build_key!r}. These are different images."
            )

        if entry.target_fingerprint != target_fingerprint:
            raise ImageResolutionError(
                f"{target_id}: the resolution was written for target fingerprint "
                f"{entry.target_fingerprint}, and this target fingerprints to "
                f"{target_fingerprint}. The target definition changed after the build, "
                "so the recorded image is not the one this run would be measuring. "
                "Rebuild."
            )

        expected_inputs = portable_identity.fingerprint
        if entry.portable_identity_fingerprint != expected_inputs:
            raise ImageResolutionError(
                f"{target_id}: the resolution was built from inputs fingerprinting to "
                f"{entry.portable_identity_fingerprint}, and the committed inputs now "
                f"fingerprint to {expected_inputs}. Something the image is built from "
                "changed since the build. Rebuild."
            )

        if self.require_harness_commit is not None:
            recorded = self.manifest.harness_commit
            if recorded != self.require_harness_commit:
                raise ImageResolutionError(
                    f"{target_id}: the resolution was produced at harness commit "
                    f"{recorded!r}, and this run requires {self.require_harness_commit!r}. "
                    "Evidence is attributed to one commit or to none."
                )

        if not is_local_image_id(entry.observed_local_image_id):
            raise ImageResolutionError(
                f"{target_id}: the resolution records "
                f"{entry.observed_local_image_id!r}, which is not an immutable image ID"
            )

        return ResolvedImage(
            source_kind=ImageSourceKind.LOCAL_BUILD,
            # The raw ID, never the tag. A tag is a name the build applied and
            # anything could have moved since; the ID is the bits.
            launch_reference=entry.observed_local_image_id,
            expected_local_image_id=entry.observed_local_image_id,
            runtime_tag=source.runtime_tag,
            portable_identity=entry.portable_identity,
            resolution_manifest_sha256=self.manifest_sha256,
            builder=self.manifest.builder,
            storage_driver=self.manifest.storage_driver,
            verification=ImageVerificationStatus.RESOLVED,
        )
