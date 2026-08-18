"""The two commands an adopter meets first: `init` and `export`.

Both were added after a clean-room install showed the first ten minutes were
spent guessing file shapes. They are tested here rather than only in the
clean-room exercise, because a scaffold that stops producing loadable files is
a silent regression: nothing else in the suite would notice.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from interop_lab.cli.main import app
from interop_lab.config.matrix import MatrixDocument, SuiteDocument
from interop_lab.models.core import TargetDefinition

runner = CliRunner()


def in_git_repository() -> bool:
    """Whether `git ls-files` can answer here.

    `export` plans from the tracked file list, so it needs a repository. The
    export it produces deliberately has no `.git`, and these tests travel with
    it — inside that tree they skip rather than fail. They still run where they
    matter, which is the repository holding the private material.
    """
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
    except OSError:  # pragma: no cover - git absent entirely
        return False


needs_git = pytest.mark.skipif(
    not in_git_repository(),
    reason="no git repository here; this runs in the source tree, not in an export of it",
)


# ------------------------------------------------------------------- init


def test_init_writes_a_target_matrix_and_suite(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "--dir", str(tmp_path / "interop")])
    assert result.exit_code == 0, result.output

    for name in ("target.yaml", "matrix.yaml", "suite.yaml"):
        assert (tmp_path / "interop" / name).is_file(), f"{name} was not written"


def test_the_scaffolded_files_actually_load(tmp_path: Path) -> None:
    """A scaffold that produces files the loader rejects is worse than none."""
    directory = tmp_path / "interop"
    result = runner.invoke(
        app,
        ["init", "--dir", str(directory), "--target-id", "my.server", "--launch", "${PYTHON} s.py"],
    )
    assert result.exit_code == 0, result.output

    target = TargetDefinition.model_validate(
        yaml.safe_load((directory / "target.yaml").read_text(encoding="utf-8"))
    )
    assert target.target_id == "my.server"
    assert target.launch_command == ["${PYTHON}", "s.py"]

    # These two are the ones a hand-written file got wrong in the clean room.
    MatrixDocument.model_validate(
        yaml.safe_load((directory / "matrix.yaml").read_text(encoding="utf-8"))
    )
    SuiteDocument.model_validate(
        yaml.safe_load((directory / "suite.yaml").read_text(encoding="utf-8"))
    )


def test_init_names_every_scenario_the_suite_references(tmp_path: Path) -> None:
    """A scaffolded suite must not reference a scenario that does not exist."""
    from interop_lab.scenarios.library import ALL_SCENARIOS

    directory = tmp_path / "interop"
    runner.invoke(app, ["init", "--dir", str(directory)])
    suite = SuiteDocument.model_validate(
        yaml.safe_load((directory / "suite.yaml").read_text(encoding="utf-8"))
    )
    unknown = [sid for sid in suite.scenarios if sid not in ALL_SCENARIOS]
    assert not unknown, f"the scaffold references scenarios that do not exist: {unknown}"


def test_init_prints_the_next_commands(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "--dir", str(tmp_path / "interop")])
    for expected in ("run -t", "baseline create", "ci --baseline"):
        assert expected in result.output, f"{expected!r} missing from the next steps"


def test_init_does_not_overwrite_existing_files(tmp_path: Path) -> None:
    """Re-running in a configured repository must not discard a real config."""
    directory = tmp_path / "interop"
    directory.mkdir()
    (directory / "target.yaml").write_text("target_id: already.mine\n", encoding="utf-8")

    result = runner.invoke(app, ["init", "--dir", str(directory)])
    assert result.exit_code == 0
    assert "already.mine" in (directory / "target.yaml").read_text(encoding="utf-8")
    assert "skipped" in result.output


# ----------------------------------------------------------------- export


@needs_git
def test_export_dry_run_writes_nothing(tmp_path: Path) -> None:
    destination = tmp_path / "out"
    result = runner.invoke(app, ["export", "--out", str(destination), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert not destination.exists(), "a dry run must not write"
    assert "dry run" in result.output


@needs_git
def test_export_reports_what_it_withholds(tmp_path: Path) -> None:
    result = runner.invoke(app, ["export", "--out", str(tmp_path / "out"), "--dry-run"])
    assert "explicitly forbidden" in result.output
    assert "export plan" in result.output


@needs_git
def test_export_writes_a_tree_with_no_git_history(tmp_path: Path) -> None:
    destination = tmp_path / "out"
    result = runner.invoke(app, ["export", "--out", str(destination)])
    assert result.exit_code == 0, result.output

    assert (destination / "EXPORT-MANIFEST.json").is_file()
    assert not (destination / ".git").exists(), "an export must carry no history"
    assert (destination / "src" / "interop_lab" / "naming.py").is_file()


@needs_git
def test_the_export_contains_no_private_material(tmp_path: Path) -> None:
    """The check that matters, run against a real materialised tree."""
    destination = tmp_path / "out"
    assert runner.invoke(app, ["export", "--out", str(destination)]).exit_code == 0

    for marker in ("private-findings", "commercialization", "private-results"):
        leaked = list(destination.rglob(f"*{marker}*"))
        assert not leaked, f"{marker} reached the export: {leaked}"

    # And nothing inside any exported file names a private finding — except the
    # scanner and its tests, which contain the *pattern* because detecting it is
    # their job. Exempting them by path is the same distinction the scanner
    # itself draws; exempting them globally would defeat the check.
    from interop_lab.export.manifest import CONTENT_EXEMPT

    exempt = {rule.pattern for rule in CONTENT_EXEMPT}
    for path in destination.rglob("*"):
        if not path.is_file() or path.suffix in {".png", ".jpg", ".ico"}:
            continue
        relative = path.relative_to(destination).as_posix()
        if relative in exempt:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        assert "ILAB-EXT-0" not in text, f"{relative} names a private finding"


@needs_git
def test_the_manifest_counts_rather_than_names_what_it_withheld(tmp_path: Path) -> None:
    destination = tmp_path / "out"
    assert runner.invoke(app, ["export", "--out", str(destination)]).exit_code == 0

    manifest = json.loads((destination / "EXPORT-MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["withheld_by_reason"], "the manifest must say how much was withheld"
    assert "excluded" not in manifest, "excluded paths must not be listed by name"
    assert manifest["summary"]["content_violations"] == 0


@needs_git
def test_export_refuses_a_non_empty_destination_without_force(tmp_path: Path) -> None:
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / "something.txt").write_text("keep me", encoding="utf-8")

    result = runner.invoke(app, ["export", "--out", str(destination)])
    assert result.exit_code != 0
    assert (destination / "something.txt").is_file(), "an existing tree must not be destroyed"


@needs_git
def test_export_force_replaces_the_destination(tmp_path: Path) -> None:
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / "stale.txt").write_text("old", encoding="utf-8")

    result = runner.invoke(app, ["export", "--out", str(destination), "--force"])
    assert result.exit_code == 0, result.output
    assert not (destination / "stale.txt").exists()
    assert (destination / "EXPORT-MANIFEST.json").is_file()
