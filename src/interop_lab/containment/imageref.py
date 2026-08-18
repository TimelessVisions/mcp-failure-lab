"""Parsing OCI/Docker image references.

Naive splitting is wrong in ways that matter here. ``image.split(":")[0]`` turns
``localhost:5000/team/server:1.2`` into ``localhost``, which would silently run a
*different image* than the one a target file pins — and the target fingerprint
would still look correct, because it hashed the string nobody parsed.

The grammar implemented is the one Docker and the OCI distribution spec use:

    reference := [registry "/"] repository [":" tag] ["@" digest]

A component is a registry when it contains ``.`` or ``:``, or is exactly
``localhost``. That rule is what separates ``myhost:5000/redis`` (registry with a
port) from ``library/redis`` (a two-segment repository path).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: A digest is an algorithm and a hex value. Only sha256 is accepted: it is what
#: every registry in use emits, and accepting an algorithm the harness cannot
#: verify would make the pin decorative.
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Tags are limited by the distribution spec to 128 characters of this alphabet.
TAG_PATTERN = re.compile(r"^[\w][\w.-]{0,127}$")

#: Lowercase path components separated by single separators, per the spec.
REPOSITORY_PATTERN = re.compile(
    r"^[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*)*$"
)

DEFAULT_REGISTRY = "docker.io"


class ImageReferenceError(ValueError):
    """A reference that cannot be parsed, or is not pinned when it must be."""


@dataclass(frozen=True)
class ImageReference:
    """A parsed reference, with each part available separately.

    Keeping the parts separate is the point: a target pins a digest, a human
    reads a tag, and the two are recorded independently so a moved tag is
    visible rather than absorbed.
    """

    registry: str | None
    repository: str
    tag: str | None
    digest: str | None

    @property
    def name(self) -> str:
        """Registry and repository, without tag or digest."""
        return f"{self.registry}/{self.repository}" if self.registry else self.repository

    @property
    def is_pinned(self) -> bool:
        """Whether this reference names specific bits rather than a movable name."""
        return self.digest is not None

    def pinned_reference(self, digest: str | None = None) -> str:
        """The digest-pinned form actually handed to the container runtime.

        The tag is dropped deliberately. ``name:tag@digest`` is legal and Docker
        accepts it, but it invites reading the tag as the identity when the
        digest is what resolves.
        """
        resolved = digest or self.digest
        if resolved is None:
            raise ImageReferenceError(
                f"{self.name!r} has no digest; a reference pinned by tag alone is not "
                "pinned, because a tag can be moved"
            )
        return f"{self.name}@{resolved}"

    def __str__(self) -> str:
        text = self.name
        if self.tag:
            text = f"{text}:{self.tag}"
        if self.digest:
            text = f"{text}@{self.digest}"
        return text


def _looks_like_registry(component: str) -> bool:
    """Whether a first path component is a registry host rather than a namespace.

    `.` catches domains, `:` catches a host:port, and localhost is the documented
    special case that has neither.
    """
    return "." in component or ":" in component or component == "localhost"


def parse(reference: str) -> ImageReference:
    """Parse a reference, or raise ImageReferenceError explaining what failed."""
    text = reference.strip()
    if not text:
        raise ImageReferenceError("empty image reference")

    digest: str | None = None
    if "@" in text:
        text, _, digest_part = text.partition("@")
        digest = digest_part
        if not DIGEST_PATTERN.match(digest):
            raise ImageReferenceError(
                f"{digest!r} is not a sha256 digest; expected sha256: followed by "
                "64 lowercase hex characters"
            )
        if not text:
            raise ImageReferenceError(f"{reference!r} has a digest but no image name")

    registry: str | None = None
    remainder = text
    head, slash, rest = text.partition("/")
    if slash and _looks_like_registry(head):
        registry = head
        remainder = rest

    tag: str | None = None
    # Only a colon in the final path component introduces a tag. A colon earlier
    # belongs to a registry port, which has already been split off above.
    last_slash = remainder.rfind("/")
    colon = remainder.rfind(":")
    if colon > last_slash:
        remainder, tag = remainder[:colon], remainder[colon + 1 :]
        if not TAG_PATTERN.match(tag):
            raise ImageReferenceError(f"{tag!r} is not a valid image tag")

    if not remainder:
        raise ImageReferenceError(f"{reference!r} has no repository component")
    if not REPOSITORY_PATTERN.match(remainder):
        raise ImageReferenceError(
            f"{remainder!r} is not a valid repository path; components must be "
            "lowercase alphanumeric with single separators"
        )

    return ImageReference(registry=registry, repository=remainder, tag=tag, digest=digest)


def require_pinned(reference: str) -> ImageReference:
    """Parse and refuse anything a registry could later resolve differently."""
    parsed = parse(reference)
    if not parsed.is_pinned:
        raise ImageReferenceError(
            f"{reference!r} is not pinned by digest. A tag is a name that can be "
            "moved, so a target pinned by tag alone is not reproducible."
        )
    return parsed


def canonical_digest_reference(image: str, digest: str) -> str:
    """Combine a target's ``image`` and ``image_digest`` into what Docker is given.

    This is the one function every caller needing "the pullable reference for this
    pinned target" should use, including the Python snippets embedded in GitHub
    Actions. It exists because the obvious inline version is wrong:

        image.split(":")[0] + "@" + digest

    On ``registry.example.com:5000/team/server:1.2.3`` that yields
    ``registry.example.com@sha256:…`` — a reference naming a *different* image, on
    a host that is not the pinned registry. The failure is silent: the string is
    well-formed, Docker will happily try to resolve it, and any fingerprint that
    hashed the original target file still looks correct.

    The digest argument wins over one carried in ``image``, and disagreement is
    refused rather than reconciled, because the two coming apart is precisely the
    condition a pin exists to detect.
    """
    parsed = parse(image)
    if parsed.digest is not None and parsed.digest != digest:
        raise ImageReferenceError(
            f"{image!r} carries digest {parsed.digest} but the pinned digest is "
            f"{digest}; they must agree"
        )
    if not DIGEST_PATTERN.match(digest):
        # `pinned_reference` accepts whatever it is handed, so a caller passing a
        # tag where a digest belongs would otherwise get back `name@2.0.0`: a
        # reference Docker rejects at pull time, long after the pin was recorded
        # as valid.
        raise ImageReferenceError(
            f"{digest!r} is not a sha256 digest; expected sha256: followed by "
            "64 lowercase hex characters"
        )
    return parsed.pinned_reference(digest)
