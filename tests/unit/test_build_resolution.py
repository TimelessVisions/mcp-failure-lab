"""Every way a build resolution can be wrong, and what the harness does about it.

The rule this file protects is one sentence: **the harness runs the image it
resolved, or it runs nothing.** Every check below is a specific way that could
stop being true, and each one has to produce a named error rather than a
degraded run — because the degraded run is the dangerous outcome. A skipped
test proves nothing; a PASS against the wrong image is worse than a failure.

These are pure. Nothing here needs Docker, because a rule enforced only where
a container runtime happens to be installed is not enforced.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    LOCAL_IMAGE_ID,
    OTHER_DIGEST,
    PROBE_DIGEST,
    PROBE_REPOSITORY,
    build_resolution,
    build_resolution_index,
    local_build_resolution,
    local_build_source,
    portable_identity,
    registry_resolution,
    registry_source,
)
from interop_lab.containment import docker
from interop_lab.containment.imagesource import (
    ImageSourceKind,
    LocalBuildImageSource,
    RegistryImageSource,
)
from interop_lab.containment.model import ContainerRuntimeFingerprint, ContainerSpec
from interop_lab.containment.resolution import (
    BUILD_RESOLUTION_SCHEMA_VERSION,
    BuildResolutionIndex,
    ImageResolutionError,
    ImageVerificationStatus,
    resolve_registry_source,
)
from interop_lab.runner.images import verify_started_container

TARGET_ID = "external.mcp_server_time"
FINGERPRINT = "0123456789abcdef"


def _index(**kwargs: Any) -> BuildResolutionIndex:
    return build_resolution_index(**kwargs)


def _resolve(index: BuildResolutionIndex, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "target_id": TARGET_ID,
        "target_fingerprint": FINGERPRINT,
        "portable_identity": portable_identity(),
    }
    kwargs.update(overrides)
    source = kwargs.pop("source", local_build_source())
    return index.resolve_local_build(source, **kwargs)


# ------------------------------------------------------- registry resolution


def test_a_registry_source_resolves_to_its_repository_digest() -> None:
    """Nothing has to be built, and nothing local is consulted."""
    resolved = resolve_registry_source(registry_source())

    assert resolved.source_kind is ImageSourceKind.REGISTRY
    assert resolved.launch_reference == f"{PROBE_REPOSITORY}@{PROBE_DIGEST}"
    assert resolved.repository_digest == PROBE_DIGEST
    assert resolved.expected_local_image_id is None
    assert resolved.verification is ImageVerificationStatus.REGISTRY_DIGEST_PINNED
    assert resolved.is_verified


def test_a_registry_reference_survives_a_registry_port() -> None:
    """Splitting a reference on its first colon collapses the port into the name."""
    source = RegistryImageSource(
        repository="registry.internal:5000/team/server", repository_digest=PROBE_DIGEST
    )
    assert source.reference == f"registry.internal:5000/team/server@{PROBE_DIGEST}"


def test_a_registry_source_without_a_digest_will_not_load() -> None:
    with pytest.raises(ValueError, match="not a sha256 digest"):
        RegistryImageSource(repository=PROBE_REPOSITORY, repository_digest="3.12-slim")


# ---------------------------------------------------- local-build resolution


def test_a_valid_entry_resolves_to_the_raw_image_id() -> None:
    resolved = _resolve(_index())

    assert resolved.source_kind is ImageSourceKind.LOCAL_BUILD
    assert resolved.launch_reference == LOCAL_IMAGE_ID
    assert resolved.expected_local_image_id == LOCAL_IMAGE_ID
    assert resolved.repository_digest is None, "a built image has no registry digest"
    assert resolved.verification is ImageVerificationStatus.RESOLVED
    assert not resolved.is_verified, "resolved is not the same claim as verified"


def test_a_missing_resolution_names_the_command_that_produces_one(tmp_path: Path) -> None:
    """The error has to be actionable: this is the common first-run failure."""
    with pytest.raises(ImageResolutionError, match="external-build"):
        BuildResolutionIndex.load(tmp_path / "absent.json")


def test_a_resolution_without_an_entry_for_this_target_is_refused() -> None:
    index = _index(target_id="external.someone_else")
    with pytest.raises(ImageResolutionError, match="no entry for target"):
        _resolve(index)


def test_a_stale_target_fingerprint_is_refused() -> None:
    """The target file changed after the build, so the image is not the measurement."""
    index = _index(target_fingerprint="ffffffffffffffff")
    with pytest.raises(ImageResolutionError, match="target fingerprint"):
        _resolve(index)


def test_changed_build_inputs_are_refused() -> None:
    """Caught even when the target file did not move.

    The target fingerprint covers the target *definition*. Editing a Dockerfile
    or adding a file to the build context changes what gets built without
    touching the definition, so the input fingerprint is a separate check.
    """
    index = _index()
    drifted = portable_identity(dockerfile_sha256="sha256:" + "9" * 64)
    with pytest.raises(ImageResolutionError, match="changed since the build"):
        _resolve(index, portable_identity=drifted)


def test_a_mismatched_build_key_is_refused() -> None:
    """Two targets whose fingerprints happened to agree are still two images.

    The entry is internally consistent — its build key and its identity's build
    key agree — and still describes a different image than the target asks for.
    That is the case the id and fingerprint checks alone would let through.
    """
    other = portable_identity(build_key="synthetic-typescript-http")
    index = _index(build_key="synthetic-typescript-http", portable=other)
    with pytest.raises(ImageResolutionError, match="different images"):
        _resolve(index, portable_identity=other)


def test_an_entry_whose_image_id_is_a_tag_will_not_even_load() -> None:
    """A tag here would let the run resolve whatever that name points at now."""
    manifest = build_resolution()
    payload = json.loads(manifest.model_dump_json())
    payload["entries"][0]["observed_local_image_id"] = "interop-lab/synthetic-python-stdio:1.2.3"

    from interop_lab.containment.resolution import BuildResolutionManifest

    with pytest.raises(ValueError, match="not a sha256"):
        BuildResolutionManifest.model_validate(payload)


def test_an_entry_whose_input_fingerprint_disagrees_with_its_identity_is_refused() -> None:
    """The manifest must be internally consistent before it is trusted at all."""
    manifest = build_resolution()
    payload = json.loads(manifest.model_dump_json())
    payload["entries"][0]["portable_identity_fingerprint"] = "0" * 64

    from interop_lab.containment.resolution import BuildResolutionManifest

    with pytest.raises(ValueError, match="does not match the recorded identity"):
        BuildResolutionManifest.model_validate(payload)


def test_a_harness_commit_policy_is_enforced_when_a_caller_demands_one() -> None:
    """Evidence is attributed to one commit or to none."""
    index = build_resolution_index(harness_commit="aaaa")
    index.require_harness_commit = "bbbb"
    with pytest.raises(ImageResolutionError, match="harness commit"):
        _resolve(index)


def test_an_unknown_schema_version_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    payload = json.loads(build_resolution().model_dump_json())
    payload["schema_version"] = "99"
    path = tmp_path / "build-resolution.json"
    path.write_text(json.dumps(payload))

    with pytest.raises(ImageResolutionError, match=BUILD_RESOLUTION_SCHEMA_VERSION):
        BuildResolutionIndex.load(path)


def test_a_manifest_with_two_entries_for_one_target_is_refused() -> None:
    """Which one would resolve is exactly the question nobody could answer."""
    payload = json.loads(build_resolution().model_dump_json())
    payload["entries"].append(payload["entries"][0])

    from interop_lab.containment.resolution import BuildResolutionManifest

    with pytest.raises(ValueError, match="duplicate target entries"):
        BuildResolutionManifest.model_validate(payload)


# ------------------------------------------------ the container that started


def test_a_started_container_running_the_resolved_image_is_verified() -> None:
    resolved = local_build_resolution(verification=ImageVerificationStatus.VERIFIED_PRESENT)
    verified = verify_started_container(resolved, LOCAL_IMAGE_ID)

    assert verified.verification is ImageVerificationStatus.VERIFIED_RUNNING
    assert verified.observed_container_image_id == LOCAL_IMAGE_ID


def test_a_started_container_running_another_image_is_a_mismatch() -> None:
    """Tag retargeting, a pruned cache, a stale manifest — all land here."""
    resolved = local_build_resolution(verification=ImageVerificationStatus.VERIFIED_PRESENT)
    verified = verify_started_container(resolved, OTHER_DIGEST)

    assert verified.verification is ImageVerificationStatus.MISMATCH
    assert not verified.is_verified
    assert verified.observed_container_image_id == OTHER_DIGEST


def test_a_container_whose_image_cannot_be_read_is_not_treated_as_verified() -> None:
    """Silence about which image ran is not evidence that the right one did."""
    resolved = local_build_resolution(verification=ImageVerificationStatus.VERIFIED_PRESENT)
    assert verify_started_container(resolved, None).verification is (
        ImageVerificationStatus.MISMATCH
    )


def test_a_retargeted_tag_does_not_change_what_was_launched() -> None:
    """The launch reference is the ID, so moving the tag cannot redirect a run.

    Stated as an assertion because it is the whole reason the tag is never used
    for execution: between the build recording it and the run using it, anything
    could have pointed that name at another image.
    """
    source = local_build_source(runtime_tag="interop-lab/synthetic-python-stdio:1.2.3")
    resolved = _resolve(_index(), source=source)

    assert resolved.runtime_tag == "interop-lab/synthetic-python-stdio:1.2.3"
    assert resolved.launch_reference == LOCAL_IMAGE_ID
    assert source.runtime_tag not in resolved.launch_reference


# -------------------------------------------------------- identity comparison


def _observed(**overrides: Any) -> ContainerRuntimeFingerprint:
    base: dict[str, Any] = {"observed_repo_digest": None, "observed_image_id": None}
    base.update(overrides)
    return ContainerRuntimeFingerprint.model_validate(base)


def test_a_registry_image_is_compared_against_its_repository_digest() -> None:
    resolved = registry_resolution()
    assert (
        docker.compare_image_identity(
            _observed(observed_repo_digest=PROBE_DIGEST, observed_image_id=LOCAL_IMAGE_ID),
            resolved,
            label="target",
        )
        == []
    )


def test_a_local_build_is_compared_against_its_image_id() -> None:
    """And never against a repository digest, which it can never equal.

    Under the containerd snapshotter a locally built, tagged image gets a
    RepoDigest whose value is its own config digest. A comparison that preferred
    that field would check the build against a value no build could produce.
    """
    resolved = local_build_resolution()
    assert (
        docker.compare_image_identity(
            _observed(observed_repo_digest=OTHER_DIGEST, observed_image_id=LOCAL_IMAGE_ID),
            resolved,
            label="target",
        )
        == []
    )

    problems = docker.compare_image_identity(
        _observed(observed_repo_digest=LOCAL_IMAGE_ID, observed_image_id=OTHER_DIGEST),
        resolved,
        label="target",
    )
    assert any("resolved image ID" in p for p in problems), problems


# ------------------------------------------------------------- the spec model


def test_a_container_spec_carries_exactly_one_kind_of_source() -> None:
    registry = ContainerSpec.model_validate({"image_source": registry_source().model_dump()})
    assert registry.source_kind is ImageSourceKind.REGISTRY
    assert registry.registry_source is not None
    assert registry.local_build_source is None

    built = ContainerSpec.model_validate({"image_source": local_build_source().model_dump()})
    assert built.source_kind is ImageSourceKind.LOCAL_BUILD
    assert built.local_build_source is not None
    assert built.registry_source is None


def test_an_unknown_field_in_an_image_source_is_rejected() -> None:
    """A typo that is silently ignored is a pin that silently is not applied."""
    with pytest.raises(ValueError, match="image_digest"):
        LocalBuildImageSource.model_validate(
            {**local_build_source().model_dump(), "image_digest": LOCAL_IMAGE_ID}
        )


def test_a_local_build_source_refuses_a_digest_in_its_runtime_tag() -> None:
    with pytest.raises(ValueError, match="carries a digest"):
        local_build_source(runtime_tag=f"interop-lab/x@{LOCAL_IMAGE_ID}")


def test_a_local_build_source_refuses_a_dockerfile_outside_its_context() -> None:
    with pytest.raises(ValueError, match="outside build_context"):
        local_build_source(dockerfile="targets/external/synthetic-typescript-http/Dockerfile")


def test_a_local_build_source_refuses_an_unpinned_base_image() -> None:
    with pytest.raises(ValueError, match="not pinned by digest"):
        local_build_source(base_image_digests=["mirror.gcr.io/library/python:3.12-slim"])
