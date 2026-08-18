"""What counts as depending on an observation, and what an unresolved run does.

Three declarations can only be answered by talking to the target, and the
previous rule recognised one of them. The consequence was quiet: a
safe-invocation baseline and a session-lifecycle baseline both recorded
``capability_required: false``, so the gate skipped the capability comparison for
exactly the entries whose applicability had been decided by observing the server.
A run that failed discovery could then be compared against an approval whose
conditions it had not reproduced.

The other half is what an undecided result does. ``PASS -> INCONCLUSIVE`` is a
loss of coverage, not a pass, and ``NOT_APPLICABLE -> INCONCLUSIVE`` is a loss of
coverage too whenever the original skip rested on an observation — but not when
it rested on the transport, which no amount of discovery could change.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from interop_lab.capabilities.model import (
    CapabilityResolutionStatus,
    ObservedTransportFeatures,
    ResolvedTargetCapabilities,
    ToolDescriptor,
)
from interop_lab.ci.baseline import (
    BaselineDocument,
    BaselineEntry,
    GateOutcome,
    GatePolicy,
    GateResult,
    InconclusivePolicy,
    RegressionKind,
    capability_dependent,
    requires_observed_surface,
    scenario_fingerprint,
    target_fingerprint,
)
from interop_lab.ci.gate import evaluate
from interop_lab.models.core import (
    Applicability,
    Classification,
    ResultDimensions,
    RunResult,
    SecurityCategory,
    TargetDefinition,
)
from interop_lab.profiles.registry import Role, Transport
from interop_lab.scenarios.library import get_scenario
from interop_lab.scenarios.model import Scenario

TOOL_SCENARIO = get_scenario("version.result_type_required")
SAFE_CALL_SCENARIO = get_scenario("baseline.safe_tool_call")
DECLARATIVE_SCENARIO = get_scenario("wire.unknown_method")


def make_target(target_id: str = "t") -> TargetDefinition:
    return TargetDefinition(
        target_id=target_id,
        role=Role.FIXTURE,
        implementation="fixture",
        language="python",
        version="1.0",
        launch_command=["${PYTHON}", "fixtures/x.py"],
        transport=Transport.STDIO,
        protocol_profile="mcp-2026-07-28",
    )


def capabilities(
    *,
    status: CapabilityResolutionStatus = CapabilityResolutionStatus.RESOLVED,
    session_issued: bool | None = True,
) -> ResolvedTargetCapabilities:
    features = ObservedTransportFeatures(
        transport="stdio",
        initialization_completed=True,
        session_identifier_issued=session_issued,
    )
    return ResolvedTargetCapabilities(
        target_id="t",
        status=status,
        protocol_profile_negotiated="2026-07-28",
        server_capabilities={"tools": {}},
        tools=[ToolDescriptor(name="echo", input_schema_sha256="a" * 64)],
        observed_transport_features=features,
        transport_features=features.as_fingerprint_material(),
    )


def unresolved() -> ResolvedTargetCapabilities:
    return ResolvedTargetCapabilities(
        target_id="t",
        status=CapabilityResolutionStatus.DISCOVERY_FAILED,
        status_detail="the target closed the connection during initialize",
    )


def make_run(
    *,
    scenario: Scenario,
    classification: Classification = Classification.PASS,
    resolved: ResolvedTargetCapabilities | None = None,
    dimensions: ResultDimensions | None = None,
) -> RunResult:
    now = datetime.now(UTC).isoformat()
    return RunResult(
        run_id=f"{scenario.scenario_id}__t__deadbeef",
        scenario_id=scenario.scenario_id,
        scenario_version="1",
        target_id="t",
        protocol_profile="mcp-2026-07-28",
        spec_version="2026-07-28",
        transport=Transport.STDIO,
        seed=0,
        started_at=now,
        finished_at=now,
        duration_ms=1.0,
        classification=classification,
        dimensions=dimensions or ResultDimensions(),
        resolved_target=make_target(),
        resolved_capabilities=resolved if resolved is not None else capabilities(),
    )


def make_baseline(
    *,
    scenario: Scenario,
    allowed: Classification = Classification.PASS,
    applicable: bool = True,
    allowed_dimensions: ResultDimensions | None = None,
    policy: GatePolicy | None = None,
    approved: ResolvedTargetCapabilities | None = None,
) -> BaselineDocument:
    target = make_target()
    surface = (approved if approved is not None else capabilities()).fingerprint()
    return BaselineDocument(
        baseline_id="b",
        created_at=datetime.now(UTC).isoformat(),
        created_reason="test",
        created_by="test",
        suite="s",
        suite_fingerprint="f",
        policy=policy or GatePolicy(),
        entries=[
            BaselineEntry(
                target_id=target.target_id,
                target_fingerprint=target_fingerprint(target),
                scenario_id=scenario.scenario_id,
                scenario_version="1",
                scenario_fingerprint=scenario_fingerprint(scenario),
                capability_fingerprint=surface.digest,
                approved_capability_surface=surface,
                capability_required=capability_dependent(scenario),
                applicable=applicable,
                allowed_classification=allowed,
                allowed_dimensions=allowed_dimensions or ResultDimensions(),
            )
        ],
    )


def run_gate(baseline: BaselineDocument, run: RunResult, scenario: Scenario) -> GateResult:
    return evaluate(
        baseline=baseline,
        runs=[(run, scenario, make_target())],
        artifacts_root=Path("artifacts"),
    )


def kinds(result: GateResult) -> set[RegressionKind]:
    return {regression.kind for regression in result.regressions}


# ------------------------------------------------------- what the rule covers


def test_all_three_observed_declarations_require_a_surface() -> None:
    assert requires_observed_surface(Applicability(requires_tools=["echo"])) is True
    assert requires_observed_surface(Applicability(requires_safe_tool_invocation=True)) is True
    assert (
        requires_observed_surface(
            Applicability(requires_transport_features={"session_identifier_issued": True})
        )
        is True
    )


def test_a_declarative_gate_needs_no_capability_discovery() -> None:
    """Transport, profile and role are answered from the target file alone."""
    declarative = Applicability(
        transports=[Transport.STREAMABLE_HTTP],
        profiles=["mcp-2026-07-28"],
        roles=[Role.SERVER],
        min_spec_version="2025-11-25",
    )

    assert requires_observed_surface(declarative) is False
    assert capability_dependent(DECLARATIVE_SCENARIO) is False


def test_the_safe_invocation_scenario_in_the_library_is_capability_dependent() -> None:
    """The regression this rule was widened for, asserted on the real scenario."""
    assert SAFE_CALL_SCENARIO.definition.applicability.requires_safe_tool_invocation is True
    assert capability_dependent(SAFE_CALL_SCENARIO) is True


# --------------------------------------- an unresolved surface is not a verdict


@pytest.mark.parametrize(
    "scenario", [TOOL_SCENARIO, SAFE_CALL_SCENARIO], ids=lambda s: s.scenario_id
)
def test_an_unresolved_surface_blocks_a_capability_dependent_entry(scenario: Scenario) -> None:
    """Neither a pass nor a skip: the approved conditions were not reproduced."""
    result = run_gate(
        make_baseline(scenario=scenario),
        make_run(scenario=scenario, classification=Classification.PASS, resolved=unresolved()),
        scenario,
    )

    assert result.outcome is GateOutcome.REGRESSION
    assert RegressionKind.CAPABILITY_UNRESOLVED in kinds(result)
    problem = next(r for r in result.regressions if r.kind is RegressionKind.CAPABILITY_UNRESOLVED)
    assert "not reused" in problem.detail
    assert result.unchanged == 0


def test_a_previous_capability_record_is_never_reused_after_failed_discovery() -> None:
    """The approved surface stays in the baseline and is not read as this run's."""
    approved = capabilities()
    baseline = make_baseline(scenario=SAFE_CALL_SCENARIO, approved=approved)
    entry = baseline.entries[0]

    result = run_gate(
        baseline,
        make_run(scenario=SAFE_CALL_SCENARIO, resolved=unresolved()),
        SAFE_CALL_SCENARIO,
    )

    # The entry is untouched, and no capability comparison reported agreement.
    assert entry.capability_fingerprint == approved.fingerprint().digest
    assert RegressionKind.CAPABILITY_DRIFT not in kinds(result)
    assert result.outcome is GateOutcome.REGRESSION


def test_a_changed_transport_feature_is_capability_drift() -> None:
    """A server that stopped issuing session identifiers moved its surface."""
    result = run_gate(
        make_baseline(scenario=TOOL_SCENARIO, approved=capabilities(session_issued=True)),
        make_run(scenario=TOOL_SCENARIO, resolved=capabilities(session_issued=False)),
        TOOL_SCENARIO,
    )

    assert RegressionKind.CAPABILITY_DRIFT in kinds(result)
    drift = next(r for r in result.regressions if r.kind is RegressionKind.CAPABILITY_DRIFT)
    assert "session_identifier_issued" in drift.detail


# ------------------------------------------------------- the inconclusive gate


def test_pass_to_inconclusive_blocks_by_default() -> None:
    result = run_gate(
        make_baseline(scenario=DECLARATIVE_SCENARIO, allowed=Classification.PASS),
        make_run(scenario=DECLARATIVE_SCENARIO, classification=Classification.INCONCLUSIVE),
        DECLARATIVE_SCENARIO,
    )

    assert result.outcome is GateOutcome.REGRESSION
    problem = next(r for r in result.regressions if r.kind is RegressionKind.INCONCLUSIVE_RESULT)
    assert problem.blocking is True
    assert "is not a pass" in problem.detail


def test_an_explicit_warn_policy_records_the_inconclusive_without_blocking() -> None:
    result = run_gate(
        make_baseline(
            scenario=DECLARATIVE_SCENARIO,
            allowed=Classification.PASS,
            policy=GatePolicy(inconclusive_result=InconclusivePolicy.WARN),
        ),
        make_run(scenario=DECLARATIVE_SCENARIO, classification=Classification.INCONCLUSIVE),
        DECLARATIVE_SCENARIO,
    )

    assert result.outcome is GateOutcome.PASSED
    problem = next(r for r in result.regressions if r.kind is RegressionKind.INCONCLUSIVE_RESULT)
    assert problem.blocking is False


def test_an_observed_skip_that_became_inconclusive_blocks() -> None:
    """The entry said "the target does not advertise this". Nobody re-established it."""
    result = run_gate(
        make_baseline(
            scenario=SAFE_CALL_SCENARIO,
            allowed=Classification.NOT_APPLICABLE,
            applicable=False,
        ),
        make_run(
            scenario=SAFE_CALL_SCENARIO,
            classification=Classification.INCONCLUSIVE,
            resolved=capabilities(),
        ),
        SAFE_CALL_SCENARIO,
    )

    assert result.outcome is GateOutcome.REGRESSION
    assert RegressionKind.INCONCLUSIVE_RESULT in kinds(result)


def test_a_declarative_skip_that_became_inconclusive_is_not_this_regression() -> None:
    """A wrong-transport skip needed no observation, so nothing was lost with one."""
    result = run_gate(
        make_baseline(
            scenario=DECLARATIVE_SCENARIO,
            allowed=Classification.NOT_APPLICABLE,
            applicable=False,
        ),
        make_run(scenario=DECLARATIVE_SCENARIO, classification=Classification.INCONCLUSIVE),
        DECLARATIVE_SCENARIO,
    )

    assert RegressionKind.INCONCLUSIVE_RESULT not in kinds(result)


# --------------------------------------------------- a new security dimension


def security_dimensions() -> ResultDimensions:
    return ResultDimensions(
        security_relevant=True,
        security_category=SecurityCategory.ORIGIN_VALIDATION,
        security_limitations=["one exchange, one build"],
    )


def test_a_newly_security_relevant_result_blocks_even_when_the_outcome_is_unchanged() -> None:
    """The case classification-only comparison cannot see."""
    result = run_gate(
        make_baseline(scenario=DECLARATIVE_SCENARIO, allowed=Classification.SPEC_VIOLATION),
        make_run(
            scenario=DECLARATIVE_SCENARIO,
            classification=Classification.SPEC_VIOLATION,
            dimensions=security_dimensions(),
        ),
        DECLARATIVE_SCENARIO,
    )

    assert result.outcome is GateOutcome.REGRESSION
    problem = next(r for r in result.regressions if r.kind is RegressionKind.NEW_SECURITY_RELEVANT)
    assert problem.observed_value is not None
    assert "origin_validation" in problem.observed_value
    # Reported as an observation needing review, never as a vulnerability.
    assert "not a vulnerability claim" in problem.detail
    assert "unconfirmed" in problem.detail


def test_an_already_approved_security_dimension_is_not_reported_again() -> None:
    result = run_gate(
        make_baseline(
            scenario=DECLARATIVE_SCENARIO,
            allowed=Classification.SPEC_VIOLATION,
            allowed_dimensions=security_dimensions(),
        ),
        make_run(
            scenario=DECLARATIVE_SCENARIO,
            classification=Classification.SPEC_VIOLATION,
            dimensions=security_dimensions(),
        ),
        DECLARATIVE_SCENARIO,
    )

    assert RegressionKind.NEW_SECURITY_RELEVANT not in kinds(result)


def test_the_security_gate_can_be_relaxed_deliberately() -> None:
    result = run_gate(
        make_baseline(
            scenario=DECLARATIVE_SCENARIO,
            allowed=Classification.SPEC_VIOLATION,
            policy=GatePolicy(fail_on_new_security_relevant_result=False),
        ),
        make_run(
            scenario=DECLARATIVE_SCENARIO,
            classification=Classification.SPEC_VIOLATION,
            dimensions=security_dimensions(),
        ),
        DECLARATIVE_SCENARIO,
    )

    problem = next(r for r in result.regressions if r.kind is RegressionKind.NEW_SECURITY_RELEVANT)
    assert problem.blocking is False
    assert result.outcome is GateOutcome.PASSED


# -------------------------------------------------- no automatic baseline edit


def test_the_gate_never_mutates_the_baseline_it_was_given() -> None:
    baseline = make_baseline(scenario=SAFE_CALL_SCENARIO)
    before = baseline.model_dump_json()

    run_gate(
        baseline, make_run(scenario=SAFE_CALL_SCENARIO, resolved=unresolved()), SAFE_CALL_SCENARIO
    )
    run_gate(
        baseline,
        make_run(
            scenario=SAFE_CALL_SCENARIO,
            classification=Classification.SPEC_VIOLATION,
            dimensions=security_dimensions(),
        ),
        SAFE_CALL_SCENARIO,
    )

    assert baseline.model_dump_json() == before
