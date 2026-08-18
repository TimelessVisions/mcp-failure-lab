"""The external image set, read once from structured data.

Both the build command and the external-matrix job need the same facts about
each image, and both used to reconstruct them from a colon-delimited string
split in bash. That is the same defect this project removed from image
references a checkpoint ago: it works until one of the fields contains a colon,
and then it silently produces wrong values rather than failing.

The manifest no longer restates the build context, the image name or the tag.
Those live on the target's ``container.image_source``, which is the thing the
harness actually launches from, so reading them from anywhere else means two
files can disagree about what is being built — and the copy nobody builds from
is the copy that goes stale. Here the manifest says only which targets are
external images, and everything else is read through the typed target model.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from interop_lab.containment.imagesource import LocalBuildImageSource
from interop_lab.models.core import TargetDefinition

#: Repository-relative, resolved against a repository root the caller passes —
#: never against the current working directory, which differs between a shell
#: script, a workflow step and a test.
#:
#: Under `tools/`, not `targets/`: `targets/external/` holds target definitions
#: and is globbed as such, so a manifest sitting there was collected as a target
#: with no `transport` and broke every test that enumerates them.
MANIFEST_PATH = Path("tools/external/images.yaml")


class ExternalImage(BaseModel):
    """One external target image, identified by the target file that defines it."""

    model_config = ConfigDict(extra="forbid")

    key: str
    target_file: str

    def target(self, repo_root: Path) -> TargetDefinition:
        """The typed target definition, read fresh. Validated, never hand-parsed.

        Deliberately uncached. A process-level cache keyed on the path would be
        one more piece of hidden global state in exactly the area this
        checkpoint spent its effort removing it from, and it would buy almost
        nothing: three small YAML files, read a handful of times per command.
        """
        return TargetDefinition.model_validate(
            yaml.safe_load((repo_root / self.target_file).read_text())
        )

    def source(self, repo_root: Path) -> LocalBuildImageSource:
        """The local-build source this image is built from.

        Raises for a target that is not locally built. An external target that
        somehow became a registry source would have nothing to build, and
        silently skipping it would leave the matrix a target short while
        reporting success.
        """
        target = self.target(repo_root)
        spec = target.container
        source = spec.local_build_source if spec is not None else None
        if source is None:
            raise ValueError(
                f"{self.target_file} does not declare a local_build image source, so "
                f"there is nothing for the external build to produce for {self.key!r}"
            )
        if source.build_key != self.key:
            raise ValueError(
                f"{self.target_file} declares build_key {source.build_key!r}, but the "
                f"image manifest lists it as {self.key!r}. A build resolution keyed by "
                "one and looked up by the other would never match."
            )
        return source

    def build_context(self, repo_root: Path) -> str:
        return self.source(repo_root).build_context

    def dockerfile(self, repo_root: Path) -> str:
        return self.source(repo_root).dockerfile

    def runtime_tag(self, repo_root: Path) -> str:
        """The label the build applies. Only ever a build output name."""
        return self.source(repo_root).runtime_tag

    def image_name(self, repo_root: Path) -> str:
        return self.source(repo_root).image_name


def load_images(repo_root: Path | None = None) -> list[ExternalImage]:
    """Every external image, in a fixed order."""
    root = repo_root or Path.cwd()
    raw = yaml.safe_load((root / MANIFEST_PATH).read_text())
    entries = (raw or {}).get("images")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{MANIFEST_PATH} declares no images")
    images = [ExternalImage.model_validate(entry) for entry in entries]

    keys = [image.key for image in images]
    if len(set(keys)) != len(keys):
        raise ValueError(f"{MANIFEST_PATH} has duplicate keys: {keys}")
    return images


def get_image(key: str, repo_root: Path | None = None) -> ExternalImage:
    for image in load_images(repo_root):
        if image.key == key:
            return image
    raise KeyError(f"unknown external image {key!r}")
