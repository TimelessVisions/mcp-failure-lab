"""Compiling a scenario into a probe plan.

Two properties matter most here, and both are the kind that break quietly:

* **Setup traffic must not consume a fault's occurrence index.** A stateful
  profile sends a handshake before the scenario; a stateless one does not. If
  occurrence counting spanned both, the same fault definition would hit the
  handshake on one profile and the first scenario request on the other, and the
  two runs would silently be testing different things.
* **Raw payloads must reach the wire byte for byte.** A JSON round trip anywhere
  between the scenario and the socket repairs a deliberately truncated body, and
  the scenario then measures nothing.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import yaml

from interop_lab.injection.faults import Boundary, Fault, FaultKind, FaultTrigger
from interop_lab.models.core import TargetDefinition
from interop_lab.probe.compile import (
    SESSION_ID_HEADER,
    SESSION_ID_VARIABLE,
    PlanCompilationError,
    compile_plan,
)
from interop_lab.probe.plan import ProbeBodyMode, ProbeMethod, ProbePhase
from interop_lab.scenarios.library import get_scenario
from interop_lab.scenarios.model import AssertionKind, AssertionSpec, Scenario, Step, StepKind

CONTAINED_2026 = "targets/http/fixture-2026-contained.yaml"
CONTAINED_2025 = "targets/http/fixture-2025-contained.yaml"


def load(path: str) -> TargetDefinition:
    return TargetDefinition.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def one_step_scenario(faults: list[Fault] | None = None) -> Scenario:
    """A scenario whose first scenario request is the one a fault should hit."""
    base = get_scenario("baseline.http_tools_list")
    return base.model_copy(update={"faults": faults or []})


# ------------------------------------------------------- setup versus scenario


def test_a_stateless_profile_gets_no_setup_traffic() -> None:
    """`initialize` was removed by the 2026 revision; sending one would be wrong."""
    compiled = compile_plan(one_step_scenario(), load(CONTAINED_2026), run_id="r")
    assert compiled.plan.setup_steps == []
    assert [s.step_id for s in compiled.plan.scenario_steps] == ["list"]


def test_a_stateful_profile_gets_a_handshake_before_the_scenario() -> None:
    compiled = compile_plan(one_step_scenario(), load(CONTAINED_2025), run_id="r")
    assert [s.step_id for s in compiled.plan.setup_steps] == [
        "__setup_initialize",
        "__setup_initialized",
    ]
    assert all(s.phase is ProbePhase.SETUP for s in compiled.plan.setup_steps)
    assert all(s.phase is ProbePhase.SCENARIO for s in compiled.plan.scenario_steps)


def test_the_handshake_captures_a_session_id_without_requiring_one() -> None:
    """2025-11-25 says a server MAY issue one. Requiring it would fabricate a failure."""
    compiled = compile_plan(one_step_scenario(), load(CONTAINED_2025), run_id="r")
    initialize = compiled.plan.setup_steps[0]

    (capture,) = initialize.captures
    assert capture.variable == SESSION_ID_VARIABLE
    assert capture.header == SESSION_ID_HEADER
    assert capture.required is False

    (declared,) = compiled.plan.variables
    assert declared.name == SESSION_ID_VARIABLE
    assert declared.required is False


def test_scenario_traffic_carries_the_captured_session_id_forward() -> None:
    compiled = compile_plan(one_step_scenario(), load(CONTAINED_2025), run_id="r")
    (substitution,) = compiled.plan.scenario_steps[0].substitutions
    assert substitution.variable == SESSION_ID_VARIABLE
    assert substitution.header == SESSION_ID_HEADER
    assert substitution.required is False


def test_a_stateful_plan_releases_the_session_on_cleanup() -> None:
    compiled = compile_plan(one_step_scenario(), load(CONTAINED_2025), run_id="r")
    (teardown,) = compiled.plan.cleanup.steps
    assert teardown.method is ProbeMethod.DELETE
    assert teardown.phase is ProbePhase.CLEANUP
    assert compiled.plan.cleanup.close_connections is True


def test_a_stateless_plan_has_nothing_to_release() -> None:
    compiled = compile_plan(one_step_scenario(), load(CONTAINED_2026), run_id="r")
    assert compiled.plan.cleanup.steps == []


# ------------------------------------------------------- fault attribution
#
# The regression this section exists for: a handshake consuming `occurrence: 0`.


def drop_first_scenario_request() -> Fault:
    return Fault(
        fault_id="drop_first",
        boundary=Boundary.OUTBOUND_MESSAGE,
        kind=FaultKind.DROP,
        trigger=FaultTrigger(occurrence=0),
    )


@pytest.mark.parametrize("path", [CONTAINED_2026, CONTAINED_2025])
def test_occurrence_zero_means_the_first_scenario_request_on_both_profiles(path: str) -> None:
    """The same fault definition reaches the same semantic event under both profiles.

    On 2025 the plan contains a two-step handshake first. If occurrence counting
    included it, this fault would land on the `initialize` request instead — the
    scenario would go out untouched, the target would answer normally, and the
    run would report a passing scenario that never ran as written.
    """
    compiled = compile_plan(
        one_step_scenario([drop_first_scenario_request()]), load(path), run_id="r"
    )

    # The scenario request was dropped...
    assert compiled.dropped_steps == {"list": "drop_first"}
    assert compiled.faults_applied == ["drop_first"]
    assert compiled.plan.scenario_steps == []

    # ...and the handshake, where present, went out untouched.
    assert all(step.body_mode is ProbeBodyMode.JSON for step in compiled.plan.setup_steps)
    assert all(step.delay_before_ms == 0 for step in compiled.plan.setup_steps)


def test_a_fault_on_a_later_occurrence_skips_the_earlier_scenario_step() -> None:
    scenario = get_scenario("recovery.http_connection_reset").model_copy(
        update={
            "faults": [
                Fault(
                    fault_id="drop_second",
                    boundary=Boundary.OUTBOUND_MESSAGE,
                    kind=FaultKind.DROP,
                    trigger=FaultTrigger(occurrence=1),
                )
            ]
        }
    )
    compiled = compile_plan(scenario, load(CONTAINED_2026), run_id="r")
    assert list(compiled.dropped_steps) == ["after_reset"]
    assert [s.step_id for s in compiled.plan.scenario_steps] == ["reset"]


def test_a_delay_fault_becomes_a_delay_on_the_step_it_targets() -> None:
    scenario = one_step_scenario(
        [
            Fault(
                fault_id="slow_first",
                boundary=Boundary.OUTBOUND_MESSAGE,
                kind=FaultKind.DELAY,
                delay_ms=250,
                trigger=FaultTrigger(occurrence=0),
            )
        ]
    )
    compiled = compile_plan(scenario, load(CONTAINED_2025), run_id="r")
    assert compiled.plan.scenario_steps[0].delay_before_ms == 250.0
    assert compiled.fault_by_step == {"list": "slow_first"}
    # The handshake is untouched, so the delay measures the scenario and not
    # the runtime's cold start.
    assert all(step.delay_before_ms == 0 for step in compiled.plan.setup_steps)


def test_a_corrupting_fault_produces_exact_bytes_not_re_encoded_json() -> None:
    scenario = one_step_scenario(
        [
            Fault(
                fault_id="corrupt",
                boundary=Boundary.OUTBOUND_MESSAGE,
                kind=FaultKind.CORRUPT_JSON,
                replacement='{"jsonrpc":"2.0","id":1,',
                trigger=FaultTrigger(occurrence=0),
            )
        ]
    )
    compiled = compile_plan(scenario, load(CONTAINED_2026), run_id="r")
    step = compiled.plan.scenario_steps[0]
    assert step.body_mode is ProbeBodyMode.TEXT
    assert step.text_body == '{"jsonrpc":"2.0","id":1,'
    assert step.json_body is None


def test_a_truncating_fault_survives_as_raw_bytes() -> None:
    scenario = one_step_scenario(
        [
            Fault(
                fault_id="truncate",
                boundary=Boundary.OUTBOUND_MESSAGE,
                kind=FaultKind.TRUNCATE,
                keep_bytes=12,
                trigger=FaultTrigger(occurrence=0),
            )
        ]
    )
    compiled = compile_plan(scenario, load(CONTAINED_2026), run_id="r")
    step = compiled.plan.scenario_steps[0]
    assert step.body_mode is ProbeBodyMode.BASE64
    assert step.base64_body is not None
    assert len(base64.b64decode(step.base64_body)) == 12


@pytest.mark.parametrize("kind", [FaultKind.TERMINATE_PROCESS, FaultKind.DUPLICATE])
def test_a_fault_the_plan_cannot_express_is_refused_not_dropped(kind: FaultKind) -> None:
    """Silently ignoring it would run a scenario without the condition under test."""
    scenario = one_step_scenario(
        [
            Fault(
                fault_id="unsupported",
                boundary=Boundary.OUTBOUND_MESSAGE,
                kind=kind,
                trigger=FaultTrigger(occurrence=0),
            )
        ]
    )
    with pytest.raises(PlanCompilationError):
        compile_plan(scenario, load(CONTAINED_2026), run_id="r")


# ------------------------------------------------------------- raw payloads


def test_a_raw_step_is_carried_as_exact_text_never_re_serialised() -> None:
    """A JSON round trip would repair the truncation the scenario is testing."""
    payload = '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{'
    scenario = get_scenario("wire.http_malformed_body")
    compiled = compile_plan(scenario, load(CONTAINED_2026), run_id="r")

    malformed = next(s for s in compiled.plan.scenario_steps if s.step_id == "malformed")
    assert malformed.body_mode is ProbeBodyMode.TEXT
    assert malformed.text_body == payload
    assert malformed.json_body is None
    # And it is still exactly those bytes after serialisation to the plan file.
    document = json.loads(json.dumps(compiled.plan.to_json_dict()))
    written = next(s for s in document["scenario_steps"] if s["step_id"] == "malformed")
    assert written["text_body"] == payload


def test_a_step_waiting_for_an_unsolicited_message_is_refused() -> None:
    """Streamable HTTP has no such channel here, and SSE is not implemented."""
    scenario = get_scenario("baseline.http_tools_list").model_copy(
        update={
            "steps": [
                Step(
                    step_id="wait",
                    kind=StepKind.EXPECT_MESSAGE,
                    optional=False,
                    assertions=[
                        AssertionSpec(assertion_id="a", kind=AssertionKind.RESPONSE_IS_RESULT)
                    ],
                )
            ]
        }
    )
    with pytest.raises(PlanCompilationError) as caught:
        compile_plan(scenario, load(CONTAINED_2026), run_id="r")
    assert "SSE" in str(caught.value)


# ------------------------------------------------------------------ headers


def test_routing_headers_are_derived_from_the_body() -> None:
    """Derived, so they agree by construction and a mismatch must be asked for."""
    compiled = compile_plan(one_step_scenario(), load(CONTAINED_2026), run_id="r")
    headers = compiled.plan.scenario_steps[0].headers
    assert headers["Mcp-Method"] == "tools/list"
    assert headers["MCP-Protocol-Version"] == "2026-07-28"


def test_a_scenario_header_override_wins_over_the_derived_header() -> None:
    """The header/body mismatch scenario is only a test if the override survives."""
    compiled = compile_plan(
        get_scenario("version.http_header_body_mismatch"), load(CONTAINED_2026), run_id="r"
    )
    headers = compiled.plan.scenario_steps[0].headers
    assert headers["Mcp-Method"] == "tools/call"
    assert compiled.plan.scenario_steps[0].json_body is not None
    assert compiled.plan.scenario_steps[0].json_body["method"] == "tools/list"


def test_request_ids_are_recorded_so_a_response_can_be_correlated() -> None:
    compiled = compile_plan(one_step_scenario(), load(CONTAINED_2026), run_id="r")
    assert compiled.request_ids["list"] == 1
    # On a stateful profile the handshake takes id 1, so the scenario takes 2 —
    # the same numbering the interactive path produces.
    stateful = compile_plan(one_step_scenario(), load(CONTAINED_2025), run_id="r")
    assert stateful.request_ids["list"] == 2
