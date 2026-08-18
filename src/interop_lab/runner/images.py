"""Resolving the image a target will run, as an explicit dependency.

The alternative — a module-level cache the launch paths reach into — was
rejected deliberately. Image resolution decides *which bits are measured*, and a
process-global answer means two runs in one process can silently share a
resolution that only one of them was entitled to. It also makes the failure mode
invisible: a path that forgot to resolve would keep working, using whatever the
last caller happened to leave behind.

So an ``ImageResolver`` is constructed once and threaded through. A path that
was not given one cannot launch a locally built target at all, which is exactly
the property that makes "did we remember to wire this through?" a question the
type checker answers rather than a question CI answers three months later.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from interop_lab.containment import docker
from interop_lab.containment.imagesource import (
    ImageSourceKind,
    LocalBuildImageSource,
    RegistryImageSource,
)
from interop_lab.containment.resolution import (
    BuildResolutionIndex,
    ImageResolutionError,
    ImageVerificationStatus,
    ResolvedImage,
    resolve_registry_source,
)
from interop_lab.external.identity import derive_portable_identity
from interop_lab.models.core import TargetDefinition


class ImageResolver:
    """Turns a target into the image identity a run is allowed to execute.

    Holds no process-global state. A resolver without a build-resolution index
    can resolve registry sources — those are pinned in the target file and need
    nothing built — and refuses local builds with a message saying what to run.
    """

    def __init__(
        self,
        *,
        repo_root: Path,
        index: BuildResolutionIndex | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.index = index

    @property
    def resolution_manifest_sha256(self) -> str | None:
        """Identity of the resolution in force, or ``None`` when there is none.

        Part of the capability cache key: two builds of identical portable
        inputs are still two different sets of bits, and evidence gathered from
        one must not be reused for the other.
        """
        return self.index.manifest_sha256 if self.index is not None else None

    def require_resolvable(self, target: TargetDefinition) -> None:
        """The half of resolution that needs no container runtime.

        Called before the Docker availability check, deliberately. A target that
        is locally built with no resolution supplied is a configuration error on
        any machine, and reporting "the docker daemon is not reachable" instead
        would send someone to fix the wrong thing — most obviously on a replay
        machine that has simply not built the target yet.
        """
        spec = target.container
        if spec is None:
            raise ImageResolutionError(
                f"target {target.target_id!r} requests container execution without a "
                "container spec; there is no image to resolve"
            )
        source = spec.local_build_source
        if source is not None and self.index is None:
            raise ImageResolutionError(
                f"target {target.target_id!r} runs a locally built image "
                f"({source.build_key!r}) and no build resolution was supplied. The "
                "identity of that image is produced by the build and recorded nowhere "
                "else, so there is nothing to run and nothing to verify. Build it with "
                "`mcp-failure-lab external-build` and pass the resulting "
                "build-resolution.json."
            )

    async def resolve(self, target: TargetDefinition) -> ResolvedImage:
        """Resolve and verify, or raise ``ImageResolutionError``.

        Never returns an unverified local build. A caller that could proceed on
        "probably the right image" would produce evidence nobody can attribute.
        """
        self.require_resolvable(target)
        spec = target.container
        assert spec is not None  # require_resolvable rejects a missing spec

        source = spec.image_source
        if isinstance(source, RegistryImageSource):
            return resolve_registry_source(source)
        return await self._resolve_local_build(target, source)

    async def _resolve_local_build(
        self, target: TargetDefinition, source: LocalBuildImageSource
    ) -> ResolvedImage:
        from interop_lab.ci.baseline import target_fingerprint

        assert self.index is not None  # require_resolvable rejects a missing index

        try:
            portable = derive_portable_identity(target, repo_root=self.repo_root)
        except (OSError, ValueError) as exc:
            raise ImageResolutionError(
                f"{target.target_id}: the committed build inputs could not be read, so "
                f"the resolution cannot be checked against them: {exc}"
            ) from exc

        resolved = self.index.resolve_local_build(
            source,
            target_id=target.target_id,
            target_fingerprint=target_fingerprint(target),
            portable_identity=portable,
        )

        # The entry may be internally perfect and name an image this engine has
        # never seen — a manifest carried between machines, or a pruned cache.
        expected = resolved.expected_local_image_id or ""
        observed = await docker.image_id_of(expected)
        if observed is None:
            raise ImageResolutionError(
                f"{target.target_id}: the build resolution names image {expected}, and "
                "this Docker engine has no such image. The resolution was produced by a "
                "different builder, or the image has been removed. Rebuild here."
            )
        if observed != expected:
            raise ImageResolutionError(
                f"{target.target_id}: the engine resolves {expected} to {observed}. The "
                "resolved identity and the engine disagree, so what would run is not "
                "what was approved."
            )

        return resolved.model_copy(
            update={"verification": ImageVerificationStatus.VERIFIED_PRESENT}
        )


@dataclass(frozen=True)
class ExecutionContext:
    """What every path that can launch a target needs, passed rather than found.

    Small on purpose. It exists so adding a second thing of this kind — another
    resolver, another root — does not mean editing every signature between the
    CLI and the container.
    """

    repo_root: Path
    resolver: ImageResolver

    @classmethod
    def create(
        cls, *, repo_root: Path | None = None, index: BuildResolutionIndex | None = None
    ) -> ExecutionContext:
        root = repo_root or Path.cwd()
        return cls(repo_root=root, resolver=ImageResolver(repo_root=root, index=index))

    @property
    def resolution_identity(self) -> str:
        """Cache key component naming the resolution in force.

        ``"none"`` is a real value, not a missing one: a context with no
        resolution can still resolve registry targets, and evidence gathered
        under it is validly cached under that key.
        """
        return self.resolver.resolution_manifest_sha256 or "none"


def verify_started_container(
    resolved: ResolvedImage, observed_image_id: str | None, *, label: str = "target"
) -> ResolvedImage:
    """Fold what the started container reports back into the resolved identity.

    Called before the first protocol byte, so a mismatch is caught while nothing
    has been sent and no evidence has been produced. The returned record carries
    the observation either way — a verified run should be able to show *why* it
    is verified, not merely assert it.
    """
    updated = resolved.model_copy(update={"observed_container_image_id": observed_image_id})
    if resolved.source_kind is ImageSourceKind.REGISTRY:
        return updated
    if observed_image_id is not None and observed_image_id == resolved.expected_local_image_id:
        return updated.model_copy(update={"verification": ImageVerificationStatus.VERIFIED_RUNNING})
    return updated.model_copy(update={"verification": ImageVerificationStatus.MISMATCH})
