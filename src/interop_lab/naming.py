"""Centralised naming.

Every user-visible string that carries the product name is defined here so a
rename does not touch business logic. That is why the rename to the public name
was a change to this file and its callers rather than a sweep over the tree.

The Python import namespace deliberately did *not* change. It is an internal
identifier, not a user-visible name: renaming it would rewrite every import in
the project and every artifact path that records a module, for no reader's
benefit. The distribution and the command carry the public name; the package
does not.
"""

from __future__ import annotations

from typing import Final

#: Public product name.
PRODUCT_NAME: Final = "MCP Failure Lab"

#: Installed CLI command, matching the distribution name.
CLI_COMMAND: Final = "mcp-failure-lab"

#: Python import namespace. Internal, and deliberately not renamed — see the
#: module docstring.
PACKAGE: Final = "interop_lab"

#: Distribution name on a package index. Not published by this repository.
DISTRIBUTION: Final = "mcp-failure-lab"

#: Harness version, recorded in every artifact for reproducibility.
HARNESS_VERSION: Final = "0.1.0"

#: Version of the on-disk artifact schemas. Bump on any breaking layout change.
#:
#: v2 replaced the container spec's single ``image``/``image_digest`` pair with
#: an explicit registry-or-local-build source, and added the resolved-image
#: record to every contained run. A v1 artifact cannot be read as v2: its one
#: digest field could hold either a registry digest or a builder-local image ID,
#: and which one it held was never recorded. Nothing guesses; a v1 artifact is
#: refused with a message saying to regenerate it.
ARTIFACT_SCHEMA_VERSION: Final = "2"

#: What the name currently is and is not. The name is settled; publication of a
#: distribution under it is a separate decision and has not been taken.
NAME_STATUS: Final = "Public name. Not published to any package registry."
