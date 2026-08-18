"""The expanded battery, and the machinery the new scenarios needed.

Three things are pinned here.

**Contract quality.** Phase 11's checklist, as assertions. A scenario is a
published claim about someone else's software; one that cannot say which dated
revision it asserts, what a conforming target does, or why a difference is not a
defect has no business being in the battery.

**Composition.** The battery has to stay a *comparison*. A scenario set that
drifted into "eight things we can only ask an HTTP server" plus "eight things we
can only ask a stdio server" would produce two reports, not a matrix.

**The new gates.** Safe invocation and observed transport features both decide
applicability, and both have to fail in the right direction: a skip whose reason
nobody observed is indistinguishable from one that was invented.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from interop_lab.capabilities.invocation import SafeToolInvocation, SideEffectClass
from interop_lab.capabilities.model import (
    CapabilityResolutionStatus,
    ObservedTransportFeatures,
    ResolvedTargetCapabilities,
    ToolDescriptor,
)
from interop_lab.models.core import Classification, ScenarioNature, TargetDefinition
from interop_lab.probe.compile import SETUP_INITIALIZE, compile_plan
from interop_lab.profiles.registry import Role, Transport
from interop_lab.runner.executor import (
    bind_safe_invocation,
    safe_invocation_verdict,
    transport_feature_gap,
)
from interop_lab.scenarios.library import ALL_SCENARIOS, SUITES, get_scenario
from interop_lab.scenarios.model import AssertionKind, StepKind

PROFILE_2025 = "mcp-2025-11-25"
PROFILE_2026 = "mcp-2026-07-28"

#: Everything that was in the battery before this checkpoint. Named rather than
#: counted, so a scenario removed to make room for a new one fails loudly
#: instead of leaving the total unchanged.
PRE_EXISTING = frozenset(
    {
        "baseline.tools_list",
        "baseline.tool_call",
        "version.result_type_required",
        "wire.malformed_json",
        "wire.unknown_method",
        "wire.response_id_correlation",
        "version.unsupported_rejected",
        "version.missing_request_meta",
        "timing.response_deadline",
        "timing.dropped_request",
        "recovery.process_terminated",
        "baseline.http_tools_list",
        "version.http_header_body_mismatch",
        "version.http_header_body_mismatch_noisy",
        "timing.http_slow_body",
        "recovery.http_connection_reset",
        "wire.http_malformed_body",
        "timing.http_dropped_request",
    }
)


def external_targets_present() -> bool:
    """Whether this tree carries the pinned external target definitions.

    They are withheld from the public export, so a test that reads them has
    nothing to assert there. Skipping says that plainly; failing would report a
    defect in a harness that is working exactly as intended.
    """
    return (Path(__file__).resolve().parents[2] / "targets" / "external").is_dir()


needs_external_targets = pytest.mark.skipif(
    not external_targets_present(),
    reason="the pinned external target definitions are not present in this tree",
)


def added() -> dict[str, Any]:
    return {sid: s for sid, s in ALL_SCENARIOS.items() if sid not in PRE_EXISTING}


def profiles_of(scenario: Any) -> set[str]:
    return {str(getattr(p, "value", p)) for p in scenario.definition.applicability.profiles}


def transports_of(scenario: Any) -> set[Transport]:
    return set(scenario.definition.applicability.transports)


# ------------------------------------------------------------- composition


def test_no_pre_existing_scenario_was_removed() -> None:
    """Including the 2026-only ones, which this checkpoint was told to keep."""
    missing = sorted(PRE_EXISTING - set(ALL_SCENARIOS))
    assert not missing, f"scenarios disappeared from the battery: {missing}"


def test_the_battery_is_the_size_it_claims_to_be() -> None:
    assert 30 <= len(ALL_SCENARIOS) <= 32, len(ALL_SCENARIOS)


def test_every_new_scenario_applies_to_the_deployed_revision() -> None:
    """2025-11-25 is what is actually running; 2026-07-28 is what is coming.

    A checkpoint that added a dozen scenarios only the newer revision can answer
    would have grown the battery without growing what it can measure against the
    three implementations in the matrix, all of which speak 2025-11-25.
    """
    only_2026 = sorted(sid for sid, s in added().items() if profiles_of(s) == {PROFILE_2026})
    assert len(only_2026) <= 1, f"too many new 2026-only scenarios: {only_2026}"

    applicable_2025 = [sid for sid, s in added().items() if PROFILE_2025 in profiles_of(s)]
    assert len(applicable_2025) >= 8, applicable_2025


def test_enough_new_scenarios_reach_streamable_http() -> None:
    http = [sid for sid, s in added().items() if Transport.STREAMABLE_HTTP in transports_of(s)]
    assert len(http) >= 8, http


def test_enough_new_scenarios_mean_the_same_thing_on_both_transports() -> None:
    """The ones that make the matrix a comparison rather than two reports."""
    cross = [
        sid
        for sid, s in added().items()
        if {Transport.STDIO, Transport.STREAMABLE_HTTP} <= transports_of(s)
    ]
    assert len(cross) >= 4, cross


def test_a_cross_transport_scenario_is_one_scenario_not_two() -> None:
    """Extending a scenario across transports beats cloning it under a new id.

    Two ids measuring the same requirement cannot be compared: the baseline
    approves them separately, the gate reports them separately, and a divergence
    between the transports looks like two unrelated results.
    """
    for sid in SUITES["cross-transport"]:
        scenario = get_scenario(sid)
        assert {Transport.STDIO, Transport.STREAMABLE_HTTP} <= transports_of(scenario), sid
        # No near-duplicate hiding under an http-prefixed name.
        assert f"{sid.split('.', 1)[0]}.http_{sid.split('.', 1)[1]}" not in ALL_SCENARIOS


# ------------------------------------------------------- contract quality


def test_every_scenario_states_what_a_conforming_target_does() -> None:
    missing = sorted(sid for sid, s in ALL_SCENARIOS.items() if not s.definition.expected_outcome)
    assert not missing, f"no expected outcome recorded: {missing}"


def test_every_new_scenario_carries_its_contract() -> None:
    """ID, version, title, category, nature, purpose, profiles, transports, roles."""
    for sid, scenario in added().items():
        definition = scenario.definition
        assert definition.version, sid
        assert definition.title, sid
        assert definition.purpose, sid
        assert definition.applicability.profiles, sid
        assert definition.applicability.transports, sid
        assert definition.applicability.roles, sid
        assert scenario.steps, sid
        assert any(step.assertions for step in scenario.steps), f"{sid} asserts nothing"


def test_a_differential_scenario_says_why_it_is_not_normative() -> None:
    """Otherwise a reader cannot tell "no rule exists" from "we did not look"."""
    for sid, scenario in ALL_SCENARIOS.items():
        if scenario.definition.nature is not ScenarioNature.DIFFERENTIAL:
            continue
        assert scenario.definition.rationale, f"{sid} is differential with no rationale"


def test_a_differential_scenario_never_grades_a_target_as_violating_a_spec() -> None:
    """A comparison that can return SPEC_VIOLATION is not a comparison.

    Both of these exist because no dated source states a server obligation for
    the behaviour they measure. An assertion defaulting to SPEC_VIOLATION would
    invent one — the single error this project treats as unforgivable.
    """
    for sid, scenario in ALL_SCENARIOS.items():
        if scenario.definition.nature is not ScenarioNature.DIFFERENTIAL:
            continue
        for step in scenario.steps:
            for assertion in step.assertions:
                assert assertion.on_fail != Classification.SPEC_VIOLATION.value, (
                    f"{sid}/{assertion.assertion_id} would report a spec violation "
                    "for behaviour no dated source governs"
                )


def test_every_new_http_transport_scenario_cites_the_transport_binding() -> None:
    """Not the schema. The schema describes messages; none of these is about one."""
    for sid in SUITES["http-transport"] + SUITES["http-lifecycle"]:
        scenario = get_scenario(sid)
        citations = [scenario.definition.normative_source or ""]
        citations += [a.normative_source or "" for step in scenario.steps for a in step.assertions]
        joined = " ".join(citations)
        assert "transport binding" in joined or "lifecycle" in joined, sid


def test_a_2025_scenario_never_cites_only_a_2026_source() -> None:
    """The existing rule, restated over the scenarios this checkpoint added."""
    problems: list[str] = []
    for sid, scenario in added().items():
        if PROFILE_2025 not in profiles_of(scenario):
            continue
        citations = [scenario.definition.normative_source]
        citations += [a.normative_source for step in scenario.steps for a in step.assertions]
        for citation in citations:
            if not citation:
                continue
            covers = "2025-11-25" in citation or "JSON-RPC" in citation
            if "2026-07-28" in citation and not covers:
                problems.append(f"{sid}: {citation!r}")
    assert not problems, problems


def test_the_lifecycle_scenarios_are_confined_to_the_revision_that_has_one() -> None:
    """2026-07-28 removed `initialize`, so it cannot be asked these questions."""
    for sid in SUITES["http-lifecycle"]:
        assert profiles_of(get_scenario(sid)) == {PROFILE_2025}, sid


def test_the_get_scenario_claims_nothing_about_streaming() -> None:
    """It reads headers and stops. Saying more would overstate what it does."""
    scenario = get_scenario("transport.http_get_stream_or_405")
    step = scenario.steps[0]

    assert step.http_abort_after_headers is True
    outcome = scenario.definition.expected_outcome
    assert "Last-Event-ID" in outcome and "no event is consumed" in outcome


# --------------------------------------------------- safe tool invocation


def make_target(**overrides: Any) -> TargetDefinition:
    base: dict[str, Any] = {
        "target_id": "fixture.invocation",
        "role": Role.FIXTURE,
        "implementation": "fixture",
        "language": "python",
        "version": "1.0",
        "launch_command": ["${PYTHON}", "fixtures/x.py"],
        "transport": Transport.STDIO,
        "protocol_profile": PROFILE_2025,
        "safe_tool_invocation": SafeToolInvocation(
            tool_name="echo",
            arguments={"text": "synthetic"},
            purpose="exercise the tool-call path",
            expected_input_schema_sha256="a" * 64,
        ),
    }
    base.update(overrides)
    return TargetDefinition.model_validate(base)


def capabilities(tools: dict[str, str]) -> ResolvedTargetCapabilities:
    return ResolvedTargetCapabilities(
        target_id="fixture.invocation",
        status=CapabilityResolutionStatus.RESOLVED,
        protocol_profile_negotiated="2025-11-25",
        tools=[
            ToolDescriptor(name=name, input_schema_sha256=digest)
            for name, digest in sorted(tools.items())
        ],
    )


def test_an_approved_invocation_confirmed_by_the_surface_may_run() -> None:
    verdict = safe_invocation_verdict(make_target(), capabilities({"echo": "a" * 64}))
    assert verdict.blocked is False


def test_a_target_with_no_approved_invocation_is_not_applicable() -> None:
    """No call was reviewed here. That is a fact about this repository."""
    verdict = safe_invocation_verdict(
        make_target(safe_tool_invocation=None), capabilities({"echo": "a" * 64})
    )
    assert verdict.blocked is True
    assert verdict.classification is Classification.NOT_APPLICABLE
    assert "does not synthesise a call" in verdict.reason


def test_a_tool_absent_from_the_observed_surface_is_not_applicable() -> None:
    """The reason was observed, so a skip may state it."""
    verdict = safe_invocation_verdict(make_target(), capabilities({"other": "a" * 64}))
    assert verdict.classification is Classification.NOT_APPLICABLE
    assert "does not advertise" in verdict.reason
    assert "other" in verdict.reason


def test_a_changed_input_schema_makes_the_scenario_unresolved_not_failed() -> None:
    """The approved arguments describe a contract the target no longer has.

    Sending them anyway would produce a failure caused by this repository and
    attributed to the server.
    """
    verdict = safe_invocation_verdict(make_target(), capabilities({"echo": "b" * 64}))
    assert verdict.classification is Classification.INCONCLUSIVE
    assert "no longer has" in verdict.reason


def test_unresolved_capabilities_never_produce_a_skip() -> None:
    """A skip would state that the target lacks the tool, and nobody looked."""
    verdict = safe_invocation_verdict(
        make_target(),
        ResolvedTargetCapabilities(
            target_id="fixture.invocation",
            status=CapabilityResolutionStatus.DISCOVERY_FAILED,
        ),
    )
    assert verdict.classification is Classification.INCONCLUSIVE


def test_the_invocation_is_bound_into_the_step_before_execution() -> None:
    """So the trace, the compiled plan and the reproduction carry the real call."""
    bound = bind_safe_invocation(get_scenario("baseline.safe_tool_call"), make_target())
    step = bound.steps[0]

    assert step.params == {"name": "echo", "arguments": {"text": "synthetic"}}


def test_a_dangerous_tool_name_is_refused_at_load_time() -> None:
    """Cheap, and it catches the obvious mistake before a report rather than after."""
    for name in ("delete_file", "http_fetch", "run_shell", "get_api_key"):
        with pytest.raises(ValueError, match="not invoked by this harness"):
            SafeToolInvocation(
                tool_name=name,
                purpose="should never load",
                expected_input_schema_sha256="a" * 64,
            )


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ("/etc/passwd", "filesystem path"),
        ("https://example.test/x", "URL"),
        ("my-password-value", "credential"),
    ],
)
def test_a_non_synthetic_argument_is_refused(value: str, match: str) -> None:
    """Synthetic means it cannot name anything that exists."""
    with pytest.raises(ValueError, match=match):
        SafeToolInvocation(
            tool_name="echo",
            arguments={"text": value},
            purpose="should never load",
            expected_input_schema_sha256="a" * 64,
        )


def test_an_invocation_without_a_stated_purpose_is_refused() -> None:
    with pytest.raises(ValueError, match="states no purpose"):
        SafeToolInvocation(tool_name="echo", purpose="   ", expected_input_schema_sha256="a" * 64)


@needs_external_targets
def test_every_external_target_approves_exactly_one_safe_call() -> None:
    """The gap this closes: the tool-call baseline used to skip all three."""
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[2]
    for name in ("mcp-server-time", "server-everything", "mcp-go-everything"):
        target = TargetDefinition.model_validate(
            yaml.safe_load((root / "targets/external" / f"{name}.yaml").read_text())
        )
        invocation = target.safe_tool_invocation
        assert invocation is not None, name
        assert invocation.side_effect_class is SideEffectClass.NONE, name
        assert invocation.purpose.strip(), name
        assert len(invocation.expected_input_schema_sha256) == 64, name
        assert invocation.limitations, name


# ------------------------------------------- observed transport features


def features(**overrides: Any) -> ResolvedTargetCapabilities:
    return ResolvedTargetCapabilities(
        target_id="t",
        status=CapabilityResolutionStatus.RESOLVED,
        protocol_profile_negotiated="2025-11-25",
        observed_transport_features=ObservedTransportFeatures(**overrides),
    )


def test_a_scenario_needing_a_session_runs_where_one_was_observed() -> None:
    assert (
        transport_feature_gap(
            {"session_identifier_issued": True},
            features(session_identifier_issued=True),
        )
        is None
    )


def test_a_server_issuing_no_session_identifier_is_skipped_with_the_observed_reason() -> None:
    """2025-11-25 makes the identifier optional, so this is not a defect."""
    reason = transport_feature_gap(
        {"session_identifier_issued": True}, features(session_identifier_issued=False)
    )

    assert reason is not None
    assert "observed as False" in reason


def test_an_unobserved_transport_is_unknown_rather_than_absent() -> None:
    """ "We did not look" and "it is not there" are different statements."""
    reason = transport_feature_gap(
        {"session_identifier_issued": True},
        ResolvedTargetCapabilities(
            target_id="t", status=CapabilityResolutionStatus.DISCOVERY_FAILED
        ),
    )

    assert reason is not None
    assert "not the same as knowing it does not" in reason


def test_the_session_lifecycle_scenario_gates_on_an_observation() -> None:
    """A target file claiming "this server issues session ids" would not do.

    That is the same architectural error as `expected_tools` deciding
    applicability: a human's claim standing in for the server's behaviour.
    """
    scenario = get_scenario("lifecycle.http_request_after_delete")
    assert scenario.definition.applicability.requires_transport_features == {
        "session_identifier_issued": True
    }


# ------------------------------------------------ transport-level HTTP steps


def http_target() -> TargetDefinition:
    return TargetDefinition.model_validate(
        {
            "target_id": "http.fixture",
            "role": Role.FIXTURE,
            "implementation": "fixture",
            "language": "python",
            "version": "1.0",
            "launch_command": ["${PYTHON}", "fixtures/http_server.py"],
            "transport": Transport.STREAMABLE_HTTP,
            "protocol_profile": PROFILE_2025,
        }
    )


def test_a_get_step_compiles_to_a_get_that_stops_after_headers() -> None:
    compiled = compile_plan(
        get_scenario("transport.http_get_stream_or_405"), http_target(), run_id="r"
    )
    step = compiled.plan.scenario_steps[0]

    assert step.method.value == "GET"
    assert step.abort.value == "after_headers"
    assert step.body_mode.value == "none"


def test_a_delete_step_carries_the_captured_session_identifier() -> None:
    """Otherwise the DELETE asks the server to terminate an unnamed session."""
    compiled = compile_plan(
        get_scenario("lifecycle.http_request_after_delete"), http_target(), run_id="r"
    )
    delete = compiled.plan.scenario_steps[0]

    assert delete.method.value == "DELETE"
    assert [s.header for s in delete.substitutions] == ["Mcp-Session-Id"]


def test_suppressing_the_handshake_removes_setup_and_session_substitution() -> None:
    """A scenario about an uninitialized server must not be handed a session.

    Both halves matter: setup would answer the question before it was asked, and
    a substitution with nothing captured would send an empty header rather than
    none.
    """
    scenario = get_scenario("lifecycle.http_request_before_initialize")
    assert scenario.suppress_profile_handshake is True

    compiled = compile_plan(scenario, http_target(), run_id="r")

    assert compiled.plan.setup_steps == []
    assert compiled.plan.cleanup.steps == []
    assert all(step.substitutions == [] for step in compiled.plan.scenario_steps)


def test_an_ordinary_scenario_still_gets_its_handshake() -> None:
    compiled = compile_plan(get_scenario("baseline.http_tools_list"), http_target(), run_id="r")

    assert next(s.step_id for s in compiled.plan.setup_steps) == SETUP_INITIALIZE


def test_a_raw_request_integrity_payload_reaches_the_wire_byte_exact() -> None:
    """A JSON round trip anywhere would repair the very defect under test."""
    scenario = get_scenario("wire.missing_method")
    compiled = compile_plan(scenario, http_target(), run_id="r")
    step = compiled.plan.scenario_steps[0]

    assert step.body_mode.value == "text"
    assert step.text_body == scenario.steps[0].raw_payload
    assert step.json_body is None


def test_the_request_integrity_payloads_are_valid_json() -> None:
    """The whole point. An unparseable payload would be testing -32700 instead."""
    import json

    for sid in SUITES["request-integrity"]:
        step = get_scenario(sid).steps[0]
        assert step.kind is StepKind.RAW, sid
        assert step.raw_payload is not None
        json.loads(step.raw_payload)  # raises if this is not valid JSON


def test_the_notification_scenario_excludes_transport_acknowledgement() -> None:
    """An HTTP 202 is not a JSON-RPC response, and must not be graded as one."""
    scenario = get_scenario("wire.notification_no_response")
    kinds = {a.kind for step in scenario.steps for a in step.assertions}

    assert AssertionKind.NO_JSONRPC_RESPONSE in kinds
    assert AssertionKind.NO_RESPONSE not in kinds
    assert "202" in scenario.definition.expected_outcome
