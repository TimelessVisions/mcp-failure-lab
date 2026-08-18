"""Official conformance parsing and separation of concerns."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from interop_lab.conformance.models import (
    CONFORMANCE_SOURCE_SHA,
    CONFORMANCE_VERSION,
    ConformanceOutcome,
    ConformanceRun,
)
from interop_lab.conformance.parser import (
    ConformanceParseError,
    find_checks_files,
    load_all_checks,
    parse_checks,
    summarise,
)
from interop_lab.models.core import Classification


def check(check_id: str, status: str) -> dict[str, Any]:
    return {"id": check_id, "name": check_id, "status": status, "description": "d"}


def test_official_and_interop_vocabularies_do_not_overlap() -> None:
    """An official failure must never be reportable as a Failure Lab finding."""
    official = {member.value for member in ConformanceOutcome}
    interop = {member.value for member in Classification}
    assert official.isdisjoint(interop)


def test_pinned_version_is_recorded() -> None:
    run = ConformanceRun(
        requirements="2026-07-28",
        target_id="t",
        url="http://127.0.0.1:1/mcp",
        command=["node"],
        outcome=ConformanceOutcome.OFFICIAL_CONFORMANCE_PASS,
    )
    provenance = run.provenance()
    assert provenance["version"] == CONFORMANCE_VERSION == "0.2.0-alpha.11"
    assert provenance["source_sha"] == CONFORMANCE_SOURCE_SHA
    assert provenance["repository"] == "modelcontextprotocol/conformance"


def test_parses_success_and_failure() -> None:
    checks = parse_checks([check("a", "SUCCESS"), check("b", "FAILURE")])
    assert [c.passed for c in checks] == [True, False]
    assert summarise(checks) == (2, 1, 1)


@pytest.mark.parametrize("status", ["SKIPPED", "PENDING", "INFO", "WARNING"])
def test_non_verdict_states_count_as_neither(status: str) -> None:
    """An informational note must not inflate the pass rate."""
    checks = parse_checks([check("a", "SUCCESS"), check("b", status)])
    total, passed, failed = summarise(checks)
    assert (total, passed, failed) == (2, 1, 0)


def test_unknown_status_is_refused_not_guessed() -> None:
    """Refusing to guess is the point: an unknown status is never a pass."""
    with pytest.raises(ConformanceParseError, match="unrecognised status"):
        parse_checks([check("a", "MAYBE")])


def test_non_array_payload_is_refused() -> None:
    with pytest.raises(ConformanceParseError, match="expected a JSON array"):
        parse_checks({"id": "a"})


def test_check_without_id_is_refused() -> None:
    with pytest.raises(ConformanceParseError, match="no usable id"):
        parse_checks([{"status": "SUCCESS"}])


def test_every_scenario_directory_is_aggregated(tmp_path: Path) -> None:
    """The official CLI writes one directory per scenario.

    Reading only the newest reported a 41-check suite as 2 checks, which
    understated coverage by an order of magnitude.
    """
    for index, status in enumerate(["SUCCESS", "FAILURE", "SUCCESS"]):
        run_dir = tmp_path / f"server-scenario-{index}"
        run_dir.mkdir()
        (run_dir / "checks.json").write_text(
            json.dumps([check(f"c{index}", status)]), encoding="utf-8"
        )

    assert len(find_checks_files(tmp_path)) == 3
    checks = load_all_checks(tmp_path)
    assert summarise(checks) == (3, 2, 1)


def test_missing_directory_yields_no_files(tmp_path: Path) -> None:
    assert find_checks_files(tmp_path / "nope") == []


def test_unavailable_framework_is_a_harness_failure_not_a_pass() -> None:
    run = ConformanceRun(
        requirements="2026-07-28",
        target_id="t",
        url="http://127.0.0.1:1/mcp",
        command=["node"],
        outcome=ConformanceOutcome.HARNESS_FAILURE,
        parse_note="not installed",
    )
    assert run.outcome is ConformanceOutcome.HARNESS_FAILURE
    assert not run.failed  # a harness failure is not a conformance failure
