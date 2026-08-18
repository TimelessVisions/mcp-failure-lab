"""What a build context actually contains, stated exactly.

The build-context fingerprint used to walk every regular file under the
configured directory. Its documentation said "every committed file in the build
context", and those are not the same set: a scratch file a developer left beside
a Dockerfile is neither committed nor invisible to the builder, so the walk
either hashed it — making the portable identity depend on one machine's untidy
working tree — or, had the walk been the other way round, silently omitted a file
the builder was about to send.

Neither is acceptable in a value the rest of the project treats as *the* portable
statement of what an image is built from. So the model is made exact and named:

    EFFECTIVE COMMITTED DOCKER CONTEXT

    The set of paths that (a) Git tracks, (b) lie under the configured build
    context, and (c) survive the effective ``.dockerignore``, plus the Dockerfile
    itself. An untracked path that would enter the builder's context is a hard
    error rather than a decision this code makes quietly in either direction.

Every entry records its normalized path, its type, its content hash, and the
executable bit — the one mode bit that changes what the image does. Paths are
POSIX-normalised and the manifest is ordered, so a Windows checkout produces the
same bytes as a Linux one; anything else would report drift on every
cross-platform comparison, which is a check that only ever cries wolf.

The ``.dockerignore`` support here is deliberately narrow — the patterns these
contexts use and no more — and any pattern outside it is refused by name rather
than approximated. An ignore rule this code misunderstands would remove a file
from the manifest that the builder still sends, which is the exact failure the
whole exercise is meant to end.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from enum import StrEnum
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

#: Bumped when the manifest's contents or ordering change, because the
#: fingerprint derived from it would otherwise move with no explanation.
CONTEXT_MANIFEST_SCHEMA_VERSION = "1"


class ContextEntryType(StrEnum):
    """What kind of thing a context entry is.

    Recorded rather than assumed. A symlink and the file it points at are
    different inputs to a build even when their contents are identical, and a
    manifest that flattened them would call two different contexts the same.
    """

    FILE = "file"
    SYMLINK = "symlink"


class BuildContextError(Exception):
    """The context cannot be described exactly, so no fingerprint may be derived.

    Raised rather than worked around. Every caller of this module is computing an
    identity that other machines will be held to, and an identity derived from a
    context nobody could enumerate precisely is worse than no identity at all.
    """


class ContextEntry(BaseModel):
    """One path the builder will receive."""

    model_config = ConfigDict(extra="forbid")

    #: Relative to the build context root, POSIX-separated, never absolute.
    path: str
    kind: ContextEntryType
    #: Content hash. Present for a regular file; absent for a symlink, whose
    #: content is its target.
    sha256: str | None = None
    #: Recorded only where it can change execution. Docker preserves the
    #: executable bit and discards the rest of the mode, so the rest is not
    #: identity — including it would make a fresh clone with a different umask
    #: fingerprint differently.
    executable: bool = False
    #: Where a permitted symlink points, POSIX-separated and relative.
    symlink_target: str | None = None


class BuildContextManifest(BaseModel):
    """Every path the builder will receive, in a stable order."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = CONTEXT_MANIFEST_SCHEMA_VERSION
    #: The context root, relative to the repository. Part of the manifest because
    #: the same file set under two different roots is two different builds.
    context: str
    entries: list[ContextEntry] = Field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        """One value over the whole manifest.

        Hashes the serialized manifest rather than an ad-hoc tuple list, so a
        field added to ``ContextEntry`` is covered the moment it exists instead
        of on the day someone remembers to add it here too.
        """
        material = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


class DockerignoreRules:
    """The subset of ``.dockerignore`` this project uses, and nothing more.

    Docker's ignore syntax has exclusions, `**` at arbitrary depth, and
    last-match-wins ordering. Implementing a near-miss of it would be worse than
    implementing none: a rule this code read differently from the builder would
    drop a path from the manifest that the build still receives, and the
    fingerprint would then certify a context that was never sent.

    So the supported forms are enumerated, and anything else raises. The
    contexts in this repository carry no ignore file at all today; this exists so
    that adding one is a deliberate, checked act rather than a silent change of
    meaning.
    """

    def __init__(self, patterns: list[str]) -> None:
        self.patterns = patterns
        for pattern in patterns:
            if pattern.startswith("!"):
                raise BuildContextError(
                    f".dockerignore exclusion {pattern!r} is not supported. Exclusions "
                    "depend on last-match-wins ordering against every preceding "
                    "pattern, and a partial implementation of that would remove paths "
                    "from this manifest that the builder still receives."
                )
            if "**" in pattern:
                raise BuildContextError(
                    f".dockerignore pattern {pattern!r} uses `**`, which is not "
                    "supported here. Add it deliberately with a matching test rather "
                    "than approximating the builder's matcher."
                )

    @classmethod
    def load(cls, context: Path) -> DockerignoreRules:
        path = context / ".dockerignore"
        if not path.is_file():
            return cls([])
        patterns: list[str] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # Docker normalises leading slashes away; a rule is always relative
            # to the context root.
            patterns.append(line.lstrip("/"))
        return cls(patterns)

    def ignores(self, relative: PurePosixPath) -> bool:
        """Whether the builder would drop this path.

        A directory pattern matches everything beneath it, which is how Docker
        behaves and is the only form these rules can express.
        """
        for pattern in self.patterns:
            if relative.match(pattern):
                return True
            if any(parent.match(pattern) for parent in relative.parents if parent.name):
                return True
        return False


def _tracked_files(context: Path, *, repo_root: Path) -> list[PurePosixPath]:
    """Paths Git tracks under the context, as Git itself reports them.

    ``git ls-files`` rather than a filesystem walk plus a guess about what is
    committed: Git is the authority on what is tracked, and it already emits
    POSIX separators on every platform.
    """
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--", str(context.relative_to(repo_root).as_posix())],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise BuildContextError(
            f"could not list tracked files under {context}: {completed.stderr.strip()}. "
            "The committed context is what this identity is derived from, so it is not "
            "guessed from the working tree when Git cannot be asked."
        )
    root = PurePosixPath(context.relative_to(repo_root).as_posix())
    return sorted(
        PurePosixPath(line).relative_to(root)
        for line in completed.stdout.split("\0")
        if line.strip()
    )


def _present_files(context: Path) -> set[PurePosixPath]:
    """Every path on disk under the context, tracked or not.

    The builder does not read the index. Whatever is here is what it receives,
    which is why this set exists at all: it is the one that has to be reconciled
    against the tracked set.
    """
    present: set[PurePosixPath] = set()
    for path in context.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            continue
        present.add(PurePosixPath(path.relative_to(context).as_posix()))
    return present


def _entry_for(context: Path, relative: PurePosixPath) -> ContextEntry:
    absolute = context / Path(*relative.parts)

    if absolute.is_symlink():
        target = absolute.readlink()
        resolved = (absolute.parent / target).resolve()
        try:
            resolved.relative_to(context.resolve())
        except ValueError:
            raise BuildContextError(
                f"{relative} is a symlink to {target}, which escapes the build context. "
                "Docker refuses to follow it out of the context, so the build would "
                "receive something other than what this manifest describes."
            ) from None
        return ContextEntry(
            path=relative.as_posix(),
            kind=ContextEntryType.SYMLINK,
            # POSIX-separated so a Windows checkout produces the same manifest.
            symlink_target=PurePosixPath(target.as_posix()).as_posix(),
        )

    if not absolute.is_file():
        raise BuildContextError(
            f"{relative} is tracked under the build context but is neither a regular "
            f"file nor a symlink. Nothing here can describe it, so no fingerprint is "
            "derived rather than one that silently omits it."
        )

    data = absolute.read_bytes()
    return ContextEntry(
        path=relative.as_posix(),
        kind=ContextEntryType.FILE,
        sha256="sha256:" + hashlib.sha256(data).hexdigest(),
        # Read from Git rather than from the filesystem would be stricter still,
        # but the working tree is what the builder sends and this is the bit that
        # decides whether the image can run the file.
        executable=bool(absolute.stat().st_mode & 0o111),
    )


def build_context_manifest(
    context: Path, *, repo_root: Path, dockerfile: Path | None = None
) -> BuildContextManifest:
    """Enumerate the effective committed Docker context.

    ``dockerfile`` is included even when it lies outside the context directory,
    because the builder receives it either way — passing ``--file`` does not make
    a Dockerfile stop being an input. Omitting it would let a Dockerfile edit
    leave the context fingerprint untouched.
    """
    if not context.is_dir():
        raise FileNotFoundError(f"build context {context} does not exist")

    rules = DockerignoreRules.load(context)
    tracked = set(_tracked_files(context, repo_root=repo_root))
    present = _present_files(context)

    # The reconciliation the old walk never performed. An untracked file that the
    # builder would receive makes this identity depend on one machine's working
    # tree, and silently including or excluding it are both wrong: one makes the
    # fingerprint unreproducible, the other makes it describe a context that was
    # not sent.
    intruders = sorted(path.as_posix() for path in present - tracked if not rules.ignores(path))
    if intruders:
        raise BuildContextError(
            f"{context} contains untracked, non-ignored file(s) that the Docker build "
            f"would receive: {intruders}. The portable identity of this image is "
            "derived from committed inputs, so an uncommitted file entering the build "
            "is refused rather than hashed into a value another machine cannot "
            "reproduce. Commit it, add it to .dockerignore, or remove it."
        )

    included = sorted(path for path in tracked if not rules.ignores(path))
    entries = [_entry_for(context, path) for path in included]

    if dockerfile is not None:
        relative_dockerfile = PurePosixPath(dockerfile.name)
        try:
            relative_dockerfile = PurePosixPath(
                dockerfile.resolve().relative_to(context.resolve()).as_posix()
            )
        except ValueError:
            relative_dockerfile = None  # type: ignore[assignment]
        if relative_dockerfile is not None and not any(
            entry.path == relative_dockerfile.as_posix() for entry in entries
        ):
            # Inside the context but ignored, or untracked-and-ignored. The
            # builder reads it regardless, so the manifest has to say so.
            entries.append(_entry_for(context, relative_dockerfile))
            entries.sort(key=lambda entry: entry.path)

    if not entries:
        raise BuildContextError(f"build context {context} contains no committed files")

    return BuildContextManifest(context=context.relative_to(repo_root).as_posix(), entries=entries)
