"""Integration tests against real subprocess targets.

These launch actual processes. They are the tests that would have caught the two
harness defects found while building the vertical slice: the mis-classification
of tolerated silence, and a minimiser that drifted to a different defect.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from interop_lab.config.loader import load_target
from interop_lab.differential.compare import compare
from interop_lab.minimizer.reduce import Minimizer, signature_of
from interop_lab.models.core import AssertionStatus, Classification, DifferentialVerdict
from interop_lab.runner.executor import ScenarioExecutor
from interop_lab.scenarios.library import get_scenario

pytestmark = pytest.mark.integration

REFERENCE = Path("targets/fixtures/reference.yaml")
FAULTY_RESULT_TYPE = Path("targets/fixtures/faulty-result-type.yaml")
FAULTY_WRONG_ID = Path("targets/fixtures/faulty-wrong-id.yaml")
SLOW = Path("targets/fixtures/slow.yaml")


@pytest.fixture
def executor(tmp_path: Path) -> ScenarioExecutor:
    return ScenarioExecutor(artifacts_dir=tmp_path, seed=0)


async def test_reference_fixture_passes_baseline(executor: ScenarioExecutor) -> None:
    result, trace = await executor.run(get_scenario("baseline.tool_call"), load_target(REFERENCE))
    assert result.classification is Classification.PASS
    assert trace.responses(), "a passing run must have captured at least one response"


async def test_wire_capture_records_both_directions(executor: ScenarioExecutor) -> None:
    _, trace = await executor.run(get_scenario("baseline.tools_list"), load_target(REFERENCE))
    directions = {event.direction.value for event in trace.protocol_events()}
    assert directions == {"outbound", "inbound"}
    assert all(event.raw_sha256 for event in trace.protocol_events())


async def test_missing_result_type_is_detected(executor: ScenarioExecutor) -> None:
    result, _ = await executor.run(
        get_scenario("version.result_type_required"), load_target(FAULTY_RESULT_TYPE)
    )
    assert result.classification is Classification.SPEC_VIOLATION
    failed = [a for a in result.assertions if a.status is AssertionStatus.FAIL]
    assert [a.assertion_id for a in failed] == ["result_type_present"]
    assert failed[0].normative_source


async def test_wrong_response_id_is_detected(executor: ScenarioExecutor) -> None:
    """A response carrying the wrong id never correlates, so the request times out."""
    result, _ = await executor.run(
        get_scenario("wire.response_id_correlation"), load_target(FAULTY_WRONG_ID)
    )
    assert result.classification is not Classification.PASS


async def test_slow_target_exceeds_deadline(executor: ScenarioExecutor) -> None:
    result, _ = await executor.run(get_scenario("timing.response_deadline"), load_target(SLOW))
    assert result.classification in (Classification.HANG, Classification.TIMEOUT)
    assert result.timeout_cause is not None


async def test_dropped_request_is_attributed_to_the_injected_fault(
    executor: ScenarioExecutor,
) -> None:
    """Negative control: the harness must not blame a blameless target."""
    result, _ = await executor.run(get_scenario("timing.dropped_request"), load_target(REFERENCE))
    assert result.classification is Classification.PASS
    assert "drop_first_request" in result.faults_applied


async def test_malformed_json_does_not_wedge_the_reference_fixture(
    executor: ScenarioExecutor,
) -> None:
    result, _ = await executor.run(get_scenario("wire.malformed_json"), load_target(REFERENCE))
    assert result.classification is Classification.PASS
    recovery = [a for a in result.assertions if a.assertion_id == "recovers"]
    assert recovery and recovery[0].status is AssertionStatus.PASS


async def test_process_is_reaped_after_every_run(executor: ScenarioExecutor) -> None:
    result, _ = await executor.run(get_scenario("baseline.tools_list"), load_target(REFERENCE))
    assert result.process.cleanup_ok
    assert result.process.exit_code is not None


async def test_terminated_process_is_detected(executor: ScenarioExecutor) -> None:
    result, _ = await executor.run(
        get_scenario("recovery.process_terminated"), load_target(REFERENCE)
    )
    assert result.classification is Classification.PASS
    assert result.process.cleanup_ok


async def test_differential_finds_the_first_divergent_field(executor: ScenarioExecutor) -> None:
    scenario = get_scenario("version.result_type_required")
    good_run, good_trace = await executor.run(scenario, load_target(REFERENCE))
    bad_run, bad_trace = await executor.run(scenario, load_target(FAULTY_RESULT_TYPE))

    diff = compare(good_run, good_trace, bad_run, bad_trace)
    assert diff.verdict is DifferentialVerdict.NORMATIVE_DIVERGENCE
    assert diff.meaningful_differences
    paths = {d.field_path for d in diff.meaningful_differences}
    assert "result.resultType" in paths


async def test_identical_targets_are_equivalent(executor: ScenarioExecutor) -> None:
    """A target compared against itself must never show a meaningful divergence."""
    scenario = get_scenario("baseline.tool_call")
    left_run, left_trace = await executor.run(scenario, load_target(REFERENCE))
    right_run, right_trace = await executor.run(scenario, load_target(REFERENCE))

    diff = compare(left_run, left_trace, right_run, right_trace)
    assert not diff.meaningful_differences
    assert diff.verdict in (
        DifferentialVerdict.EQUIVALENT,
        DifferentialVerdict.EXPECTED_DIFFERENCE,
    )


async def test_minimization_preserves_the_original_failure(executor: ScenarioExecutor) -> None:
    """Regression test for a real defect: reduction must not drift to another bug.

    Dropping `params.name` also yields SPEC_VIOLATION, but via "unknown tool"
    rather than the missing `resultType`. Matching on the classification alone
    accepted that reduction and produced a reproduction of the wrong defect.
    """
    scenario = get_scenario("version.result_type_required")
    target = load_target(FAULTY_RESULT_TYPE)

    baseline, _ = await executor.run(scenario, target)
    original_signature = signature_of(baseline)
    assert original_signature[1] == ("result_type_present",)

    reduced, report = await Minimizer(executor, target).minimize(scenario, baseline.classification)
    final, _ = await executor.run(reduced, target)

    assert signature_of(final) == original_signature
    assert report.stable
    assert report.stability_runs == 3
    assert report.minimized_mutation_surface_bytes <= report.original_mutation_surface_bytes
    # This scenario has removable payload, so reduction must actually happen.
    assert report.reduced


async def test_not_applicable_is_not_a_pass(executor: ScenarioExecutor) -> None:
    """A 2026-only scenario against a 2025 target must be skipped, never passed."""
    target = load_target(Path("targets/typescript/sdk-server.yaml"))
    result, _ = await executor.run(get_scenario("version.result_type_required"), target)
    assert result.classification is Classification.NOT_APPLICABLE
    assert result.not_applicable_reason


async def test_faults_do_not_fire_during_setup(executor: ScenarioExecutor) -> None:
    """Regression: fault occurrence indices must exclude the handshake.

    On a stateful profile the `initialize` exchange is the first outbound
    traffic. When faults were armed during setup, `occurrence: 0` injected into
    the handshake instead of the scenario's first request, so the same fault
    definition hit a different message depending on the dated profile — and the
    failure was then attributed to the wrong step.
    """
    stateful = load_target(Path("targets/python/sdk-server-2025.yaml"))
    result, trace = await executor.run(get_scenario("timing.dropped_request"), stateful)

    assert result.classification is Classification.PASS
    assert result.faults_applied == ["drop_first_request"]

    # The handshake must have completed untouched.
    methods = [e.rpc_method for e in trace.protocol_events() if e.rpc_method]
    assert "initialize" in methods
    handshake = [e for e in trace.protocol_events() if e.rpc_method == "initialize"]
    assert all(e.fault_applied is None for e in handshake)


async def test_dead_target_write_is_not_a_harness_error(executor: ScenarioExecutor) -> None:
    """Writing to an exited target is a fact about the target, not a harness bug."""
    stateful = load_target(Path("targets/python/sdk-server-2025.yaml"))
    result, _ = await executor.run(get_scenario("recovery.process_terminated"), stateful)
    assert result.classification is not Classification.HARNESS_ERROR
    assert result.harness_error is None
