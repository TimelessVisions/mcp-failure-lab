"""The release checksum file, and the two defects it exists to prevent.

Both defects were real, both shipped, and both were invisible to the tests that
existed at the time — so this module tests the *failures* first and the happy
path second.

The controls at the bottom are the load-bearing part. A test suite that only
asserts the corrected implementation is correct cannot distinguish "these rules
are enforced" from "these rules are unreachable": every assertion would pass
against an implementation that skipped the file entirely. So each control
re-implements the historical behaviour and asserts that it *would* have been
accepted, next to the corrected implementation refusing the same input.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from interop_lab.export.checksums import (
    CHECKSUMS_FILENAME,
    GENERATED_DIRECTORIES,
    MANIFEST_FILENAME,
    ChecksumError,
    generate,
    parse,
    verify,
)

# --------------------------------------------------------------- fixtures


def write_manifest(root: Path, paths: list[str]) -> Path:
    """Write a manifest naming exactly these paths, in this order."""
    manifest = {
        "manifest_version": "1",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "harness_version": "0.1.0",
        "summary": {"included": len(paths), "excluded": 0, "safe": True},
        "included": [{"path": path, "reason": "test fixture"} for path in paths],
        "withheld_by_reason": {},
        "note": "test fixture",
    }
    target = root / MANIFEST_FILENAME
    target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return target


@pytest.fixture
def export(tmp_path: Path) -> Path:
    """A small, valid materialised export: three files in two directories."""
    root = tmp_path / "export"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "README.md").write_text("# readme\n", encoding="utf-8")
    (root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "docs" / "guide.md").write_text("# guide\n", encoding="utf-8")
    write_manifest(root, ["README.md", "src/app.py", "docs/guide.md"])
    return root


def run(root: Path) -> object:
    return generate(root, root / MANIFEST_FILENAME, root / CHECKSUMS_FILENAME)


def lines_of(root: Path) -> list[str]:
    return (root / CHECKSUMS_FILENAME).read_text(encoding="utf-8").splitlines()


# ------------------------------------------------------------- 1. correct


def test_a_correct_manifest_over_correct_files_verifies(export: Path) -> None:
    report = run(export)
    assert report.verified is True  # type: ignore[attr-defined]
    assert report.hashed_files == 3  # type: ignore[attr-defined]
    assert report.lines == 4  # type: ignore[attr-defined]


def test_the_written_file_is_readable_by_coreutils(export: Path) -> None:
    """`sha256sum -c` must accept what this writes, or it is not a checksum file."""
    run(export)
    result = subprocess.run(
        ["sha256sum", "-c", CHECKSUMS_FILENAME],
        cwd=export,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ------------------------------------------- 2-10. manifest and path rules


def test_a_missing_manifest_listed_file_fails(export: Path) -> None:
    """The historical defect. A `continue` here is how a release loses a file."""
    (export / "src" / "app.py").unlink()
    with pytest.raises(ChecksumError, match="missing from the export"):
        run(export)


def test_a_duplicate_manifest_path_fails(export: Path) -> None:
    write_manifest(export, ["README.md", "src/app.py", "README.md"])
    with pytest.raises(ChecksumError, match="listed twice"):
        run(export)


def test_an_absolute_posix_path_fails(export: Path) -> None:
    write_manifest(export, ["/etc/passwd"])
    with pytest.raises(ChecksumError, match="absolute POSIX path"):
        run(export)


def test_an_absolute_windows_path_fails(export: Path) -> None:
    write_manifest(export, ["C:\\Windows\\System32\\drivers\\etc\\hosts"])
    with pytest.raises(ChecksumError, match="absolute Windows path"):
        run(export)


def test_a_windows_path_with_forward_slashes_fails(export: Path) -> None:
    """`C:/x` is absolute on Windows and an ordinary relative path on POSIX."""
    write_manifest(export, ["C:/Windows/System32/config"])
    with pytest.raises(ChecksumError, match="absolute Windows path"):
        run(export)


@pytest.mark.parametrize("path", ["\\\\server\\share\\file", "//server/share/file"])
def test_a_unc_path_fails(export: Path, path: str) -> None:
    write_manifest(export, [path])
    with pytest.raises(ChecksumError, match="UNC"):
        run(export)


def test_a_traversal_path_fails(export: Path) -> None:
    write_manifest(export, ["../outside.txt"])
    with pytest.raises(ChecksumError, match=r"'\.\.' traversal"):
        run(export)


def test_a_traversal_that_normalises_back_inside_still_fails(export: Path) -> None:
    """`src/../README.md` lands in the root, and still describes a walk out of it.

    Rejected on the spelling rather than on the resolved location, because a
    resolve-then-compare check accepts this one: it resolves inside the root.
    """
    write_manifest(export, ["src/../README.md"])
    with pytest.raises(ChecksumError, match=r"'\.\.' traversal"):
        run(export)


def test_a_nested_traversal_escaping_the_root_fails(export: Path) -> None:
    write_manifest(export, ["src/../../secrets.txt"])
    with pytest.raises(ChecksumError, match=r"'\.\.' traversal"):
        run(export)


def test_an_empty_path_fails(export: Path) -> None:
    write_manifest(export, [""])
    with pytest.raises(ChecksumError, match="empty path"):
        run(export)


def test_a_directory_listed_as_a_file_fails(export: Path) -> None:
    write_manifest(export, ["README.md", "src"])
    with pytest.raises(ChecksumError, match="is a directory"):
        run(export)


def test_a_symlink_escaping_the_export_root_fails(export: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("private\n", encoding="utf-8")
    (export / "link.txt").symlink_to(outside)
    write_manifest(export, ["README.md", "link.txt"])
    with pytest.raises(ChecksumError, match="symbolic link"):
        run(export)


def test_a_symlinked_parent_directory_fails(export: Path, tmp_path: Path) -> None:
    """A link one level up is the same escape, one segment earlier."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "leak.txt").write_text("private\n", encoding="utf-8")
    (export / "linked").symlink_to(outside, target_is_directory=True)
    write_manifest(export, ["linked/leak.txt"])
    with pytest.raises(ChecksumError, match="symbolic link"):
        run(export)


def test_the_same_file_under_two_spellings_fails(export: Path) -> None:
    """`src/app.py` and `./src//app.py` are one file wearing two names."""
    write_manifest(export, ["src/app.py", "./src//app.py"])
    with pytest.raises(ChecksumError, match="second spelling"):
        run(export)


def test_a_manifest_with_an_unknown_field_is_refused(export: Path) -> None:
    payload = json.loads((export / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    payload["surprise"] = "unexpected"
    (export / MANIFEST_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ChecksumError, match="not a valid export manifest"):
        run(export)


def test_a_manifest_that_lists_itself_is_refused(export: Path) -> None:
    """The rule this implementation chose, asserted rather than assumed.

    The manifest is appended separately and exactly once, which is what makes the
    line count `unique included files + 1` with nothing to explain.
    """
    write_manifest(export, ["README.md", MANIFEST_FILENAME])
    with pytest.raises(ChecksumError, match="must not be listed as included"):
        run(export)


def test_a_manifest_that_lists_the_checksum_file_is_refused(export: Path) -> None:
    write_manifest(export, ["README.md", CHECKSUMS_FILENAME])
    with pytest.raises(ChecksumError, match="must not be listed as included"):
        run(export)


# --------------------------------------------------- 11-14. checksum rules


def test_a_mutated_file_fails_verification(export: Path) -> None:
    run(export)
    (export / "README.md").write_text("# tampered\n", encoding="utf-8")
    with pytest.raises(ChecksumError, match="checksum differs"):
        verify(export, export / MANIFEST_FILENAME, export / CHECKSUMS_FILENAME)


def test_an_extra_checksum_entry_fails(export: Path) -> None:
    run(export)
    path = export / CHECKSUMS_FILENAME
    digest = hashlib.sha256(b"anything\n").hexdigest()
    path.write_text(path.read_text(encoding="utf-8") + f"{digest}  stowaway.txt\n", "utf-8")
    with pytest.raises(ChecksumError, match="not in the approved export set"):
        verify(export, export / MANIFEST_FILENAME, path)


def test_a_missing_checksum_entry_fails(export: Path) -> None:
    run(export)
    path = export / CHECKSUMS_FILENAME
    kept = [line for line in lines_of(export) if "src/app.py" not in line]
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    with pytest.raises(ChecksumError, match="absent from SHA256SUMS"):
        verify(export, export / MANIFEST_FILENAME, path)


def test_a_duplicate_checksum_entry_fails(export: Path) -> None:
    run(export)
    path = export / CHECKSUMS_FILENAME
    existing = lines_of(export)
    path.write_text("\n".join([*existing, existing[0]]) + "\n", encoding="utf-8")
    with pytest.raises(ChecksumError, match="listed twice in SHA256SUMS"):
        verify(export, export / MANIFEST_FILENAME, path)


def test_a_malformed_line_fails(export: Path) -> None:
    run(export)
    path = export / CHECKSUMS_FILENAME
    path.write_text("not a checksum line\n", encoding="utf-8")
    with pytest.raises(ChecksumError, match="malformed line"):
        verify(export, export / MANIFEST_FILENAME, path)


def test_a_file_with_no_trailing_newline_fails(export: Path) -> None:
    run(export)
    path = export / CHECKSUMS_FILENAME
    path.write_text(path.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")
    with pytest.raises(ChecksumError, match="does not end with a newline"):
        verify(export, export / MANIFEST_FILENAME, path)


# ----------------------------------------- 15, 17, 19-22. shape and output


def test_the_line_count_is_exactly_included_plus_one(export: Path) -> None:
    report = run(export)
    manifest = json.loads((export / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert len(lines_of(export)) == len(manifest["included"]) + 1
    assert report.expected_lines == len(manifest["included"]) + 1  # type: ignore[attr-defined]


def test_exactly_one_trailing_newline_is_written(export: Path) -> None:
    run(export)
    text = (export / CHECKSUMS_FILENAME).read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert not text.endswith("\n\n")


def test_the_manifest_appears_exactly_once(export: Path) -> None:
    run(export)
    named = [line.split("  ", 1)[1] for line in lines_of(export)]
    assert named.count(MANIFEST_FILENAME) == 1


def test_the_checksum_file_never_hashes_itself(export: Path) -> None:
    run(export)
    named = [line.split("  ", 1)[1] for line in lines_of(export)]
    assert CHECKSUMS_FILENAME not in named


def test_path_ordering_is_stable_regardless_of_manifest_order(export: Path) -> None:
    """Reordering the manifest must not reorder the checksums.

    The manifest's *own* line is exempt from the byte comparison, and correctly
    so: reordering `included` changes the manifest file's bytes, so its digest
    changing is the mechanism working. Everything else must be identical.
    """
    run(export)
    first = lines_of(export)

    write_manifest(export, ["src/app.py", "docs/guide.md", "README.md"])
    run(export)
    second = lines_of(export)

    assert first[:-1] == second[:-1], "exported-file lines must not depend on manifest ordering"

    named = [line.split("  ", 1)[1] for line in first]
    assert named == [line.split("  ", 1)[1] for line in second]
    assert named[:-1] == sorted(named[:-1])
    assert named[-1] == MANIFEST_FILENAME


def test_two_consecutive_runs_produce_identical_bytes(export: Path) -> None:
    run(export)
    first = (export / CHECKSUMS_FILENAME).read_bytes()
    run(export)
    assert (export / CHECKSUMS_FILENAME).read_bytes() == first


def test_the_report_is_machine_readable(export: Path) -> None:
    report = run(export)
    payload = report.to_dict()  # type: ignore[attr-defined]
    assert json.loads(json.dumps(payload))["verified"] is True
    assert payload["hashed_files"] == 3
    assert payload["lines"] == payload["expected_lines"] == 4


# ------------------------------------------ 16-18. generated content stays out


@pytest.mark.parametrize("directory", GENERATED_DIRECTORIES)
def test_a_generated_directory_is_excluded_but_left_on_disk(export: Path, directory: str) -> None:
    """The gate installs and builds inside the export before this step runs.

    Those directories must stay on disk — later steps need them — and must not
    reach the release checksums. Both halves are asserted: a fix that deleted
    them would break the gate, and a fix that hashed them would misdescribe the
    release.
    """
    generated = export / directory / "nested"
    generated.mkdir(parents=True)
    (generated / "vendored.js").write_text("module.exports = 1\n", encoding="utf-8")

    run(export)

    named = [line.split("  ", 1)[1] for line in lines_of(export)]
    assert not any(name.startswith(f"{directory}/") for name in named)
    assert len(named) == 4
    assert (generated / "vendored.js").is_file(), "the gate still needs this on disk"


def test_dist_is_hashed_when_it_is_an_approved_exported_file(export: Path) -> None:
    """Exclusion is by manifest, not by name.

    Nothing filters `dist` out: it is absent because the allowlist did not
    approve it. A `dist` path the manifest *does* include is an ordinary
    exported file and is hashed like any other.
    """
    (export / "dist").mkdir()
    (export / "dist" / "shipped.txt").write_text("shipped\n", encoding="utf-8")
    write_manifest(export, ["README.md", "dist/shipped.txt"])

    run(export)

    named = [line.split("  ", 1)[1] for line in lines_of(export)]
    assert "dist/shipped.txt" in named


# ---------------------------------------------------------- end-to-end CLI


def test_the_cli_generates_and_then_checks_a_real_export(export: Path) -> None:
    """One test that goes through the actual command the gate script invokes.

    Helper-function tests do not prove the shell script calls anything correct.
    """
    command = [sys.executable, "-m", "interop_lab.cli.main"]

    generated = subprocess.run(
        [*command, "export-checksums", "--root", str(export)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr
    payload = json.loads(generated.stdout)
    assert payload["mode"] == "generate"
    assert payload["verified"] is True
    assert payload["lines"] == 4

    checked = subprocess.run(
        [*command, "export-checksums", "--root", str(export), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert json.loads(checked.stdout)["mode"] == "check"


def test_the_cli_exits_non_zero_when_a_manifest_file_is_missing(export: Path) -> None:
    (export / "docs" / "guide.md").unlink()
    result = subprocess.run(
        [sys.executable, "-m", "interop_lab.cli.main", "export-checksums", "--root", str(export)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert not (export / CHECKSUMS_FILENAME).exists(), "nothing may be written on failure"


# ----------------------------------------------------------------- controls
#
# Without these, every assertion above would also pass against an
# implementation that never enforced anything.


def test_control_the_historical_skip_would_have_accepted_a_missing_file(export: Path) -> None:
    """Reproduce the shipped `if not file.is_file(): continue` and show it passing.

    This is the defect verbatim. It writes a checksum file that is short one
    line, reports success, and leaves nothing for a reader to notice.
    """
    (export / "src" / "app.py").unlink()
    manifest = json.loads((export / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    paths = sorted(entry["path"] for entry in manifest["included"])

    historical: list[str] = []
    for name in [*paths, MANIFEST_FILENAME]:
        file = export / name
        if not file.is_file():
            continue  # the defect
        historical.append(f"{hashlib.sha256(file.read_bytes()).hexdigest()}  {name}")

    assert len(historical) == 3, "the historical generator silently dropped the missing file"
    assert not any("src/app.py" in line for line in historical)

    with pytest.raises(ChecksumError, match="missing from the export"):
        run(export)


def test_control_the_historical_recursive_walk_would_have_hashed_dependencies(
    export: Path,
) -> None:
    """Reproduce the older defect: hashing the directory instead of the export.

    By the time the gate hashes, it has run `uv sync` and `npm ci` in place. A
    recursive walk therefore covers a virtualenv and a `node_modules`; the
    corrected implementation cannot, because it never looks at the tree.
    """
    for directory in (".venv/lib", "node_modules/left-pad"):
        nested = export / directory
        nested.mkdir(parents=True)
        (nested / "installed.txt").write_text("dependency\n", encoding="utf-8")

    walked = sorted(p.relative_to(export).as_posix() for p in export.rglob("*") if p.is_file())
    assert ".venv/lib/installed.txt" in walked
    assert "node_modules/left-pad/installed.txt" in walked
    assert len(walked) > 4, "the recursive approach picks up generated dependencies"

    run(export)
    named = [line.split("  ", 1)[1] for line in lines_of(export)]
    assert len(named) == 4
    assert not any(name.startswith((".venv/", "node_modules/")) for name in named)


def test_control_parse_rejects_what_it_is_supposed_to_reject() -> None:
    """The reader is strict, so a hand-edited checksum file cannot pass as valid."""
    with pytest.raises(ChecksumError):
        parse("deadbeef  short-digest.txt\n")
    with pytest.raises(ChecksumError):
        parse(f"{'a' * 64} single-space.txt\n")
    assert parse(f"{'a' * 64}  fine.txt\n")[0].path == "fine.txt"
