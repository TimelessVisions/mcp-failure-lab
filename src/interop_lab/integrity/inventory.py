"""Every tracked file whose exact bytes are load-bearing.

A digest over ``path.read_bytes()`` is a promise that the file's bytes are the
same everywhere. Git breaks that promise by default: with ``core.autocrlf``
active — which is the default on Windows runners — a checkout rewrites LF to
CRLF, and a hash that is correct on Linux is wrong on Windows for a reason that
has nothing to do with the file's contents.

This has now happened twice. First to the pinned OASIS SARIF schema, which was
fixed by one ``schemas/*.json -text`` line. Then to
``targets/external/server-everything/package-lock.json``, which the earlier fix
did not cover, and which failed the Windows job in exactly the same way while
passing every Linux job. Two instances of one defect is a pattern, and a pattern
deserves an enumeration rather than a third one-off rule.

So the inventory is computed here rather than written down. It is derived from
the same image manifest the hashing code reads, so a new external image brings
its Dockerfile and lockfile into the inventory automatically, and
``test_byte_hash_portability`` fails if ``.gitattributes`` does not yet protect
them. A new hashed file cannot be added quietly and discovered on Windows.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from interop_lab.external.images import load_images

#: Lockfile names the external evidence manifest hashes out of a build context.
#: Kept in step with `interop_lab.external.matrix`, which reads the first of
#: these that exists and records it as the target's dependency-lock hash.
CONTEXT_LOCK_NAMES: tuple[str, ...] = (
    "package-lock.json",
    "requirements.lock.txt",
    "go.sum",
)


class BytePinnedFile(BaseModel):
    """One tracked file that is trusted by digest rather than by content."""

    model_config = ConfigDict(extra="forbid")

    #: Repository-relative, POSIX separators, so the value is the same on every
    #: platform and can be handed straight to `git check-attr`.
    path: str
    #: Where the digest is taken. Named so a reviewer can find the check that
    #: breaks when the bytes move.
    hashed_by: str
    #: What stops working if the bytes change underfoot.
    why: str


def _static_entries(repo_root: Path) -> list[BytePinnedFile]:
    """Files hashed by a fixed path rather than discovered through a manifest."""
    candidates = [
        BytePinnedFile(
            path="schemas/sarif-2.1.0.json",
            hashed_by="interop_lab.reporting.sarif.load_pinned_schema",
            why=(
                "SARIF output is validated against this exact schema. A rewritten "
                "byte makes the pinned digest fail and no report can be emitted."
            ),
        ),
        BytePinnedFile(
            path="uv.lock",
            hashed_by="interop_lab.artifacts.runtime.lock_hash",
            why=(
                "Recorded in every run's runtime fingerprint as the identity of the "
                "dependency set the evidence was produced under."
            ),
        ),
    ]
    return [entry for entry in candidates if (repo_root / entry.path).is_file()]


def _external_image_entries(repo_root: Path) -> list[BytePinnedFile]:
    """Dockerfiles and dependency locks, discovered from the image manifest."""
    entries: list[BytePinnedFile] = []
    for image in load_images(repo_root):
        source = image.source(repo_root)
        context = Path(source.build_context)

        dockerfile = Path(source.dockerfile)
        if (repo_root / dockerfile).is_file():
            entries.append(
                BytePinnedFile(
                    path=dockerfile.as_posix(),
                    hashed_by="interop_lab.external.matrix (dockerfile_sha256)",
                    why=(
                        f"Identifies how {image.key} was built. The external evidence "
                        "manifest records this digest as part of the build inputs."
                    ),
                )
            )

        for lock_name in CONTEXT_LOCK_NAMES:
            lock = context / lock_name
            if not (repo_root / lock).is_file():
                continue
            entries.append(
                BytePinnedFile(
                    path=lock.as_posix(),
                    hashed_by="interop_lab.external.matrix (lock_sha256)",
                    why=(
                        f"The dependency closure {image.key} installs. The declared "
                        "supply_chain.lock_sha256 in its target file must equal this "
                        "file's bytes on every machine that checks it."
                    ),
                )
            )
            break

    return entries


def _npm_lock_entries(repo_root: Path) -> list[BytePinnedFile]:
    """Every other tracked npm lockfile.

    Not hashed by the harness today, but ``npm ci`` reads them byte for byte and
    refuses to run against a lockfile it considers malformed. A CRLF rewrite is
    a supply-chain change wearing a whitespace disguise, so they get the same
    protection as the ones that are hashed.
    """
    tracked = _tracked_files(repo_root)
    known = {entry.path for entry in _external_image_entries(repo_root)}
    return [
        BytePinnedFile(
            path=path,
            hashed_by="npm ci (targets and tooling)",
            why="An npm lockfile is the exact dependency closure installed at build time.",
        )
        for path in tracked
        if path.endswith("package-lock.json") and path not in known
    ]


def _tracked_files(repo_root: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [entry for entry in completed.stdout.split("\0") if entry]


def byte_pinned_files(repo_root: Path | None = None) -> list[BytePinnedFile]:
    """Every tracked file whose exact bytes must survive a checkout, in path order."""
    root = repo_root or Path.cwd()
    entries = _static_entries(root) + _external_image_entries(root) + _npm_lock_entries(root)
    return sorted(entries, key=lambda entry: entry.path)


def text_attribute(path: str, *, repo_root: Path) -> str:
    """What Git will do to ``path`` on checkout, as Git itself reports it.

    Asking `git check-attr` rather than pattern-matching `.gitattributes` by
    hand: precedence between overlapping rules is Git's to decide, and a
    reimplementation of it would be one more thing that can be subtly wrong.
    """
    completed = subprocess.run(
        ["git", "check-attr", "text", "--", path],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    # Format: "<path>: text: <value>". The path may itself contain ": ".
    return completed.stdout.rstrip("\n").rsplit(": ", 1)[-1]


def unprotected_byte_pinned_files(repo_root: Path | None = None) -> list[BytePinnedFile]:
    """Byte-pinned files Git is still permitted to rewrite on checkout.

    Only ``unset`` — the ``-text`` form — actually guarantees the bytes.
    ``unspecified`` leaves the decision to ``core.autocrlf``, and ``set`` asks
    for the rewrite explicitly.
    """
    root = repo_root or Path.cwd()
    return [
        entry
        for entry in byte_pinned_files(root)
        if text_attribute(entry.path, repo_root=root) != "unset"
    ]
