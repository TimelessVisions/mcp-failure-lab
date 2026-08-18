"""Shared fixtures for constructing image sources and resolutions in tests.

These exist so a test that is about *containment controls* does not have to
restate the whole image model to say "a perfectly ordinary registry image", and
so a test that is about *local-build resolution* constructs a manifest the same
way the real builder does. Without them every test grows its own idea of what a
valid resolution looks like, and the ones that drift are the ones that keep
passing after the model changes underneath them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from interop_lab.containment.imagesource import (
    ImageSourceKind,
    LocalBuildImageSource,
    RegistryImageSource,
)
from interop_lab.containment.resolution import (
    BuildResolutionEntry,
    BuildResolutionIndex,
    BuildResolutionManifest,
    ImageVerificationStatus,
    PortableBuildIdentity,
    ResolvedImage,
    resolve_registry_source,
)
from interop_lab.naming import HARNESS_VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]

PROBE_REPOSITORY = "mirror.gcr.io/library/python"
PROBE_DIGEST = "sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36"

#: A second, deliberately different digest. Used to prove the harness notices
#: when the image that ran is not the image that was resolved.
OTHER_DIGEST = "sha256:" + "b" * 64

#: A plausible builder-local image ID, distinct from every registry digest above
#: so a test cannot pass by comparing the wrong pair of fields.
LOCAL_IMAGE_ID = "sha256:" + "c" * 64


def registry_source(**overrides: Any) -> RegistryImageSource:
    base: dict[str, Any] = {
        "repository": PROBE_REPOSITORY,
        "repository_digest": PROBE_DIGEST,
        "display_tag": "3.12-slim",
    }
    base.update(overrides)
    return RegistryImageSource.model_validate(base)


def local_build_source(**overrides: Any) -> LocalBuildImageSource:
    base: dict[str, Any] = {
        "kind": "local_build",
        "build_key": "synthetic-python-stdio",
        "runtime_tag": "interop-lab/synthetic-python-stdio:1.2.3",
        "build_context": "targets/external/synthetic-python-stdio",
        "dockerfile": "targets/external/synthetic-python-stdio/Dockerfile",
    }
    base.update(overrides)
    return LocalBuildImageSource.model_validate(base)


def registry_resolution(**overrides: Any) -> ResolvedImage:
    """What a registry-pinned target resolves to. No build involved."""
    return resolve_registry_source(registry_source(**overrides))


def local_build_resolution(
    *,
    image_id: str = LOCAL_IMAGE_ID,
    verification: ImageVerificationStatus = ImageVerificationStatus.VERIFIED_PRESENT,
    portable: PortableBuildIdentity | None = None,
) -> ResolvedImage:
    """What a locally built target resolves to, already checked."""
    return ResolvedImage(
        source_kind=ImageSourceKind.LOCAL_BUILD,
        launch_reference=image_id,
        expected_local_image_id=image_id,
        runtime_tag="interop-lab/synthetic-python-stdio:1.2.3",
        portable_identity=portable or portable_identity(),
        resolution_manifest_sha256="0" * 64,
        builder="docker",
        storage_driver="overlayfs",
        verification=verification,
    )


def portable_identity(**overrides: Any) -> PortableBuildIdentity:
    """A synthetic portable identity, valid but not derived from the repository."""
    base: dict[str, Any] = {
        "build_key": "synthetic-python-stdio",
        "source_repository": "synthetic-python-stdio",
        "source_version": "1.2.3",
        "lock_sha256": "sha256:" + "1" * 64,
        "dockerfile_sha256": "sha256:" + "2" * 64,
        "base_image_digests": [f"{PROBE_REPOSITORY}@{PROBE_DIGEST}"],
        "build_command_fingerprint": "3" * 64,
        "build_context_fingerprint": "4" * 64,
    }
    base.update(overrides)
    return PortableBuildIdentity.model_validate(base)


def build_resolution(
    *,
    target_id: str = "external.mcp_server_time",
    target_fingerprint: str = "0123456789abcdef",
    build_key: str = "synthetic-python-stdio",
    runtime_tag: str = "interop-lab/synthetic-python-stdio:1.2.3",
    image_id: str = LOCAL_IMAGE_ID,
    portable: PortableBuildIdentity | None = None,
    harness_commit: str | None = None,
    storage_driver: str = "overlayfs",
) -> BuildResolutionManifest:
    """One valid manifest, built the way the real builder builds one."""
    identity = portable or portable_identity()
    return BuildResolutionManifest(
        harness_commit=harness_commit,
        harness_version=HARNESS_VERSION,
        generated_at=datetime.now(UTC).isoformat(),
        builder="docker",
        builder_version="28.0.0",
        docker_engine_version="28.0.0",
        storage_driver=storage_driver,
        architecture="x86_64",
        entries=[
            BuildResolutionEntry(
                target_id=target_id,
                target_fingerprint=target_fingerprint,
                build_key=build_key,
                portable_identity=identity,
                portable_identity_fingerprint=identity.fingerprint,
                runtime_tag=runtime_tag,
                observed_local_image_id=image_id,
            )
        ],
    )


def build_resolution_index(**kwargs: Any) -> BuildResolutionIndex:
    manifest = build_resolution(**kwargs)
    return BuildResolutionIndex(manifest, manifest_sha256="f" * 64)
