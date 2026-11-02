"""Parser for the official conformance framework's machine-readable output.

The official CLI writes a ``checks.json`` array under its ``--output-dir``. Each
entry looks like::

    {
      "id": "tools-list",
      "name": "ToolsList",
      "description": "Server lists available tools with valid structure",
      "status": "SUCCESS" | "FAILURE" | "WARNING" | "SKIPPED",
      "timestamp": "...",
      "errorMessage": "...",            # only on failure
      "specReferences": [{"id": ..., "url": ...}],
      "details": {...}
    }

Parsing is intentionally forgiving about unknown members and strict about the
ones it relies on: an output shape this parser does not recognise must surface
as a harness failure, never as a silent pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from interop_lab.conformance.models import ConformanceCheck

SUCCESS_STATES = frozenset({"SUCCESS", "PASS", "PASSED"})
FAILURE_STATES = frozenset({"FAILURE", "FAIL", "FAILED", "ERROR"})
SKIPPED_STATES = frozenset({"SKIPPED", "SKIP", "PENDING", "NOT_APPLICABLE"})
#: Reported by the official CLI but not a verdict. Counted as neither pass nor
#: fail, so an informational note can never inflate a pass rate.
INFORMATIONAL_STATES = frozenset({"INFO", "WARNING", "WARN", "NOTE"})


class ConformanceParseError(Exception):
    """The official output could not be understood. Never treated as a pass."""


def find_checks_files(output_dir: Path) -> list[Path]:
    """Locate every ``checks.json`` the official CLI wrote for a run.

    The official CLI writes one directory per scenario, so a suite run produces
    many files. Reading only the newest silently discards almost every result —
    and would report a suite of dozens of scenarios as a handful of checks.
    """
    if not output_dir.is_dir():
        return []
    return sorted(output_dir.rglob("checks.json"))


def parse_checks(payload: Any) -> list[ConformanceCheck]:
    """Convert the official checks array into typed records."""
    if not isinstance(payload, list):
        raise ConformanceParseError(
            f"expected a JSON array of checks, got {type(payload).__name__}"
        )

    checks: list[ConformanceCheck] = []
    for index, entry in enumerate(payload):
        if not isinstance(entry, dict):
            raise ConformanceParseError(f"check {index} is not an object")
        check_id = entry.get("id") or entry.get("name")
        if not isinstance(check_id, str):
            raise ConformanceParseError(f"check {index} has no usable id")

        status = entry.get("status")
        passed: bool | None
        if isinstance(status, str) and status.upper() in SUCCESS_STATES:
            passed = True
        elif isinstance(status, str) and status.upper() in FAILURE_STATES:
            passed = False
        elif isinstance(status, str) and (
            status.upper() in SKIPPED_STATES or status.upper() in INFORMATIONAL_STATES
        ):
            passed = None
        else:
            raise ConformanceParseError(
                f"check {check_id!r} has unrecognised status {status!r}; refusing to "
                "guess whether it passed"
            )

        checks.append(
            ConformanceCheck(
                check_id=check_id,
                scenario=entry.get("name") if isinstance(entry.get("name"), str) else None,
                passed=passed,
                status=status.upper(),
                message=(
                    entry.get("errorMessage")
                    if isinstance(entry.get("errorMessage"), str)
                    else entry.get("description")
                    if isinstance(entry.get("description"), str)
                    else None
                ),
            )
        )
    return checks


def load_checks(path: Path) -> list[ConformanceCheck]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConformanceParseError(f"could not read {path}: {exc}") from exc
    return parse_checks(payload)


def load_all_checks(output_dir: Path) -> list[ConformanceCheck]:
    """Aggregate every scenario's checks from one official run directory."""
    checks: list[ConformanceCheck] = []
    for path in find_checks_files(output_dir):
        checks.extend(load_checks(path))
    return checks


def summarise(checks: list[ConformanceCheck]) -> tuple[int, int, int]:
    """Return ``(total, passed, failed)``. Skipped checks count in neither."""
    passed = sum(1 for c in checks if c.passed is True)
    failed = sum(1 for c in checks if c.passed is False)
    return len(checks), passed, failed


def count_non_verdicts(checks: list[ConformanceCheck]) -> tuple[int, int]:
    """Return ``(skipped, informational)``.

    Both have ``passed is None``. They are not the same count: a skip is a
    check that did not run, and a warning is a note. Neither is a pass.
    """
    skipped = 0
    informational = 0
    for check in checks:
        if check.passed is not None or check.status is None:
            continue
        token = check.status.upper()
        if token in SKIPPED_STATES:
            skipped += 1
        elif token in INFORMATIONAL_STATES:
            informational += 1
    return skipped, informational
