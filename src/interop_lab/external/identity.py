"""Deriving a target's portable build identity from committed inputs.

Everything here is computed from files in this repository plus values the target
file declares. Nothing is read from a container runtime, and nothing depends on
the machine doing the computing — that is the property that makes the result
safe to fingerprint a target with and safe to ship in a reproduction package.

The one subtlety is the build command. Two builds from byte-identical inputs
produce different images if the builder was invoked differently, and this
project measured exactly that: without ``SOURCE_DATE_EPOCH`` and
``rewrite-timestamp`` every rebuild produced a new image ID because BuildKit
stamps each layer with the wall clock. So the flags are part of the identity,
declared here once and used by the builder from the same constant.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from interop_lab.containment.context import build_context_manifest
from interop_lab.containment.imagesource import LocalBuildImageSource
from interop_lab.containment.resolution import PortableBuildIdentity
from interop_lab.models.core import TargetDefinition

#: The canonical builder invocation, minus anything machine-specific. Ordered,
#: because the fingerprint below is a hash of it.
#:
#: SOURCE_DATE_EPOCH=0     fixes the timestamp BuildKit records
#: rewrite-timestamp=true  applies it to the layers, not only the config
#: --provenance=false      keeps the result a plain image rather than an index
#:                         with an attestation manifest, whose digest depends on
#:                         build metadata
#: unpack=false            BuildKit refuses rewrite-timestamp together with the
#:                         eager unpack that type=docker implies under the
#:                         containerd snapshotter
BUILD_COMMAND_TOKENS: tuple[str, ...] = (
    "SOURCE_DATE_EPOCH=0",
    "docker build",
    "--provenance=false",
    "--output=type=image,rewrite-timestamp=true,unpack=false",
)


def build_command_fingerprint() -> str:
    """One value standing for how the builder is invoked."""
    return hashlib.sha256(json.dumps(list(BUILD_COMMAND_TOKENS)).encode()).hexdigest()


def sha256_of(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def build_context_fingerprint(
    context: Path, *, repo_root: Path, dockerfile: Path | None = None
) -> str:
    """The effective committed Docker context, reduced to one value.

    This used to walk every regular file under the directory while its
    documentation said "every committed file in the build context". Those are
    different sets, and the gap between them was not theoretical: a scratch file
    left beside a Dockerfile is neither committed nor invisible to the builder,
    so the walk hashed it and made a value the rest of the project treats as
    portable depend on one machine's working tree.

    ``interop_lab.containment.context`` now enumerates the set exactly and refuses
    to describe a context it cannot describe precisely — including one holding an
    untracked file the builder would receive.
    """
    return build_context_manifest(context, repo_root=repo_root, dockerfile=dockerfile).fingerprint


def derive_portable_identity(target: TargetDefinition, *, repo_root: Path) -> PortableBuildIdentity:
    """The portable inputs of a locally built target.

    Raises for a target that is not locally built: a caller asking for the build
    inputs of a registry image has confused the two source kinds, and returning
    an empty identity would let that confusion travel.
    """
    spec = target.container
    source = spec.local_build_source if spec is not None else None
    if source is None:
        raise ValueError(
            f"target {target.target_id!r} has no local-build image source, so it has no "
            "build inputs. A registry image is identified by its repository digest."
        )
    return derive_from_source(
        source,
        repo_root=repo_root,
        source_repository=target.package_name,
        source_version=target.version,
        source_commit=target.commit_sha,
        source_archive_sha256=(
            target.supply_chain.source_archive_sha256 if target.supply_chain else None
        ),
        lock_sha256=target.supply_chain.lock_sha256 if target.supply_chain else None,
    )


def derive_from_source(
    source: LocalBuildImageSource,
    *,
    repo_root: Path,
    source_repository: str | None = None,
    source_version: str | None = None,
    source_commit: str | None = None,
    source_archive_sha256: str | None = None,
    lock_sha256: str | None = None,
) -> PortableBuildIdentity:
    """The same derivation, for a caller holding the source rather than the target."""
    dockerfile = repo_root / source.dockerfile
    if not dockerfile.is_file():
        raise FileNotFoundError(f"Dockerfile {dockerfile} does not exist")

    manifest = build_context_manifest(
        repo_root / source.build_context, repo_root=repo_root, dockerfile=dockerfile
    )
    return PortableBuildIdentity(
        build_key=source.build_key,
        source_repository=source_repository,
        source_version=source_version,
        source_commit=source_commit,
        source_archive_sha256=source_archive_sha256,
        lock_sha256=lock_sha256,
        dockerfile_sha256=sha256_of(dockerfile),
        base_image_digests=list(source.base_image_digests),
        build_command_fingerprint=build_command_fingerprint(),
        build_context_fingerprint=manifest.fingerprint,
        build_context_manifest=manifest,
        toolchain_pins=dict(source.toolchain_pins),
    )
