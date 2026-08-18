"""Artifact portability.

A committed artifact must reproduce on a machine that is not the one that
produced it. That rules out absolute paths, temporary directories, and
interpreter locations baked in at capture time.

Two mechanisms:

* **Artifact URIs** — ``artifact://runs/<run-id>/wire.jsonl`` replaces an
  absolute filesystem path. Resolution happens at read time against whatever
  artifact root the reader has.
* **Executable tokens** — ``${PYTHON}`` in a launch command resolves to the
  interpreter running the harness. A checked-in ``.venv/bin/python`` is not
  portable: it is POSIX-only and assumes a particular working directory.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any

ARTIFACT_SCHEME = "artifact://"

#: Absolute paths that are portable by convention rather than machine-specific.
#: `/usr/bin/env` is the canonical portable shebang: it exists at that path on
#: every POSIX system precisely so scripts need not hard-code an interpreter.
PORTABLE_ABSOLUTE_PATHS: frozenset[str] = frozenset({"/usr/bin/env"})

#: Tokens permitted inside a launch command, resolved at run time.
PYTHON_TOKEN = "${PYTHON}"  # noqa: S105 - a substitution token, not a credential
EXECUTABLE_TOKENS: dict[str, str] = {PYTHON_TOKEN: "the interpreter running the harness"}

_ABSOLUTE_POSIX = re.compile(r"(?<![\w.])/(?:[\w.\-+]+/)+[\w.\-+]*")
_ABSOLUTE_WINDOWS = re.compile(r"[A-Za-z]:[\\/](?:[^\\/\n\"]+[\\/])*[^\\/\n\"]*")
_UNC_WINDOWS = re.compile(r"\\\\[^\\\n]+\\[^\\\n]+")

#: Paths that are machine-specific even when they look ordinary.
_FORBIDDEN_FRAGMENTS = (
    "/tmp/claude-",  # noqa: S108 - matched as a forbidden fragment, never used as a path
    "scratchpad",
    "/home/user/",
    "/Users/",
    "C:\\Users\\",
    "/github/workspace",
    "/runner/work",
    "/var/folders/",
    "/private/var/",
)


class PortabilityError(Exception):
    """An artifact contains something that will not survive relocation."""


def to_artifact_uri(path: str | Path, *, root: Path) -> str:
    """Express ``path`` relative to the artifact root as an artifact URI.

    Falls back to a repository-relative path when the target lies outside the
    artifact root but inside the working tree.
    """
    candidate = Path(path)
    try:
        relative = candidate.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        try:
            relative = candidate.resolve().relative_to(Path.cwd())
        except (ValueError, OSError):
            # Nothing sensible to anchor against; keep only the tail so no
            # machine-specific prefix survives.
            relative = Path(candidate.name)
    return ARTIFACT_SCHEME + PurePosixPath(*relative.parts).as_posix()


def resolve_artifact_uri(uri: str, *, root: Path) -> Path:
    """Resolve an artifact URI against a reader-supplied root."""
    if not uri.startswith(ARTIFACT_SCHEME):
        return Path(uri)
    return root / PurePosixPath(uri[len(ARTIFACT_SCHEME) :])


def resolve_executable_tokens(command: list[str]) -> list[str]:
    """Expand documented executable tokens for the current machine."""
    return [part.replace(PYTHON_TOKEN, sys.executable) for part in command]


def uses_only_documented_tokens(command: list[str]) -> bool:
    return all(
        not _looks_absolute(part) or any(token in part for token in EXECUTABLE_TOKENS)
        for part in command
    )


def _looks_absolute(text: str) -> bool:
    return bool(
        _ABSOLUTE_POSIX.search(text) or _ABSOLUTE_WINDOWS.search(text) or _UNC_WINDOWS.search(text)
    )


#: Schemes whose "//" is part of a URI, not a filesystem path.
_URI_SCHEMES = re.compile(r"\b(?:artifact|https?|file|ftp|git\+ssh|ssh)://\S*")


def violations_in_text(text: str, *, context: str = "") -> list[str]:
    """Return every portability problem found in a piece of text.

    URIs are removed before path matching. An ``artifact://`` reference is
    exactly what portability requires, and the scheme's own ``//`` would
    otherwise be mistaken for an absolute path.
    """
    problems: list[str] = []
    text = _URI_SCHEMES.sub(" ", text)

    for fragment in _FORBIDDEN_FRAGMENTS:
        if fragment in text:
            problems.append(f"{context}: contains machine-specific fragment {fragment!r}")

    for match in _ABSOLUTE_POSIX.finditer(text):
        value = match.group(0)
        if value.startswith(("/mcp", "/tools/", "/resources/", "/prompts/", "/server/")):
            continue  # JSON-RPC method names and URL paths, not filesystem paths.
        if any(token in value for token in EXECUTABLE_TOKENS):
            continue
        if value in PORTABLE_ABSOLUTE_PATHS:
            continue
        problems.append(f"{context}: absolute POSIX path {value!r}")

    for match in _ABSOLUTE_WINDOWS.finditer(text):
        problems.append(f"{context}: absolute Windows path {match.group(0)!r}")
    for match in _UNC_WINDOWS.finditer(text):
        problems.append(f"{context}: UNC path {match.group(0)!r}")

    return problems


def _walk(value: Any, path: str, problems: list[str], *, allow_tokens: bool) -> None:
    if isinstance(value, str):
        if allow_tokens and any(token in value for token in EXECUTABLE_TOKENS):
            return
        problems.extend(violations_in_text(value, context=path))
    elif isinstance(value, dict):
        for key, item in value.items():
            # A launch command may carry a documented token.
            nested_tokens = allow_tokens or key == "launch_command"
            _walk(item, f"{path}.{key}" if path else str(key), problems, allow_tokens=nested_tokens)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk(item, f"{path}[{index}]", problems, allow_tokens=allow_tokens)


def validate_payload(payload: Any, *, context: str = "") -> list[str]:
    """Recursively check a decoded artifact for non-portable references."""
    problems: list[str] = []
    _walk(payload, context, problems, allow_tokens=False)
    return problems


def assert_portable(payload: Any, *, context: str = "") -> None:
    problems = validate_payload(payload, context=context)
    if problems:
        raise PortabilityError("artifact is not portable:\n  " + "\n  ".join(problems))
