"""Building the external images, and recording what this builder produced.

The previous version of this wrote the built image ID back into the tracked
target file with ``--write-pins``. That was wrong in two ways at once.

It made a build **mutate its own inputs**: the target file is what the target
fingerprint is computed from, so recording the output into it changed the
identity of the thing that had just been measured. CI then ran the battery
against a working tree that no longer matched the commit under test, and
``git diff`` after a build was permanently dirty.

And it recorded a **machine-local value as though it were portable**. The image
ID a builder produces depends on its storage driver — the containerd
snapshotter exports a rewritten manifest digest, the classic driver writes a
config ID — so the committed pin was a value no second machine could reproduce,
sitting in a field that reads as a reproducibility guarantee.

So the build writes a separate artifact instead. The tracked tree is untouched,
which CI asserts with ``git diff --exit-code``; the portable inputs stay
committed and are verified before anything is built; and the one value that is
genuinely local to this builder lives in the resolution, next to the engine
facts that explain what kind of identifier it is.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from interop_lab.containment.resolution import (
    BuildResolutionEntry,
    BuildResolutionManifest,
    BuildStatus,
    PortableBuildIdentity,
    is_local_image_id,
)
from interop_lab.external.identity import derive_portable_identity
from interop_lab.external.images import ExternalImage, load_images
from interop_lab.naming import HARNESS_VERSION

#: BuildKit prints this under the containerd snapshotter and not under the
#: classic storage driver. Parsed rather than assumed, and recorded only when it
#: is genuinely there: inventing a value would manufacture the cross-driver
#: stability this project measured it does not have.
_EXPORTED_MANIFEST = re.compile(r"exporting manifest (sha256:[0-9a-f]{64})")

#: What a build-resolution entry does not establish, attached to every one.
ENTRY_LIMITATIONS: tuple[str, ...] = (
    "observed_local_image_id is this engine's identifier for these bits, not a "
    "portable one. Another storage driver produces a different identifier for the "
    "same build.",
    "The image was not pushed, so it has no registry repository digest.",
)


class ExternalBuildError(Exception):
    """A build could not be performed, so no resolution may be written."""


@dataclass
class BuildOutcome:
    """One image built once: what came out, and how long it took."""

    image_id: str
    duration_ms: float
    oci_manifest_digest: str | None = None


def build_argv(
    image: ExternalImage,
    *,
    repo_root: Path,
    no_cache: bool = False,
    ca_bundle: Path | None = None,
) -> list[str]:
    """The builder invocation, assembled in one pass.

    Kept pure and separately tested. The flags are not preferences: without
    ``SOURCE_DATE_EPOCH`` and ``rewrite-timestamp`` two builds of identical
    inputs on one machine produce two different image IDs, because BuildKit
    stamps each layer with the wall clock. That was measured here, and the same
    flag list is what ``external.identity`` fingerprints, so the two cannot drift
    apart without the fingerprint saying so.
    """
    source = image.source(repo_root)
    argv = ["docker", "build", "--provenance=false"]
    if no_cache:
        argv.append("--no-cache")
    if ca_bundle is not None:
        argv += ["--secret", f"id=ca,src={ca_bundle}"]
    argv += [
        "--output",
        f"type=image,name={source.runtime_tag},rewrite-timestamp=true,unpack=false",
        "--file",
        str(repo_root / source.dockerfile),
        str(repo_root / source.build_context),
    ]
    return argv


def build_one(
    image: ExternalImage,
    *,
    repo_root: Path,
    no_cache: bool = False,
    ca_bundle: Path | None = None,
    echo: bool = True,
) -> BuildOutcome:
    """Build one image and read back the identity this engine gave it."""
    source = image.source(repo_root)
    argv = build_argv(image, repo_root=repo_root, no_cache=no_cache, ca_bundle=ca_bundle)
    if echo:
        print(f"=== building {source.runtime_tag} from {source.build_context}", flush=True)

    started = time.perf_counter()
    completed = subprocess.run(
        argv,
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        env={**build_env()},
    )
    duration_ms = (time.perf_counter() - started) * 1000.0

    if completed.returncode != 0:
        raise ExternalBuildError(
            f"{image.key}: docker build exited {completed.returncode}\n{completed.stderr[-4000:]}"
        )
    if echo and completed.stderr:
        print(completed.stderr[-2000:], flush=True)

    # The build labelled its output with the runtime tag; this is the one place
    # the tag is used, and it is used to *learn* the ID rather than to run
    # anything. Everything downstream launches by ID.
    inspected = subprocess.run(
        ["docker", "image", "inspect", source.runtime_tag, "--format", "{{.Id}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    image_id = inspected.stdout.strip()
    if inspected.returncode != 0 or not is_local_image_id(image_id):
        raise ExternalBuildError(
            f"{image.key}: the build reported success but "
            f"`docker image inspect {source.runtime_tag}` returned {image_id!r}. "
            "Without an image ID there is nothing a run could verify."
        )

    exported = _EXPORTED_MANIFEST.search(completed.stderr)
    return BuildOutcome(
        image_id=image_id,
        duration_ms=duration_ms,
        oci_manifest_digest=exported.group(1) if exported else None,
    )


def build_env() -> dict[str, str]:
    import os

    env = dict(os.environ)
    # Fixes the timestamp BuildKit records. Part of the fingerprinted command.
    env["SOURCE_DATE_EPOCH"] = "0"
    return env


def _engine_facts(repo_root: Path) -> dict[str, str | None]:
    """Read the daemon's own description of itself, for the manifest header."""

    def _run(argv: list[str]) -> str | None:
        completed = subprocess.run(argv, cwd=repo_root, capture_output=True, text=True, check=False)
        value = completed.stdout.strip()
        return value or None if completed.returncode == 0 else None

    return {
        "storage_driver": _run(["docker", "info", "--format", "{{.Driver}}"]),
        "architecture": _run(["docker", "info", "--format", "{{.Architecture}}"]),
        "docker_engine_version": _run(["docker", "info", "--format", "{{.ServerVersion}}"]),
        "builder_version": _run(["docker", "version", "--format", "{{.Client.Version}}"]),
    }


def entry_for(
    image: ExternalImage,
    outcome: BuildOutcome,
    *,
    repo_root: Path,
    portable: PortableBuildIdentity,
) -> BuildResolutionEntry:
    from interop_lab.ci.baseline import target_fingerprint

    target = image.target(repo_root)
    source = image.source(repo_root)
    return BuildResolutionEntry(
        target_id=target.target_id,
        target_fingerprint=target_fingerprint(target),
        build_key=image.key,
        portable_identity=portable,
        portable_identity_fingerprint=portable.fingerprint,
        runtime_tag=source.runtime_tag,
        observed_local_image_id=outcome.image_id,
        oci_manifest_digest=outcome.oci_manifest_digest,
        build_duration_ms=outcome.duration_ms,
        build_status=BuildStatus.SUCCEEDED,
        limitations=list(ENTRY_LIMITATIONS),
    )


def build_all(
    *,
    repo_root: Path,
    no_cache: bool = False,
    ca_bundle: Path | None = None,
    harness_commit: str | None = None,
    echo: bool = True,
) -> BuildResolutionManifest:
    """Build every external image and return the resolution describing them.

    The portable identity is derived *before* the build, from the committed
    inputs, so the entry records what the builder was asked to build rather than
    a description reverse-engineered from what came out.
    """
    images = load_images(repo_root)
    if not images:
        raise ExternalBuildError("the external image manifest resolved to nothing")

    facts = _engine_facts(repo_root)
    manifest = BuildResolutionManifest(
        harness_commit=harness_commit,
        harness_version=HARNESS_VERSION,
        generated_at=datetime.now(UTC).isoformat(),
        builder="docker",
        builder_version=facts["builder_version"],
        docker_engine_version=facts["docker_engine_version"],
        storage_driver=facts["storage_driver"],
        architecture=facts["architecture"],
    )

    for image in images:
        portable = derive_portable_identity(image.target(repo_root), repo_root=repo_root)
        outcome = build_one(
            image, repo_root=repo_root, no_cache=no_cache, ca_bundle=ca_bundle, echo=echo
        )
        entry = entry_for(image, outcome, repo_root=repo_root, portable=portable)
        manifest.entries.append(entry)
        if echo:
            print(f"    image id:  {entry.observed_local_image_id}", flush=True)
            print(f"    inputs:    {entry.portable_identity_fingerprint}", flush=True)
            print(
                f"    oci:       {entry.oci_manifest_digest or 'not emitted by this builder'}",
                flush=True,
            )

    return manifest


def write_resolution(manifest: BuildResolutionManifest, path: Path) -> Path:
    """Write the resolution, creating its directory. Never a tracked target file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return path
