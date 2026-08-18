"""Image reference parsing.

The motivating defect: `image.split(":")[0]` turns
`localhost:5000/team/server:1.2` into `localhost`, so the harness would run a
different image than the target pinned — while the fingerprint still looked
correct, because it hashed a string nobody parsed.
"""

from __future__ import annotations

import pytest

from interop_lab.containment.imageref import (
    ImageReference,
    ImageReferenceError,
    canonical_digest_reference,
    parse,
    require_pinned,
)

DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64


@pytest.mark.parametrize(
    ("reference", "registry", "repository", "tag", "digest"),
    [
        # Plain names, with and without an implied namespace.
        ("redis", None, "redis", None, None),
        ("library/redis", None, "library/redis", None, None),
        ("redis:7.2", None, "redis", "7.2", None),
        # Registry hosts, detected by a dot.
        ("docker.io/library/redis", "docker.io", "library/redis", None, None),
        (
            "mirror.gcr.io/library/python:3.12-slim",
            "mirror.gcr.io",
            "library/python",
            "3.12-slim",
            None,
        ),
        # A port is the case naive splitting gets wrong.
        ("localhost:5000/team/server:1.2", "localhost:5000", "team/server", "1.2", None),
        ("registry.example.com:8443/a/b/c:v1", "registry.example.com:8443", "a/b/c", "v1", None),
        # localhost with no dot and no port is the documented special case.
        ("localhost/server", "localhost", "server", None, None),
        # Deeply nested paths.
        ("ghcr.io/org/team/sub/project:tag", "ghcr.io", "org/team/sub/project", "tag", None),
        # Digests, alone and alongside a tag.
        (f"redis@{DIGEST}", None, "redis", None, DIGEST),
        (f"redis:7.2@{DIGEST}", None, "redis", "7.2", DIGEST),
        (
            f"localhost:5000/team/server:1.2@{DIGEST}",
            "localhost:5000",
            "team/server",
            "1.2",
            DIGEST,
        ),
    ],
)
def test_parses_every_reference_shape(
    reference: str, registry: str | None, repository: str, tag: str | None, digest: str | None
) -> None:
    parsed = parse(reference)
    assert parsed == ImageReference(
        registry=registry, repository=repository, tag=tag, digest=digest
    )


def test_a_port_is_not_mistaken_for_a_tag() -> None:
    """The exact defect this module exists to prevent."""
    parsed = parse("localhost:5000/team/server:1.2")
    assert parsed.registry == "localhost:5000"
    assert parsed.repository == "team/server"
    assert parsed.tag == "1.2"
    assert parsed.name == "localhost:5000/team/server"
    # The naive version produced "localhost", which is a different image.
    assert parsed.name != "localhost"


def test_a_namespace_is_not_mistaken_for_a_registry() -> None:
    """`library/redis` has no registry; `docker.io/redis` does."""
    assert parse("library/redis").registry is None
    assert parse("docker.io/redis").registry == "docker.io"


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "   ",
        f"@{DIGEST}",
        "redis@sha256:tooshort",
        "redis@md5:" + "a" * 32,
        "redis@" + "a" * 64,
        "REDIS",
        "redis:",
        "redis:" + "x" * 200,
    ],
)
def test_malformed_references_are_rejected(reference: str) -> None:
    with pytest.raises(ImageReferenceError):
        parse(reference)


def test_uppercase_repository_is_rejected() -> None:
    """Registries reject these, so accepting them would defer a confusing error."""
    with pytest.raises(ImageReferenceError, match="not a valid repository"):
        parse("docker.io/Library/Redis")


# ------------------------------------------------------------------- pinning


def test_a_tag_only_reference_is_not_pinned() -> None:
    assert parse("redis:7.2").is_pinned is False
    with pytest.raises(ImageReferenceError, match="not pinned by digest"):
        require_pinned("redis:7.2")


def test_a_digest_reference_is_pinned() -> None:
    assert require_pinned(f"redis@{DIGEST}").is_pinned


def test_the_pinned_form_drops_the_tag() -> None:
    """`name:tag@digest` invites reading the tag as the identity; the digest resolves."""
    parsed = parse(f"mirror.gcr.io/library/python:3.12-slim@{DIGEST}")
    assert parsed.pinned_reference() == f"mirror.gcr.io/library/python@{DIGEST}"


def test_the_pinned_form_can_take_an_externally_resolved_digest() -> None:
    """A locally built image has a digest the reference string does not carry."""
    parsed = parse("localhost:5000/team/server:1.2")
    assert parsed.pinned_reference(OTHER_DIGEST) == f"localhost:5000/team/server@{OTHER_DIGEST}"


def test_pinning_an_unpinned_reference_without_a_digest_raises() -> None:
    with pytest.raises(ImageReferenceError, match="no digest"):
        parse("redis:7.2").pinned_reference()


def test_round_trips_through_str() -> None:
    for reference in (
        "redis",
        "redis:7.2",
        "localhost:5000/team/server:1.2",
        f"ghcr.io/org/project:v1@{DIGEST}",
    ):
        assert str(parse(reference)) == reference


# ------------------------------------------------- canonical_digest_reference
#
# The single entry point every caller uses to turn a target's `image` plus
# `image_digest` into the reference handed to the container runtime — including
# the Python snippets embedded in the GitHub Actions workflows, which previously
# reimplemented it inline and got it wrong.


@pytest.mark.parametrize(
    ("image", "expected_name"),
    [
        # Shapes where the naive `split(":")[0]` happens to agree. Included so a
        # regression cannot hide behind the easy cases.
        ("python", "python"),
        ("python:3.12", "python"),
        ("nested/repository/name", "nested/repository/name"),
        ("mirror.gcr.io/library/python:3.12-slim", "mirror.gcr.io/library/python"),
        ("ghcr.io/team/server:2.0.0", "ghcr.io/team/server"),
        # Shapes where the naive version silently names a different image.
        (
            "registry.example.com:5000/team/server:1.2.3",
            "registry.example.com:5000/team/server",
        ),
        (
            f"registry.example.com:5000/team/server@{DIGEST}",
            "registry.example.com:5000/team/server",
        ),
        # IPv6 literal hosts, which carry colons in the host itself.
        ("[::1]:5000/team/server:1.0", "[::1]:5000/team/server"),
        ("[2001:db8::1]:5000/team/server", "[2001:db8::1]:5000/team/server"),
    ],
)
def test_canonical_digest_reference_keeps_the_whole_name(image: str, expected_name: str) -> None:
    assert canonical_digest_reference(image, DIGEST) == f"{expected_name}@{DIGEST}"


def test_canonical_digest_reference_survives_a_registry_port() -> None:
    """The bug this checkpoint exists to keep out, stated as an assertion.

    `image.split(":")[0] + "@" + digest` turns a pinned server on a private
    registry into a reference for the *registry host itself*. It is well-formed,
    so nothing downstream complains — it simply resolves different bits.
    """
    image = "registry.example.com:5000/team/server:1.2.3"
    naive = image.split(":")[0] + "@" + DIGEST

    assert canonical_digest_reference(image, DIGEST) != naive
    assert naive == f"registry.example.com@{DIGEST}"
    assert canonical_digest_reference(image, DIGEST) == (
        f"registry.example.com:5000/team/server@{DIGEST}"
    )


def test_canonical_digest_reference_drops_the_tag() -> None:
    """A digest and a tag are two identities; only one of them resolves."""
    assert canonical_digest_reference("ghcr.io/team/server:2.0.0", DIGEST) == (
        f"ghcr.io/team/server@{DIGEST}"
    )


def test_canonical_digest_reference_refuses_a_disagreeing_digest() -> None:
    """Two digests coming apart is what a pin exists to detect, not to reconcile."""
    with pytest.raises(ImageReferenceError, match="they must agree"):
        canonical_digest_reference(f"ghcr.io/team/server@{DIGEST}", OTHER_DIGEST)


def test_canonical_digest_reference_accepts_an_agreeing_digest() -> None:
    assert canonical_digest_reference(f"ghcr.io/team/server@{DIGEST}", DIGEST) == (
        f"ghcr.io/team/server@{DIGEST}"
    )


def test_canonical_digest_reference_rejects_a_non_digest() -> None:
    with pytest.raises(ImageReferenceError):
        canonical_digest_reference("ghcr.io/team/server:2.0.0", "2.0.0")
