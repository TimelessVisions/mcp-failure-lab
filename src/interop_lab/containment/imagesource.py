"""Where a container image comes from, as two genuinely different things.

A single ``image`` plus ``image_digest`` pair had to stand for four claims that
are not interchangeable:

* a registry repository digest — the identity of bits a registry will serve;
* a builder-local image ID — the identity of bits *this machine* produced;
* a mutable tag — a name, which is not an identity at all;
* the provenance that makes either of the first two meaningful.

Collapsing them was measured to be wrong rather than merely untidy. A locally
built image has an ID and no registry digest until it is pushed; a pulled image
has a registry digest whose value is not its ID. Writing whichever one existed
into one field made the pin comparison either always succeed or always fail,
depending on which kind of image happened to be described — and the target
fingerprint still looked correct, because it hashed the field nobody could
interpret.

Worse, the local ID is not portable. A daemon using the containerd snapshotter
exports a manifest digest with ``rewrite-timestamp`` applied; one using the
classic storage driver writes a config ID and skips the rewrite. Two different
identifiers for the same bits, so a committed local ID is a value no second
machine can reproduce — while looking exactly like a reproducibility guarantee.

So the two sources are separate types with separate rules:

``RegistryImageSource``
    A repository digest is mandatory, the runtime reference is
    ``repository@digest``, and a tag is informational. Nothing has to be built.

``LocalBuildImageSource``
    Carries no digest and no image ID at all. It names the build, the committed
    build context and Dockerfile, and the portable inputs those are built from.
    The identity of the bits is resolved at runtime, from a build-resolution
    manifest produced by the builder that actually made them.

A boolean ``local_build: true`` would not do: the two cases require different
fields and obey different validation rules, and a flag cannot express either.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from interop_lab.containment.imageref import (
    DIGEST_PATTERN,
    ImageReferenceError,
    canonical_digest_reference,
    parse,
    require_pinned,
)


class ImageSourceKind(StrEnum):
    """How the bits that will run came to exist here."""

    #: Pulled from a registry, identified by a repository digest.
    REGISTRY = "registry"
    #: Built on this machine from committed inputs. Identified at runtime.
    LOCAL_BUILD = "local_build"


def _reject_repository_relative_escape(value: str, *, field: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(
            f"{field} {value!r} must be a repository-relative path without '..'; an "
            "absolute path in a target file stops resolving on any other machine"
        )
    return value


class RegistryImageSource(BaseModel):
    """Bits a registry will serve, pinned by repository digest.

    The digest is mandatory and is what resolves. ``display_tag`` exists so a
    human reading the file knows which release it names, and is never used to
    launch anything: a tag is a name that can be moved.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal[ImageSourceKind.REGISTRY] = ImageSourceKind.REGISTRY

    #: Registry and repository only. A tag or digest here would be a second
    #: place for the identity to live, and the two could disagree.
    repository: str
    #: sha256:… — the manifest digest the registry serves under this name.
    repository_digest: str
    #: Informational. Recorded for readers, never resolved.
    display_tag: str | None = None

    @model_validator(mode="after")
    def _repository_is_a_bare_name(self) -> RegistryImageSource:
        try:
            parsed = parse(self.repository)
        except ImageReferenceError as exc:
            raise ValueError(
                f"repository {self.repository!r} is not a valid reference: {exc}"
            ) from exc
        if parsed.tag is not None:
            raise ValueError(
                f"repository {self.repository!r} carries tag {parsed.tag!r}. The tag "
                "belongs in display_tag: keeping it here gives the identity two homes, "
                "and a reader cannot tell which one resolved the image."
            )
        if parsed.digest is not None:
            raise ValueError(
                f"repository {self.repository!r} carries a digest. Put it in "
                "repository_digest, which is the field the runtime is given."
            )
        return self

    @model_validator(mode="after")
    def _digest_is_a_digest(self) -> RegistryImageSource:
        if not DIGEST_PATTERN.match(self.repository_digest):
            raise ValueError(
                f"repository_digest {self.repository_digest!r} is not a sha256 digest. A "
                "registry source pinned by anything else is not pinned, because the name "
                "it uses can be moved."
            )
        return self

    @property
    def reference(self) -> str:
        """The digest-pinned reference actually handed to the runtime."""
        return canonical_digest_reference(self.repository, self.repository_digest)

    @property
    def build_key(self) -> None:
        """A registry source has no build. Present so callers need no isinstance."""
        return None


class LocalBuildImageSource(BaseModel):
    """Bits this machine built, identified at runtime rather than in the file.

    Nothing here is a content digest, deliberately. The fields name *what was
    built from* — all of it committed to this repository or pinned upstream —
    and the identity of what came out is resolved through a build-resolution
    manifest written by the builder that produced it.

    ``runtime_tag`` is a naming convenience for the build output and for a human
    reading a ``docker images`` listing. It is never the trusted identity and it
    is never what launches a container: a tag can be retargeted at another image
    between the build and the run, which is precisely the substitution the
    resolution manifest exists to detect.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal[ImageSourceKind.LOCAL_BUILD]

    #: Which entry of ``tools/external/images.yaml`` builds this image, and so
    #: which build-resolution entry may satisfy it.
    build_key: str
    #: The label the build applies to its output. Never resolved for execution.
    runtime_tag: str
    #: Repository-relative directory handed to the builder.
    build_context: str
    #: Repository-relative path to the Dockerfile, inside the build context.
    dockerfile: str
    #: Base images the Dockerfile pulls, pinned by registry digest. Portable:
    #: these are the one part of a local build that another machine can fetch.
    base_image_digests: list[str] = Field(default_factory=list)
    #: Toolchain versions that change the output but live outside the context —
    #: a compiler pinned by the base image, an interpreter version.
    toolchain_pins: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _runtime_tag_is_a_tag_and_nothing_more(self) -> LocalBuildImageSource:
        try:
            parsed = parse(self.runtime_tag)
        except ImageReferenceError as exc:
            raise ValueError(
                f"runtime_tag {self.runtime_tag!r} is not a valid reference: {exc}"
            ) from exc
        if parsed.digest is not None:
            raise ValueError(
                f"runtime_tag {self.runtime_tag!r} carries a digest. A local build has no "
                "identity until it is built, and committing one records this builder's "
                "answer as though every builder would produce it."
            )
        if parsed.tag is None:
            raise ValueError(
                f"runtime_tag {self.runtime_tag!r} has no tag. The build has to label its "
                "output with something, and an untagged name would collide across releases."
            )
        return self

    @model_validator(mode="after")
    def _build_inputs_are_repository_relative(self) -> LocalBuildImageSource:
        _reject_repository_relative_escape(self.build_context, field="build_context")
        _reject_repository_relative_escape(self.dockerfile, field="dockerfile")
        context = PurePosixPath(self.build_context)
        if not PurePosixPath(self.dockerfile).is_relative_to(context):
            raise ValueError(
                f"dockerfile {self.dockerfile!r} is outside build_context "
                f"{self.build_context!r}; the builder would never send it"
            )
        return self

    @model_validator(mode="after")
    def _base_images_are_pinned(self) -> LocalBuildImageSource:
        for reference in self.base_image_digests:
            try:
                require_pinned(reference)
            except ImageReferenceError as exc:
                raise ValueError(
                    f"base image {reference!r} is not pinned by digest ({exc}). An "
                    "unpinned base makes every hash below describe an input that can "
                    "change underneath it."
                ) from exc
        return self

    @property
    def image_name(self) -> str:
        """Repository of the build output, without its tag."""
        return parse(self.runtime_tag).name


#: The union every container spec carries. Discriminated on ``kind`` so pydantic
#: selects the model rather than trying each and reporting whichever error is
#: least helpful.
ImageSource = Annotated[
    RegistryImageSource | LocalBuildImageSource,
    Field(discriminator="kind"),
]


#: Keys that identified an image under the collapsed model. Detected explicitly
#: so a stale target file gets a migration message rather than a bare
#: "extra fields not permitted".
LEGACY_IMAGE_KEYS = ("image", "image_digest")


def reject_legacy_image_fields(values: object) -> None:
    """Raise a migration message for a container spec written under the old model.

    ``extra="forbid"`` already refuses these keys, but its message names the
    field and not the reason. A target file pinned this way was written when one
    digest field stood for a registry digest and a builder-local ID alike, and
    it cannot be reinterpreted automatically: which of the two it holds is
    exactly the thing that was never recorded.
    """
    if not isinstance(values, dict):
        return
    present = [key for key in LEGACY_IMAGE_KEYS if key in values]
    if not present:
        return
    raise ValueError(
        f"container declares {present}, which is the pre-v3 image model. That pair "
        "could hold either a registry repository digest or a builder-local image ID, "
        "and nothing recorded which — so it cannot be migrated automatically. Replace "
        "it with an explicit image_source:\n"
        "  image_source:\n"
        "    kind: registry\n"
        "    repository: <registry/repository>\n"
        "    repository_digest: sha256:…\n"
        "or, for an image built from this repository:\n"
        "  image_source:\n"
        "    kind: local_build\n"
        "    build_key: <key in tools/external/images.yaml>\n"
        "    runtime_tag: <name:tag>\n"
        "    build_context: targets/external/<dir>\n"
        "    dockerfile: targets/external/<dir>/Dockerfile"
    )
