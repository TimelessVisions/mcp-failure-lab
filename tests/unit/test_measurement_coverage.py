"""The coverage ledger must be complete, and it must not flatter the evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from interop_lab.benchmark.coverage import (
    HEADLINE_MINIMUM_SAMPLES,
    REQUIRED_METRIC_IDS,
    CoverageState,
    MeasurementCategory,
    MeasurementCoverage,
    MeasurementCoverageEntry,
    build_coverage,
)
from interop_lab.benchmark.model import MeasurementState


def _write(directory: Path, name: str, payload: Any) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


def _operation(metric_id: str, samples: list[float]) -> dict[str, Any]:
    return {
        "metric_id": metric_id,
        "display_name": metric_id,
        "state": MeasurementState.MEASURED.value,
        "boundary": "b",
        "environment": "e",
        "warmup_count": 1,
        "samples_ms": samples,
    }


def test_every_promised_metric_appears_even_when_the_directory_is_empty(
    tmp_path: Path,
) -> None:
    """An absent artifact makes a metric NOT_MEASURED. It never removes it."""
    coverage = build_coverage(tmp_path)

    assert {entry.metric_id for entry in coverage.entries} == set(REQUIRED_METRIC_IDS)
    for metric_id in REQUIRED_METRIC_IDS:
        entry = coverage.entry(metric_id)
        assert entry.reason_not_measured, f"{metric_id} states no reason"
        assert entry.limitation


def test_a_metric_with_no_samples_is_never_reported_measured(tmp_path: Path) -> None:
    coverage = build_coverage(tmp_path)
    for entry in coverage.entries:
        if entry.state is CoverageState.NOT_MEASURED:
            assert entry.sample_count == 0
            assert entry.artifact_reference is None


def test_an_operation_below_the_sample_floor_is_partial_not_measured(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "measured-operations.json",
        {"operations": [_operation("replay", [1.0, 2.0])]},
    )
    entry = build_coverage(tmp_path).entry("replay")

    assert entry.state is CoverageState.PARTIALLY_MEASURED
    assert entry.sample_count == 2
    assert entry.reason_not_measured


def test_an_operation_at_the_floor_is_measured(tmp_path: Path) -> None:
    samples = [float(n) for n in range(HEADLINE_MINIMUM_SAMPLES)]
    _write(tmp_path, "measured-operations.json", {"operations": [_operation("replay", samples)]})
    entry = build_coverage(tmp_path).entry("replay")

    assert entry.state is CoverageState.MEASURED
    assert entry.sample_count == HEADLINE_MINIMUM_SAMPLES
    assert entry.artifact_reference == "measured-operations.json"
    assert entry.reason_not_measured is None


def test_startup_and_first_request_stay_unmeasured_whatever_else_was_timed(
    tmp_path: Path,
) -> None:
    """The two dimensions with no boundary must not become measurable by proximity.

    Both are named by the methodology and neither has a boundary. The tempting
    error is to notice that the battery duration covers them and let one of them
    inherit a number from it, so this asserts they stay absent even when every
    surrounding artifact is present and full.
    """
    samples = [float(n) for n in range(HEADLINE_MINIMUM_SAMPLES)]
    _write(
        tmp_path,
        "measured-operations.json",
        {
            "operations": [
                _operation(metric, samples)
                for metric in ("capability_discovery", "cleanup", "replay", "minimization")
            ]
        },
    )
    _write(
        tmp_path,
        "timings.json",
        [
            {
                "name": "x: applicable suite runtime",
                "state": MeasurementState.MEASURED.value,
                "samples_ms": samples,
            }
        ],
    )
    coverage = build_coverage(tmp_path)

    assert coverage.entry("target_startup").state is CoverageState.NOT_MEASURED
    assert coverage.entry("first_valid_request").state is CoverageState.NOT_MEASURED
    assert "subtraction" in (coverage.entry("target_startup").reason_not_measured or "")


def test_the_two_structurally_incomparable_overhead_pairs_stay_unmeasured(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "containment-overhead.json",
        {
            "pairs": [
                {
                    "transport": "stdio",
                    "state": MeasurementState.NOT_MEASURED.value,
                    "reason": "no contained twin exists",
                },
                {
                    "transport": "streamable_http (2025-11-25)",
                    "state": MeasurementState.NOT_MEASURED.value,
                    "reason": "the contained side issues sessions and the host side does not",
                },
                {
                    "transport": "streamable_http (2026-07-28)",
                    "state": MeasurementState.MEASURED.value,
                    "host": {"samples_ms": [1.0] * HEADLINE_MINIMUM_SAMPLES},
                    "contained": {"samples_ms": [2.0] * HEADLINE_MINIMUM_SAMPLES},
                },
            ]
        },
    )
    coverage = build_coverage(tmp_path)

    assert coverage.entry("containment_overhead_stdio").state is CoverageState.NOT_MEASURED
    assert coverage.entry("containment_overhead_http_2025").state is CoverageState.NOT_MEASURED
    assert coverage.entry("containment_overhead_http_2026").state is CoverageState.MEASURED


def test_cross_environment_stays_partial_until_a_second_environment_is_readable(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "reproduction-results.json",
        {"environment_b": {"state": MeasurementState.NOT_MEASURED.value, "reason": "one machine"}},
    )
    entry = build_coverage(tmp_path).entry("cross_environment_result_signatures")

    assert entry.state is CoverageState.PARTIALLY_MEASURED
    assert entry.reason_not_measured


def test_clean_image_build_reports_its_durations_without_a_headline(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "build-resolution.json",
        {"entries": [{"target_id": "a", "build_duration_ms": 1200.0}]},
    )
    entry = build_coverage(tmp_path).entry("clean_image_build")

    assert entry.state is CoverageState.PARTIALLY_MEASURED
    assert entry.sample_count == 1
    assert entry.warmup_count == 0


def test_an_absent_measurement_without_a_reason_does_not_validate() -> None:
    with pytest.raises(ValidationError, match="records no reason"):
        MeasurementCoverageEntry(
            metric_id="replay",
            display_name="Replay",
            category=MeasurementCategory.ANALYSIS,
            state=CoverageState.NOT_MEASURED,
            measurement_method="m",
            environment="e",
            limitation="l",
        )


def test_a_measured_metric_below_the_floor_does_not_validate() -> None:
    with pytest.raises(ValidationError, match="partially_measured, not measured"):
        MeasurementCoverageEntry(
            metric_id="replay",
            display_name="Replay",
            category=MeasurementCategory.ANALYSIS,
            state=CoverageState.MEASURED,
            sample_count=2,
            measurement_method="m",
            environment="e",
            limitation="l",
        )


def test_a_ledger_missing_a_promised_metric_does_not_validate() -> None:
    complete = build_coverage(Path("/nonexistent"))
    partial = [entry for entry in complete.entries if entry.metric_id != "replay"]

    with pytest.raises(ValidationError, match="omits"):
        MeasurementCoverage(entries=partial)


def test_a_ledger_naming_an_unpromised_metric_does_not_validate() -> None:
    complete = build_coverage(Path("/nonexistent"))
    extra = complete.entries[0].model_copy(update={"metric_id": "invented_metric"})

    with pytest.raises(ValidationError, match="does not promise"):
        MeasurementCoverage(entries=[*complete.entries, extra])
