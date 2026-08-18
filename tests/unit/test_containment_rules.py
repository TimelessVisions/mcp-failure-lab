"""Containment rules that must hold without a container runtime present.

These are the checks that decide what isolation is *requested* and what a target
file is allowed to say. They run everywhere, including on a machine with no
Docker, because a rule enforced only where Docker happens to be installed is not
enforced.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from conftest import LOCAL_IMAGE_ID, local_build_resolution, registry_resolution
from interop_lab.containment import docker
from interop_lab.containment.model import (
    CONTAINMENT_LIMITS,
    UNUSABLE_CONTAINMENT,
    ContainerRuntimeFingerprint,
    ContainerSpec,
    ContainmentOutcome,
    ContainmentRecord,
    ExecutionMode,
    NetworkMode,
    Provenance,
)
from interop_lab.models.core import Classification, TargetDefinition
from interop_lab.profiles.registry import Role, Transport

IMAGE = "mirror.gcr.io/library/python"
DIGEST = "sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36"
#: A second, deliberately different digest. Used to prove the harness notices
#: when the image that ran is not the image that was pinned.
OTHER_DIGEST = "sha256:" + "b" * 64


def make_spec(**overrides: Any) -> ContainerSpec:
    base: dict[str, Any] = {
        "image_source": {
            "kind": "registry",
            "repository": IMAGE,
            "repository_digest": DIGEST,
            "display_tag": "3.12-slim",
        }
    }
    base.update(overrides)
    return ContainerSpec.model_validate(base)


def make_target(**overrides: Any) -> TargetDefinition:
    base: dict[str, Any] = {
        "target_id": "t",
        "role": Role.FIXTURE,
        "implementation": "fixture",
        "language": "python",
        "version": "1.0",
        "launch_command": ["${PYTHON}", "fixtures/x.py"],
        "transport": Transport.STDIO,
        "protocol_profile": "mcp-2026-07-28",
    }
    base.update(overrides)
    return TargetDefinition.model_validate(base)


# ------------------------------------------------------------------ the spec


def test_an_image_must_be_pinned_by_digest() -> None:
    """A tag is a name that can be moved, so a tag is not a pin."""
    with pytest.raises(ValidationError, match="not a sha256 digest"):
        make_spec(
            image_source={
                "kind": "registry",
                "repository": IMAGE,
                "repository_digest": "latest",
            }
        )


def test_the_digest_is_what_is_passed_to_the_runtime() -> None:
    source = make_spec().registry_source
    assert source is not None
    assert source.reference == f"mirror.gcr.io/library/python@{DIGEST}"


def test_a_pre_v3_container_spec_is_refused_with_a_migration_message() -> None:
    """The old pair could hold either kind of digest, and never said which."""
    with pytest.raises(ValidationError, match="pre-v3 image model"):
        ContainerSpec.model_validate({"image": IMAGE, "image_digest": DIGEST})


def test_a_registry_source_will_not_hide_the_tag_inside_the_repository() -> None:
    """Two homes for the identity is one home too many."""
    with pytest.raises(ValidationError, match="belongs in display_tag"):
        make_spec(
            image_source={
                "kind": "registry",
                "repository": f"{IMAGE}:3.12-slim",
                "repository_digest": DIGEST,
            }
        )


def test_defaults_are_the_strict_end_of_every_axis() -> None:
    spec = make_spec()
    assert spec.network is NetworkMode.NONE
    assert spec.read_only_root is True
    assert spec.drop_all_capabilities is True
    assert spec.no_new_privileges is True
    assert spec.user == "65534:65534"
    assert spec.repo_mounts == []


def test_http_containment_requires_a_publishable_port() -> None:
    with pytest.raises(ValidationError, match="requires container_port"):
        make_spec(network=NetworkMode.PUBLISHED_LOOPBACK)


def test_an_unreachable_port_is_rejected_rather_than_ignored() -> None:
    with pytest.raises(ValidationError, match="nothing can reach it"):
        make_spec(network=NetworkMode.NONE, container_port=8000)


@pytest.mark.parametrize("mount", ["/etc", "../../secrets", "fixtures/../.."])
def test_mounts_cannot_escape_the_repository(mount: str) -> None:
    with pytest.raises(ValidationError, match="repository-relative"):
        make_spec(repo_mounts=[mount])


# ------------------------------------------------------- target-level rules


def test_external_code_cannot_run_on_the_host() -> None:
    """The rule that carries external testing: it fails at load, not at run."""
    with pytest.raises(ValidationError, match="container or not at all"):
        make_target(provenance=Provenance.EXTERNAL, execution_mode=ExecutionMode.HOST)


def test_external_code_defaults_are_not_quietly_permissive() -> None:
    """Declaring external provenance without saying anything else must fail."""
    with pytest.raises(ValidationError):
        make_target(provenance=Provenance.EXTERNAL)


def test_container_mode_without_a_spec_is_rejected() -> None:
    with pytest.raises(ValidationError, match="nothing to enforce"):
        make_target(execution_mode=ExecutionMode.CONTAINER)


def test_a_container_spec_that_is_not_enforced_is_rejected() -> None:
    """A spec on a host target would read as containment that does not exist."""
    with pytest.raises(ValidationError, match="reads as containment"):
        make_target(execution_mode=ExecutionMode.HOST, container=make_spec().model_dump())


def test_stdio_containment_must_have_no_network() -> None:
    with pytest.raises(ValidationError, match="stdio needs no network"):
        make_target(
            execution_mode=ExecutionMode.CONTAINER,
            container=make_spec(
                network=NetworkMode.PUBLISHED_LOOPBACK, container_port=8000
            ).model_dump(),
        )


def test_http_containment_must_be_reachable_somehow() -> None:
    """`network=none` leaves nothing able to open a socket to an HTTP target."""
    with pytest.raises(ValidationError, match="nothing could open a socket"):
        make_target(
            transport=Transport.STREAMABLE_HTTP,
            execution_mode=ExecutionMode.CONTAINER,
            container=make_spec().model_dump(),
        )


def test_http_containment_accepts_the_isolated_sidecar_mode() -> None:
    """The preferred mode: no published port, reachable only from the probe."""
    target = make_target(
        transport=Transport.STREAMABLE_HTTP,
        execution_mode=ExecutionMode.CONTAINER,
        container=make_spec(network=NetworkMode.ISOLATED_SIDECAR, container_port=8000).model_dump(),
    )
    assert target.container is not None
    assert target.container.network is NetworkMode.ISOLATED_SIDECAR


def test_the_isolated_sidecar_mode_still_needs_a_port() -> None:
    """The probe has to know where to connect."""
    with pytest.raises(ValidationError, match="requires container_port"):
        make_spec(network=NetworkMode.ISOLATED_SIDECAR)


def test_the_committed_contained_target_loads() -> None:
    import yaml

    payload = yaml.safe_load(
        Path("targets/fixtures/reference-contained.yaml").read_text(encoding="utf-8")
    )
    target = TargetDefinition.model_validate(payload)
    assert target.execution_mode is ExecutionMode.CONTAINER
    assert target.container is not None
    assert target.container.network is NetworkMode.NONE


# --------------------------------------------------------------- the argv


def _argv(spec: ContainerSpec, **kwargs: Any) -> list[str]:
    defaults: dict[str, Any] = {
        # Passed rather than read off the spec: for a local build the spec
        # deliberately does not know it, and a helper that reached for a field
        # here would be the one place a tag could get onto the command line.
        "image_reference": registry_resolution().launch_reference,
        "container_name": "c",
        "command": ["${PYTHON}", "fixtures/reference_server.py"],
        "env": {},
    }
    defaults.update(kwargs)
    return docker.build_run_argv(spec, **defaults)


def test_a_local_build_launches_by_raw_image_id_never_by_tag() -> None:
    """A tag can be retargeted between the build and the run."""
    resolved = local_build_resolution()
    argv = _argv(make_spec(), image_reference=resolved.launch_reference)
    assert LOCAL_IMAGE_ID in argv
    assert not any("interop-lab/mcp-server-time" in part for part in argv), argv


def test_every_requested_control_appears_in_the_argv() -> None:
    argv = _argv(make_spec(memory_mb=256, cpus=0.5, pids_limit=64))
    joined = " ".join(argv)
    assert "--network none" in joined
    assert "--memory 256m" in joined
    assert "--memory-swap 256m" in joined, "a memory limit without a swap limit is not a limit"
    assert "--cpus 0.5" in joined
    assert "--pids-limit 64" in joined
    assert "--read-only" in joined
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert "--user 65534:65534" in joined
    assert f"mirror.gcr.io/library/python@{DIGEST}" in joined


def test_a_published_port_is_bound_to_loopback_only() -> None:
    """Publishing to 0.0.0.0 would expose a hostile target to the local network."""
    argv = _argv(
        make_spec(network=NetworkMode.PUBLISHED_LOOPBACK, container_port=8000),
        host_port=41234,
    )
    assert "--publish" in argv
    published = argv[argv.index("--publish") + 1]
    assert published == "127.0.0.1:41234:8000"
    assert not published.startswith("0.0.0.0")


def test_repo_mounts_are_read_only() -> None:
    argv = _argv(make_spec(repo_mounts=["fixtures"]), repo_root="/repo")
    mount = argv[argv.index("--volume") + 1]
    assert mount == "/repo/fixtures:/opt/interop/fixtures:ro"
    assert mount.endswith(":ro")


def test_the_python_token_resolves_inside_the_image_not_on_the_host() -> None:
    """`${PYTHON}` means the harness's interpreter — a host path, meaningless here."""
    spec = make_spec(repo_mounts=["fixtures"])
    command = docker.containerised_command(
        spec, ["${PYTHON}", "fixtures/reference_server.py", "--flag"]
    )
    assert command == ["python", "/opt/interop/fixtures/reference_server.py", "--flag"]


def test_the_container_does_not_inherit_a_host_interpreter_path() -> None:
    argv = _argv(make_spec(repo_mounts=["fixtures"]), repo_root="/repo")
    assert not any(part.startswith("/usr/") and "python" in part for part in argv[-3:])


# ------------------------------------------------------- observed vs requested


def _observed(**overrides: Any) -> ContainerRuntimeFingerprint:
    base: dict[str, Any] = {
        "memory_limit_bytes": 512 * 1024 * 1024,
        "nano_cpus": 1_000_000_000,
        "pids_limit": 128,
        "read_only_root": True,
        "user": "65534:65534",
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "network_mode": "none",
        "observed_repo_digest": DIGEST,
    }
    base.update(overrides)
    return ContainerRuntimeFingerprint.model_validate(base)


def test_a_container_matching_the_request_reports_no_weakness() -> None:
    assert docker.compare(make_spec(), _observed(), resolved=registry_resolution()) == []


# --------------------------------------------------- image identity, verified


def test_a_different_image_is_the_first_thing_reported() -> None:
    """A perfectly confined wrong image is not the measurement anyone approved."""
    weaknesses = docker.compare(
        make_spec(),
        _observed(observed_repo_digest=OTHER_DIGEST),
        resolved=registry_resolution(),
    )
    assert weaknesses, "running a different image must be flagged"
    assert "repository digest requested" in weaknesses[0], weaknesses


def test_a_locally_built_image_is_verified_by_image_id() -> None:
    """No registry digest exists for a local build, so the ID is what is checked."""
    resolved = local_build_resolution(image_id=LOCAL_IMAGE_ID)
    spec = make_spec()

    matching = _observed(observed_repo_digest=None, observed_image_id=LOCAL_IMAGE_ID)
    assert docker.compare(spec, matching, resolved=resolved) == []

    mismatched = _observed(observed_repo_digest=None, observed_image_id=OTHER_DIGEST)
    weaknesses = docker.compare(spec, mismatched, resolved=resolved)
    assert any("resolved image ID" in w for w in weaknesses), weaknesses


def test_a_local_build_is_never_checked_against_a_repository_digest() -> None:
    """The two identifiers describe different things and can never be equal.

    A locally built image under the containerd snapshotter can carry a
    self-referential RepoDigest. Comparing that against the resolved image ID —
    or, worse, falling back to it when the ID is missing — is how a local build
    got checked against a value it could never match.
    """
    resolved = local_build_resolution(image_id=LOCAL_IMAGE_ID)
    observed = _observed(observed_repo_digest=OTHER_DIGEST, observed_image_id=LOCAL_IMAGE_ID)
    assert docker.compare(make_spec(), observed, resolved=resolved) == []


def test_a_registry_image_is_verified_when_its_id_equals_its_own_manifest_digest() -> None:
    """The containerd snapshotter case, which used to fail a correctly pinned image.

    There an image ID *is* the manifest digest, so a pulled image's ID equals its
    own repository digest — and the heuristic that reads "digest differs from ID"
    as "pulled" concludes the opposite. It discarded the digest as a mere
    restatement of the ID, reported that no repository digest could be read back,
    and turned every contained HTTP run on such a daemon into a HARNESS_ERROR
    against an image that was pinned exactly as requested.

    Verification now matches the pinned digest against the digests the engine
    reports, which is the claim being made rather than a reading of it.
    """
    observed = _observed(
        # As `docker image inspect` reports it under the containerd snapshotter.
        observed_repo_digests=[f"mirror.gcr.io/library/python@{DIGEST}"],
        # Nulled by the "looks locally built" reading, exactly as in production.
        observed_repo_digest=None,
        observed_image_id=DIGEST,
    )

    assert docker.compare(make_spec(), observed, resolved=registry_resolution()) == []


def test_a_registry_image_with_only_other_digests_is_still_a_weakness() -> None:
    """Reading the list must not become "accept whatever the engine reports"."""
    observed = _observed(
        observed_repo_digests=[f"mirror.gcr.io/library/python@{OTHER_DIGEST}"],
        observed_repo_digest=None,
        observed_image_id=OTHER_DIGEST,
    )

    weaknesses = docker.compare(make_spec(), observed, resolved=registry_resolution())

    assert any("repository digest requested" in w for w in weaknesses), weaknesses


def test_an_unreadable_image_identity_is_a_weakness_not_a_pass() -> None:
    """Silence about which image ran is not evidence that the right one did."""
    weaknesses = docker.compare(
        make_spec(),
        _observed(observed_repo_digest=None, observed_image_id=None),
        resolved=registry_resolution(),
    )
    assert any("no target repository digest" in w for w in weaknesses), weaknesses

    local = docker.compare(
        make_spec(),
        _observed(observed_repo_digest=None, observed_image_id=None),
        resolved=local_build_resolution(),
    )
    assert any("no target image ID" in w for w in local), local


def test_the_four_identity_fields_are_distinct() -> None:
    """Conflating them is how a 'verified' image turns out to be another one."""
    fingerprint = ContainerRuntimeFingerprint(
        requested_repo_digest=DIGEST,
        observed_repo_digest=OTHER_DIGEST,
        observed_image_id="sha256:" + "c" * 64,
        build_identity="locally-built id=sha256:ccc created=2026-08-13",
    )
    values = {
        fingerprint.requested_repo_digest,
        fingerprint.observed_repo_digest,
        fingerprint.observed_image_id,
        fingerprint.build_identity,
    }
    assert len(values) == 4, "each field must be able to hold a different fact"


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"pids_limit": 0}, "pids limit"),
        ({"memory_limit_bytes": 0}, "memory limit"),
        ({"nano_cpus": 0}, "cpu limit"),
        ({"read_only_root": False}, "read-only root"),
        ({"user": "0:0"}, "user requested"),
        ({"cap_drop": []}, "cap-drop ALL"),
        ({"security_opt": []}, "no-new-privileges"),
        ({"network_mode": "bridge"}, "network none requested"),
    ],
)
def test_each_missing_control_is_named(override: dict[str, Any], expected: str) -> None:
    """Docker accepts flags it cannot honour and reports success either way."""
    weaknesses = docker.compare(make_spec(), _observed(**override), resolved=registry_resolution())
    assert any(expected in w for w in weaknesses), weaknesses


def test_a_weakened_container_makes_the_run_unusable_as_evidence() -> None:
    record = ContainmentRecord(
        mode=ExecutionMode.CONTAINER,
        provenance=Provenance.EXTERNAL,
        outcome=ContainmentOutcome.CONTAINMENT_WEAKER_THAN_REQUESTED,
        requested=make_spec(),
        weaknesses=["pids limit requested 64, container has 0"],
    )
    assert not record.is_usable_evidence


@pytest.mark.parametrize("outcome", sorted(UNUSABLE_CONTAINMENT))
def test_no_unusable_outcome_can_be_read_as_a_result(outcome: ContainmentOutcome) -> None:
    record = ContainmentRecord(
        mode=ExecutionMode.CONTAINER, provenance=Provenance.EXTERNAL, outcome=outcome
    )
    assert not record.is_usable_evidence


def test_a_contained_run_still_states_what_it_does_not_protect() -> None:
    limits = docker.stated_limits(make_spec())
    assert limits
    assert any("not a security boundary" in limit for limit in limits)
    # The host-reachability caveat belongs to published_loopback only.
    assert not any("published_loopback" in limit for limit in limits)

    http_limits = docker.stated_limits(
        make_spec(network=NetworkMode.PUBLISHED_LOOPBACK, container_port=8000)
    )
    assert any("published_loopback" in limit for limit in http_limits)
    assert len(http_limits) == len(CONTAINMENT_LIMITS)


# ---------------------------------------------------------- no host fallback


def test_docker_unavailable_raises_rather_than_falling_back(monkeypatch: Any) -> None:
    """The single most important property of this module.

    Falling back to the host would produce a run recorded as contained that was
    not, which is worse than no run at all.
    """

    async def unavailable() -> tuple[bool, str, None]:
        return False, "docker is not on PATH", None

    monkeypatch.setattr(docker, "probe", unavailable)
    with pytest.raises(docker.ContainmentUnavailableError, match="does not fall back"):
        asyncio.run(docker.ensure_available())


def test_the_unavailable_message_says_which_check_failed(monkeypatch: Any) -> None:
    async def unavailable() -> tuple[bool, str, None]:
        return False, "the docker client is installed but no daemon is running", None

    monkeypatch.setattr(docker, "probe", unavailable)
    with pytest.raises(docker.ContainmentUnavailableError, match="no daemon is running"):
        asyncio.run(docker.ensure_available())


def test_containment_unavailable_is_never_a_pass() -> None:
    """It is an environment failure, and environment failures are HARNESS_ERROR."""
    record = ContainmentRecord(
        mode=ExecutionMode.CONTAINER,
        provenance=Provenance.EXTERNAL,
        outcome=ContainmentOutcome.CONTAINMENT_UNAVAILABLE,
        unavailable_reason="docker is not on PATH",
    )
    assert not record.is_usable_evidence
    assert Classification.PASS not in {Classification.HARNESS_ERROR}


def test_the_host_http_session_still_refuses_a_contained_target() -> None:
    """The last line of defence behind the strategy routing.

    Contained HTTP now has a real path — the probe sidecar — and
    `select_strategy` sends every contained HTTP target there. This session is
    the *host* path, and reaching it with a contained target means the routing
    was bypassed: by a caller constructing the session directly, or by a future
    branch that forgot the rule.

    It stays a refusal rather than a fallback. Running the target on the host
    anyway would write `contained` into an artifact describing an ordinary local
    process, which is the false claim the whole containment layer exists to
    prevent.
    """
    from interop_lab.transports.http.session import StreamableHttpSession

    target = make_target(
        transport=Transport.STREAMABLE_HTTP,
        execution_mode=ExecutionMode.CONTAINER,
        container=make_spec(
            network=NetworkMode.PUBLISHED_LOOPBACK, container_port=8000
        ).model_dump(),
    )
    session = StreamableHttpSession(target, run_id="r")
    with pytest.raises(docker.ContainmentUnavailableError, match="probe sidecar"):
        asyncio.run(session.start())

    record = session.containment
    assert record is not None
    assert record.outcome is ContainmentOutcome.CONTAINMENT_FAILED
    assert not record.is_usable_evidence
    assert session.process is None, "no process may be launched when containment is refused"


def test_host_execution_is_recorded_as_uncontained_not_as_contained() -> None:
    record = ContainmentRecord(
        mode=ExecutionMode.HOST,
        provenance=Provenance.LOCAL_FIXTURE,
        outcome=ContainmentOutcome.NOT_CONTAINED_BY_DESIGN,
        stated_limits=["nothing isolated it"],
    )
    assert record.is_usable_evidence, "a local fixture on the host is still valid evidence"
    assert record.observed is None
    assert record.stated_limits
