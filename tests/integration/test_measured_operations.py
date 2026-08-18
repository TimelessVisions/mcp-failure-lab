"""The newly measured operations must actually measure something.

The failure this guards against is specific and was hit once while these were
being written: a replay whose package failed to load returned in twelve
microseconds, five times, and produced a fast, precise, entirely meaningless
median. So the assertions here are about the *shape* of a real measurement —
distinct samples, a median inside its own range, a warm-up excluded and
recorded — rather than about any particular duration, which is a property of the
machine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from interop_lab.benchmark.coverage import CoverageState, build_coverage
from interop_lab.benchmark.model import MIN_TIMING_SAMPLES, MeasurementState
from interop_lab.benchmark.operations import MeasuredOperations, measure_operations
from interop_lab.benchmark.reporting import write_json

pytestmark = [pytest.mark.integration, pytest.mark.slow]

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Every boundary this module claims to time. A metric that stopped being
#: measured would otherwise vanish from the suite along with its assertions.
EXPECTED = (
    "capability_discovery",
    "cleanup",
    "report_generation",
    "ci_gate",
    "minimization",
    "replay",
)


@pytest.fixture(scope="module")
def measured(tmp_path_factory: pytest.TempPathFactory) -> MeasuredOperations:
    return measure_operations(
        repo_root=REPO_ROOT,
        work_dir=tmp_path_factory.mktemp("operations"),
        repetitions=MIN_TIMING_SAMPLES,
        warmups=1,
    )


def test_every_claimed_boundary_is_measured(measured: MeasuredOperations) -> None:
    recorded = {operation.metric_id for operation in measured.operations}
    assert recorded == set(EXPECTED)

    for operation in measured.operations:
        assert operation.state is MeasurementState.MEASURED, (
            f"{operation.metric_id} was not measured: {operation.insufficient_reason}"
        )


def test_each_measurement_has_the_shape_of_a_real_one(measured: MeasuredOperations) -> None:
    for operation in measured.operations:
        assert len(operation.samples_ms) == MIN_TIMING_SAMPLES
        assert operation.median_ms is not None
        assert operation.min_ms is not None and operation.max_ms is not None
        # The median lies inside the range it was computed from, and every
        # sample is a positive duration. A zero here means the boundary was
        # never entered.
        assert operation.min_ms <= operation.median_ms <= operation.max_ms
        assert all(sample > 0 for sample in operation.samples_ms)


def test_the_warm_up_is_excluded_and_recorded(measured: MeasuredOperations) -> None:
    """Excluded from the statistics, visible in the artifact.

    Discarding it would make the exclusion a claim in a document rather than
    something a reader can check.
    """
    for operation in measured.operations:
        assert operation.warmup_count == 1
        assert len(operation.warmup_samples_ms) == 1
        assert operation.warmup_samples_ms[0] > 0
        assert operation.warmup_samples_ms[0] not in operation.samples_ms


def test_each_measurement_states_its_boundary_environment_and_limitation(
    measured: MeasuredOperations,
) -> None:
    for operation in measured.operations:
        assert operation.boundary
        assert operation.environment
        assert operation.limitations, f"{operation.metric_id} states no limitation"


def test_the_samples_are_not_all_identical(measured: MeasuredOperations) -> None:
    """A single repeated value is a constant, not a measurement.

    Sub-millisecond operations legitimately produce close values, so this only
    requires that they are not literally one number five times — which is what a
    cached call or a no-op would produce.
    """
    for operation in measured.operations:
        assert len(set(operation.samples_ms)) > 1, (
            f"{operation.metric_id} produced {operation.samples_ms[0]} five times, "
            "which is a constant rather than a measurement"
        )


def test_the_coverage_ledger_reads_these_measurements_back(
    measured: MeasuredOperations, tmp_path: Path
) -> None:
    """The ledger's states come from the artifact, not from a declaration."""
    write_json(tmp_path / "measured-operations.json", measured.model_dump(mode="json"))
    coverage = build_coverage(tmp_path)

    for metric_id in EXPECTED:
        entry = coverage.entry(metric_id)
        assert entry.state is CoverageState.MEASURED, entry.reason_not_measured
        assert entry.sample_count == MIN_TIMING_SAMPLES
        assert entry.artifact_reference == "measured-operations.json"

    # And the dimensions with no boundary stay absent, whatever was measured.
    assert coverage.entry("target_startup").state is CoverageState.NOT_MEASURED
    assert coverage.entry("first_valid_request").state is CoverageState.NOT_MEASURED
