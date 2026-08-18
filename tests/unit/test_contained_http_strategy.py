"""The contained-HTTP strategy's decision logic, without a container runtime.

The end-to-end path is exercised for real in
`tests/integration/test_sidecar_containment.py` and by the CLI runs in CI. What
is tested here is the reasoning applied to what comes back — which is where the
damaging mistakes live, because every one of them produces a plausible-looking
artifact:

* grading a setup response as though it answered a scenario step,
* calling a run usable evidence when the probe's own containment was weaker
  than requested,
* letting a leaked container disappear from the verdict.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from interop_lab.containment.model import ContainmentOutcome
from interop_lab.containment.sidecar import CleanupReport, SidecarResult
from interop_lab.models.core import AssertionStatus, Classification, TargetDefinition, TimeoutCause
from interop_lab.probe.compile import compile_plan
from interop_lab.probe.events import build_trace
from interop_lab.runner.contained_http import ContainedHttpPlanStrategy, process_outcome_for
from interop_lab.scenarios.library import get_scenario

CONTAINED_2026 = "targets/http/fixture-2026-contained.yaml"
CONTAINED_2025 = "targets/http/fixture-2025-contained.yaml"
STARTED = datetime(2026, 1, 1, tzinfo=UTC)


def load(path: str) -> TargetDefinition:
    return TargetDefinition.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def response(step_id: str, *, phase: str = "scenario", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "seq": 1,
        "at_ms": 5.0,
        "direction": "inbound",
        "phase": phase,
        "step_id": step_id,
        "method": "POST",
        "path": "/mcp",
        "status": 200,
        "headers": {"Content-Type": "application/json"},
        "message": {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"tools": [], "resultType": "complete"},
        },
        "body_bytes": 40,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------- grading


def test_a_conforming_response_grades_every_assertion_as_passing() -> None:
    strategy = ContainedHttpPlanStrategy()
    scenario = get_scenario("baseline.http_tools_list")
    compiled = compile_plan(scenario, load(CONTAINED_2026), run_id="r")
    trace = build_trace("r", [response("list")], started_at=STARTED)

    assertions, timed_out, cause, expected = strategy.grade(scenario, compiled, trace)

    assert [a.status for a in assertions] == [AssertionStatus.PASS] * 3
    assert not timed_out and cause is None and not expected


def test_a_setup_response_is_never_graded_as_a_scenario_step() -> None:
    """The most plausible-looking wrong answer this layer could give.

    A handshake reply is a well-formed result. Graded against a scenario
    assertion it would pass, and the run would report a scenario that never
    received an answer as though it had.
    """
    strategy = ContainedHttpPlanStrategy()
    scenario = get_scenario("baseline.http_tools_list")
    compiled = compile_plan(scenario, load(CONTAINED_2025), run_id="r")
    trace = build_trace("r", [response("__setup_initialize", phase="setup")], started_at=STARTED)

    assertions, timed_out, _, _ = strategy.grade(scenario, compiled, trace)

    assert timed_out, "a scenario step with no response is not a pass"
    assert all(a.status is not AssertionStatus.PASS for a in assertions), assertions


def test_a_step_with_no_response_is_attributed_to_the_target() -> None:
    strategy = ContainedHttpPlanStrategy()
    scenario = get_scenario("baseline.http_tools_list")
    compiled = compile_plan(scenario, load(CONTAINED_2026), run_id="r")
    trace = build_trace(
        "r",
        [
            {
                "seq": 1,
                "at_ms": 1.0,
                "direction": "lifecycle",
                "phase": "scenario",
                "step_id": "list",
                "timeout_cause": "target_no_response",
            }
        ],
        started_at=STARTED,
    )

    _, timed_out, cause, _ = strategy.grade(scenario, compiled, trace)
    assert timed_out
    assert cause is TimeoutCause.TARGET_NO_RESPONSE


def test_a_harness_dropped_request_is_not_blamed_on_the_target() -> None:
    """The negative control. A blameless target must not be charged for our fault."""
    strategy = ContainedHttpPlanStrategy()
    scenario = get_scenario("timing.http_dropped_request")
    compiled = compile_plan(scenario, load(CONTAINED_2026), run_id="r")
    assert compiled.dropped_steps, "the fault should have removed the step from the plan"

    trace = build_trace("r", [], started_at=STARTED)
    assertions, timed_out, cause, expected = strategy.grade(scenario, compiled, trace)

    assert cause is TimeoutCause.HARNESS_DEADLINE
    # The scenario declares the timeout expected, so it is a decided observation
    # rather than an aborted run.
    assert expected and not timed_out
    assert [a.status for a in assertions] == [AssertionStatus.PASS]


def test_a_transport_error_is_attributed_as_no_response_from_the_target() -> None:
    strategy = ContainedHttpPlanStrategy()
    scenario = get_scenario("recovery.http_connection_reset")
    compiled = compile_plan(scenario, load(CONTAINED_2026), run_id="r")
    trace = build_trace(
        "r",
        [
            {
                "seq": 1,
                "at_ms": 1.0,
                "direction": "lifecycle",
                "phase": "scenario",
                "step_id": "reset",
                "transport_error": "ConnectionError: peer closed the connection",
            }
        ],
        started_at=STARTED,
    )

    _, _, cause, _ = strategy.grade(scenario, compiled, trace)
    assert cause is TimeoutCause.TARGET_NO_RESPONSE


def test_grading_stops_at_the_first_unexpected_timeout() -> None:
    """A hung target invalidates every later step, exactly as on the host path."""
    strategy = ContainedHttpPlanStrategy()
    scenario = get_scenario("wire.http_malformed_body")
    compiled = compile_plan(scenario, load(CONTAINED_2026), run_id="r")
    trace = build_trace(
        "r",
        [
            {
                "seq": 1,
                "at_ms": 1.0,
                "direction": "lifecycle",
                "phase": "scenario",
                "step_id": "malformed",
                "timeout_cause": "target_no_response",
            }
        ],
        started_at=STARTED,
    )

    assertions, timed_out, _, _ = strategy.grade(scenario, compiled, trace)
    assert timed_out
    # Only the first step's assertion was evaluated; the recovery step is not
    # graded against a target already known to be silent.
    assert [a.assertion_id for a in assertions] == ["parse_error_returned"]


def test_a_response_is_matched_by_step_id_not_by_position() -> None:
    """Positional matching would shift every later assertion onto the wrong event."""
    strategy = ContainedHttpPlanStrategy()
    scenario = get_scenario("recovery.http_connection_reset")
    compiled = compile_plan(scenario, load(CONTAINED_2026), run_id="r")
    # Only the *second* step answered.
    trace = build_trace("r", [response("after_reset")], started_at=STARTED)

    assertions, _, _, _ = strategy.grade(scenario, compiled, trace)
    first, second = assertions
    assert first.assertion_id == "reset_observed"
    assert first.status is not AssertionStatus.PASS
    assert second.assertion_id == "recovers"
    assert second.status is AssertionStatus.PASS


# ----------------------------------------------------------------- containment


def sidecar_result(**overrides: Any) -> SidecarResult:
    base: dict[str, Any] = {"outcome": "completed", "detail": ""}
    base.update(overrides)
    return SidecarResult(**base)


def test_a_clean_run_is_recorded_as_contained() -> None:
    strategy = ContainedHttpPlanStrategy()
    record = strategy.containment_from(load(CONTAINED_2026), sidecar_result())

    assert record.outcome is ContainmentOutcome.CONTAINED
    assert record.is_usable_evidence
    assert record.requested is not None
    assert record.stated_limits, "a contained run must state what it does not protect against"


def test_a_weak_probe_makes_the_whole_run_unusable_evidence() -> None:
    """The probe handles untrusted output. A weaker probe is a weaker boundary.

    Recording the target as `contained` while the probe ran as root would
    describe an isolation the run did not have — and it is the probe, not the
    target, that the untrusted bytes are parsed in.
    """
    strategy = ContainedHttpPlanStrategy()
    record = strategy.containment_from(
        load(CONTAINED_2026),
        sidecar_result(
            probe_weaknesses=["probe user requested '65534:65534', container runs as ''"]
        ),
    )

    assert record.outcome is ContainmentOutcome.CONTAINMENT_WEAKER_THAN_REQUESTED
    assert not record.is_usable_evidence
    assert any("probe user" in weakness for weakness in record.weaknesses)


def test_a_weak_target_makes_the_run_unusable_evidence() -> None:
    strategy = ContainedHttpPlanStrategy()
    record = strategy.containment_from(
        load(CONTAINED_2026), sidecar_result(target_weaknesses=["target published port 8000"])
    )
    assert record.outcome is ContainmentOutcome.CONTAINMENT_WEAKER_THAN_REQUESTED
    assert not record.is_usable_evidence


def test_a_failed_containment_is_never_a_target_verdict() -> None:
    strategy = ContainedHttpPlanStrategy()
    record = strategy.containment_from(
        load(CONTAINED_2026),
        sidecar_result(outcome="containment_failed", detail="network already exists"),
    )
    assert record.outcome is ContainmentOutcome.CONTAINMENT_FAILED
    assert not record.is_usable_evidence
    assert record.unavailable_reason is not None


# --------------------------------------------------------------------- cleanup


def test_a_clean_cleanup_leaves_the_process_outcome_untouched() -> None:
    outcome = process_outcome_for(sidecar_result())
    assert outcome.cleanup_ok is True
    assert outcome.cleanup_detail is None


def test_a_leak_reaches_the_process_outcome_the_classifier_reads() -> None:
    outcome = process_outcome_for(
        sidecar_result(cleanup=CleanupReport(leaked=["network interop-lab-net-x survived removal"]))
    )
    assert outcome.cleanup_ok is False
    assert "interop-lab-net-x" in (outcome.cleanup_detail or "")


# ----------------------------------------------------------- docker unavailable


def test_no_docker_is_a_harness_error_and_never_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rule the whole containment layer rests on, on the contained HTTP path.

    A contained HTTP target has no host path to fall back to even in principle,
    so there is nothing to be tempted by — but the outcome still has to be an
    environment failure rather than an inconclusive shrug.
    """
    import interop_lab.runner.contained_http as module

    async def _unavailable() -> tuple[bool, str, None]:
        return False, "the docker daemon is not reachable", None

    monkeypatch.setattr(module, "docker_probe", _unavailable)

    from interop_lab.profiles.registry import get_profile
    from interop_lab.runner.executor import ScenarioExecutor

    executor = ScenarioExecutor(artifacts_dir=Path("artifacts"))
    strategy = ContainedHttpPlanStrategy()
    scenario = get_scenario("baseline.http_tools_list")
    target = load(CONTAINED_2026)

    import asyncio

    outcome = asyncio.run(
        strategy.execute(
            scenario,
            target,
            run_id="r",
            profile=get_profile(target.protocol_profile),
            context=executor.context,
        )
    )

    assert outcome.containment is not None
    assert outcome.containment.outcome is ContainmentOutcome.CONTAINMENT_UNAVAILABLE
    assert not outcome.containment.is_usable_evidence
    assert outcome.assertions == [], "nothing may be graded without containment"
    assert outcome.harness_error is not None
    # And the executor turns that into HARNESS_ERROR rather than PASS.
    result, _ = asyncio.run(executor.run(scenario, target, persist=False))
    assert result.classification is Classification.HARNESS_ERROR
