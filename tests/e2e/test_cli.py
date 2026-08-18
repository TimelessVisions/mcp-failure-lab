"""End-to-end CLI tests: the workflow a user actually runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from interop_lab.cli.main import app
from interop_lab.naming import NAME_STATUS, PRODUCT_NAME

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

runner = CliRunner()

REFERENCE = "targets/fixtures/reference.yaml"
FAULTY = "targets/fixtures/faulty-result-type.yaml"


def test_version_reports_the_product_name_and_its_publication_status() -> None:
    """`version` must say what this is *and* what it is not.

    The status line used to read PROVISIONAL, because the name was. It is now
    settled, but "named" is not "published" — so the assertion moved to the
    claim that still matters: nothing here has been pushed to a registry.
    """
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert PRODUCT_NAME in result.stdout
    assert NAME_STATUS in result.stdout
    assert "Not published to any package registry" in NAME_STATUS


def test_list_scenarios_lists_every_scenario() -> None:
    result = runner.invoke(app, ["list-scenarios"])
    assert result.exit_code == 0
    assert "version.result_type_required" in result.stdout
    assert "baseline.tools_list" in result.stdout


def test_list_profiles_shows_both_eras() -> None:
    result = runner.invoke(app, ["list-profiles"])
    assert result.exit_code == 0
    assert "2025-11-25" in result.stdout
    assert "2026-07-28" in result.stdout


def test_run_smoke_passes_and_writes_a_json_report(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    result = runner.invoke(
        app,
        [
            "run",
            "-t",
            REFERENCE,
            "-s",
            "smoke",
            "--artifacts",
            str(tmp_path),
            "--json",
            str(report_path),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert report_path.is_file()

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["suite"] == "smoke"
    assert report["runs"]
    for run in report["runs"]:
        assert run["classification"] == "PASS"
        # Provenance required in every report.
        assert run["resolved_target"]["version"]
        assert run["spec_version"]
        assert run["trace_path"]


def test_run_exits_nonzero_on_a_spec_violation(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "-t",
            FAULTY,
            "--scenario",
            "version.result_type_required",
            "--artifacts",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    assert "SPEC_VIOLATION" in result.stdout


def test_diff_reports_normative_divergence(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "diff",
            "--left",
            REFERENCE,
            "--right",
            FAULTY,
            "--scenario",
            "version.result_type_required",
            "--artifacts",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    assert "normative_di" in result.stdout


def test_minimize_then_replay_roundtrip(tmp_path: Path) -> None:
    """The headline workflow: reduce a failure, then reproduce it from the package."""
    minimize = runner.invoke(
        app,
        [
            "minimize",
            "-t",
            FAULTY,
            "--scenario",
            "version.result_type_required",
            "--artifacts",
            str(tmp_path),
            "--finding-id",
            "TEST-0001",
        ],
    )
    assert minimize.exit_code == 0, minimize.stdout

    repro_dir = tmp_path / "repros" / "TEST-0001"
    for name in (
        "repro.yaml",
        "README.md",
        "wire.jsonl",
        "normalized-wire.jsonl",
        "expected.json",
        "actual.json",
        "environment.json",
        "target.json",
        "scenario.json",
        "seed.txt",
        "run.sh",
        "run.ps1",
    ):
        assert (repro_dir / name).is_file(), f"reproduction package is missing {name}"

    replay = runner.invoke(
        app, ["replay", str(repro_dir / "repro.yaml"), "--artifacts", str(tmp_path)]
    )
    assert replay.exit_code == 0, replay.stdout
    assert "reproduced" in replay.stdout


def test_unknown_scenario_fails_loudly(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["run", "-t", REFERENCE, "--scenario", "nope.nope", "--artifacts", str(tmp_path)]
    )
    assert result.exit_code == 2


def test_show_rerenders_a_stored_report(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    runner.invoke(
        app,
        [
            "run",
            "-t",
            REFERENCE,
            "-s",
            "smoke",
            "--artifacts",
            str(tmp_path),
            "--json",
            str(report_path),
        ],
    )
    result = runner.invoke(app, ["show", str(report_path)])
    assert result.exit_code == 0
    assert "baseline.tools_list" in result.stdout
