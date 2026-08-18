"""An approval is bound to the capability surface that justified it.

The gate already compared two identities, and both are computed from files in
this repository: the target fingerprint moves when someone edits a target
definition, and the scenario fingerprint moves when someone edits a scenario.
Neither moves when the *target* changes what it advertises.

So an upstream release could add a tool, remove one, or re-specify one that kept
its name, and every approved entry would still compare clean — while the decision
each entry records had been made against a surface that no longer existed. These
tests pin the third identity, and the distinctions it has to keep straight: a
changed surface is not a defect, an unobservable surface is not a skip, and
neither one is ever absorbed into the baseline by the gate itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from interop_lab.capabilities.model import (
    CapabilityFingerprint,
    CapabilityResolutionStatus,
    ResolvedTargetCapabilities,
    ToolDescriptor,
    describe_capability_drift,
)
from interop_lab.ci.baseline import (
    BaselineDocument,
    BaselineEntry,
    DriftPolicy,
    GateOutcome,
    GatePolicy,
    RegressionKind,
    build_baseline,
    capability_dependent,
    scenario_fingerprint,
    target_fingerprint,
)
from interop_lab.ci.gate import evaluate
from interop_lab.models.core import Classification, RunResult, TargetDefinition
from interop_lab.profiles.registry import Role, Transport
from interop_lab.scenarios.library import get_scenario
from interop_lab.scenarios.model import Scenario

#: A scenario whose applicability rests on a tool the target has to advertise.
TOOL_SCENARIO: Scenario = get_scenario("version.result_type_required")
#: One that does not: its gate is answered entirely from the target file.
DECLARED_SCENARIO: Scenario = get_scenario("wire.unknown_method")


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
    tools: dict[str, str] | None = None,
    *,
    status: CapabilityResolutionStatus = CapabilityResolutionStatus.RESOLVED,
    negotiated: str = "2026-07-28",
    server_capabilities: dict[str, Any] | None = None,
) -> ResolvedTargetCapabilities:
    """A resolved record advertising ``{tool name: input schema hash}``."""
    return ResolvedTargetCapabilities(
        target_id="t",
        status=status,
        protocol_profile_negotiated=negotiated,
        server_capabilities=server_capabilities or {"tools": {}},
        tools=[
            ToolDescriptor(name=name, input_schema_sha256=digest)
            for name, digest in sorted((tools or {"echo": "a" * 64}).items())
        ],
    )


def make_run(
    *,
    scenario: Scenario = TOOL_SCENARIO,
    classification: Classification = Classification.PASS,
    resolved: ResolvedTargetCapabilities | None = None,
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
        resolved_target=make_target(),
        resolved_capabilities=resolved if resolved is not None else capabilities(),
    )


def make_baseline(
    *,
    scenario: Scenario = TOOL_SCENARIO,
    approved: ResolvedTargetCapabilities | None = None,
    capability_required: bool | None = None,
    policy: GatePolicy | None = None,
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
                capability_required=(
                    capability_dependent(scenario)
                    if capability_required is None
                    else capability_required
                ),
                applicable=True,
                allowed_classification=Classification.PASS,
            )
        ],
    )


def run_gate(
    baseline: BaselineDocument,
    run: RunResult,
    scenario: Scenario = TOOL_SCENARIO,
) -> Any:
    return evaluate(
        baseline=baseline,
        runs=[(run, scenario, make_target())],
        artifacts_root=Path("artifacts"),
    )


def kinds(result: Any) -> set[RegressionKind]:
    return {regression.kind for regression in result.regressions}


# --------------------------------------------------------- unchanged surface


def test_an_unchanged_surface_passes_and_is_counted_as_unchanged() -> None:
    result = run_gate(make_baseline(), make_run())

    assert result.outcome is GateOutcome.PASSED
    assert RegressionKind.CAPABILITY_DRIFT not in kinds(result)
    assert result.unchanged == 1


def test_a_reordered_tool_list_is_not_drift() -> None:
    """A server may list its tools in any order; the surface is the same."""
    approved = capabilities({"echo": "a" * 64, "add": "b" * 64})
    observed = capabilities({"add": "b" * 64, "echo": "a" * 64})

    result = run_gate(make_baseline(approved=approved), make_run(resolved=observed))

    assert result.outcome is GateOutcome.PASSED


# ------------------------------------------------------------ what changed


def test_a_tool_addition_blocks_and_names_the_tool() -> None:
    result = run_gate(
        make_baseline(approved=capabilities({"echo": "a" * 64})),
        make_run(resolved=capabilities({"echo": "a" * 64, "add": "b" * 64})),
    )

    assert result.outcome is GateOutcome.REGRESSION
    drift = next(r for r in result.regressions if r.kind is RegressionKind.CAPABILITY_DRIFT)
    assert "tool added: add" in drift.detail
    # A capability change is not a defect, and the gate must not imply it is.
    assert "not by itself a defect" in drift.detail


def test_a_tool_removal_blocks_and_names_the_tool() -> None:
    result = run_gate(
        make_baseline(approved=capabilities({"echo": "a" * 64, "add": "b" * 64})),
        make_run(resolved=capabilities({"echo": "a" * 64})),
    )

    drift = next(r for r in result.regressions if r.kind is RegressionKind.CAPABILITY_DRIFT)
    assert "tool removed: add" in drift.detail


def test_the_same_tool_name_with_a_changed_schema_is_drift() -> None:
    """The change a tool-name comparison cannot see, and the reason for all this."""
    result = run_gate(
        make_baseline(approved=capabilities({"echo": "a" * 64})),
        make_run(resolved=capabilities({"echo": "c" * 64})),
    )

    assert result.outcome is GateOutcome.REGRESSION
    drift = next(r for r in result.regressions if r.kind is RegressionKind.CAPABILITY_DRIFT)
    assert "input schema changed for tool echo" in drift.detail
    # Reported once, as one change — not as a removal plus an addition.
    assert "tool added" not in drift.detail
    assert "tool removed" not in drift.detail


def test_a_changed_negotiated_profile_is_drift() -> None:
    result = run_gate(
        make_baseline(approved=capabilities(negotiated="2026-07-28")),
        make_run(resolved=capabilities(negotiated="2025-11-25")),
    )

    drift = next(r for r in result.regressions if r.kind is RegressionKind.CAPABILITY_DRIFT)
    assert "negotiated profile changed: 2026-07-28 -> 2025-11-25" in drift.detail


def test_a_changed_server_capability_is_drift() -> None:
    result = run_gate(
        make_baseline(approved=capabilities(server_capabilities={"tools": {}})),
        make_run(resolved=capabilities(server_capabilities={"tools": {}, "resources": {}})),
    )

    drift = next(r for r in result.regressions if r.kind is RegressionKind.CAPABILITY_DRIFT)
    assert "server capability added: resources" in drift.detail


def test_the_drift_description_covers_every_part_of_the_surface() -> None:
    """Including output schemas and transport features, which no test above moves."""
    approved = CapabilityFingerprint(
        negotiated_profile="2026-07-28",
        tool_names=["echo"],
        tool_schema_hashes={"echo": "a" * 64},
        tool_output_schema_hashes={"echo": "o" * 64},
        transport_features={"session_identifier_issued": True},
    )
    observed = CapabilityFingerprint(
        negotiated_profile="2026-07-28",
        tool_names=["echo"],
        tool_schema_hashes={"echo": "a" * 64},
        tool_output_schema_hashes={"echo": "p" * 64},
        transport_features={"session_identifier_issued": False},
    )

    changes = describe_capability_drift(approved, observed)

    assert "output schema changed for tool echo" in changes
    assert any("transport feature changed: session_identifier_issued" in c for c in changes)


# ---------------------------------------------------------------- policy


def test_a_warning_policy_keeps_the_gate_green_and_still_reports_the_drift() -> None:
    """A warning that is not printed is not a warning."""
    result = run_gate(
        make_baseline(
            approved=capabilities({"echo": "a" * 64}),
            policy=GatePolicy(capability_drift=DriftPolicy.WARN),
        ),
        make_run(resolved=capabilities({"echo": "a" * 64, "add": "b" * 64})),
    )

    assert result.outcome is GateOutcome.PASSED
    drift = next(r for r in result.regressions if r.kind is RegressionKind.CAPABILITY_DRIFT)
    assert drift.blocking is False
    assert "tool added: add" in drift.detail


def test_capability_drift_blocks_by_default() -> None:
    """The default has to be the safe one: nobody knows what the result means yet."""
    assert GatePolicy().capability_drift is DriftPolicy.FAIL


# ------------------------------------------------------- unresolved capabilities


def test_failed_discovery_on_a_capability_dependent_entry_blocks() -> None:
    """Not NOT_APPLICABLE, not PASS, and not the previously approved record."""
    result = run_gate(
        make_baseline(capability_required=True),
        make_run(
            resolved=ResolvedTargetCapabilities(
                target_id="t",
                status=CapabilityResolutionStatus.DISCOVERY_FAILED,
                status_detail="the target never answered",
            )
        ),
    )

    assert result.outcome is GateOutcome.REGRESSION
    unresolved = next(
        r for r in result.regressions if r.kind is RegressionKind.CAPABILITY_UNRESOLVED
    )
    assert unresolved.blocking is True
    assert "discovery_failed" in unresolved.detail
    # The failure must not be dressed up as a skip or reuse the old observation.
    assert RegressionKind.SILENTLY_SKIPPED not in kinds(result)
    assert "not reused" in unresolved.detail


def test_failed_discovery_is_never_reported_as_a_target_defect() -> None:
    result = run_gate(
        make_baseline(capability_required=True),
        make_run(
            resolved=ResolvedTargetCapabilities(
                target_id="t", status=CapabilityResolutionStatus.CONTAINMENT_UNUSABLE
            )
        ),
    )

    assert RegressionKind.NEW_FAILURE not in kinds(result)
    for regression in result.regressions:
        assert regression.kind is not RegressionKind.CLASSIFICATION_CHANGED


def test_an_entry_that_never_depended_on_observation_tolerates_failed_discovery() -> None:
    """Its applicability came from the target file, which a run always has."""
    baseline = BaselineDocument(
        baseline_id="b",
        created_at=datetime.now(UTC).isoformat(),
        created_reason="test",
        created_by="test",
        suite="s",
        suite_fingerprint="f",
        entries=[
            BaselineEntry(
                target_id="t",
                target_fingerprint=target_fingerprint(make_target()),
                scenario_id=DECLARED_SCENARIO.scenario_id,
                scenario_version="1",
                scenario_fingerprint=scenario_fingerprint(DECLARED_SCENARIO),
                capability_required=False,
                applicable=True,
                allowed_classification=Classification.PASS,
            )
        ],
    )
    run = make_run(
        scenario=DECLARED_SCENARIO,
        resolved=ResolvedTargetCapabilities(
            target_id="t", status=CapabilityResolutionStatus.DISCOVERY_FAILED
        ),
    )

    result = evaluate(
        baseline=baseline,
        runs=[(run, DECLARED_SCENARIO, make_target())],
        artifacts_root=Path("artifacts"),
    )

    assert result.outcome is GateOutcome.PASSED


# ------------------------------------------------------- integrity and approval


def test_a_run_whose_record_disagrees_with_its_digest_is_a_harness_failure() -> None:
    """Never an observation about the target: something rewrote the record.

    The model refuses this combination at construction, so a record cannot be
    *built* in this state — which is why the run is assembled first and then
    mutated. That is the shape the gate's own check exists for: everything
    between the executor and the gate holds live objects, and validation happened
    before any of it ran.
    """
    run = make_run()
    assert run.resolved_capabilities is not None
    run.resolved_capabilities.capability_fingerprint = "0" * 64

    result = run_gate(make_baseline(), run)

    assert result.outcome is GateOutcome.HARNESS_FAILURE
    assert result.harness_failures


def test_baseline_creation_records_the_surface_it_approved() -> None:
    resolved = capabilities({"echo": "a" * 64})
    document = build_baseline(
        baseline_id="b",
        reason="test",
        created_by="test",
        suite="s",
        scenarios=[TOOL_SCENARIO],
        targets=[make_target()],
        runs=[(make_run(resolved=resolved), TOOL_SCENARIO, make_target())],
    )

    entry = document.entries[0]
    assert entry.capability_fingerprint == resolved.fingerprint().digest
    assert entry.approved_capability_surface is not None
    assert entry.capability_required is True


def test_a_gate_run_never_updates_the_approved_fingerprint() -> None:
    """Absorbing drift because it happened is how a gate stops detecting drift."""
    baseline = make_baseline(approved=capabilities({"echo": "a" * 64}))
    approved_before = baseline.entries[0].capability_fingerprint

    run_gate(baseline, make_run(resolved=capabilities({"echo": "c" * 64})))

    assert baseline.entries[0].capability_fingerprint == approved_before


def test_capability_dependence_is_read_from_the_scenario_not_guessed() -> None:
    assert capability_dependent(TOOL_SCENARIO) is True
    assert capability_dependent(DECLARED_SCENARIO) is False


def test_an_approved_declarative_skip_needs_no_capability_record() -> None:
    """A scenario the executor skipped before touching the target has none.

    Transport, profile and role are decided from the target definition, so a
    scenario restricted to another transport cannot become applicable no matter
    what the target advertises — and paying for a discovery run to learn that
    would be wasted work on every target in the matrix. Those runs carry no
    capability record by design, and the gate demanding one reported a missing
    observation nobody needed to make.
    """
    target = make_target()
    baseline = BaselineDocument(
        baseline_id="b",
        created_at=datetime.now(UTC).isoformat(),
        created_reason="test",
        created_by="test",
        suite="s",
        suite_fingerprint="f",
        entries=[
            BaselineEntry(
                target_id="t",
                target_fingerprint=target_fingerprint(target),
                scenario_id=TOOL_SCENARIO.scenario_id,
                scenario_version="1",
                scenario_fingerprint=scenario_fingerprint(TOOL_SCENARIO),
                # Approved as not applicable, and still capability-dependent —
                # which is the combination that used to fail.
                capability_required=True,
                applicable=False,
                allowed_classification=Classification.NOT_APPLICABLE,
            )
        ],
    )
    run = make_run(classification=Classification.NOT_APPLICABLE, resolved=None)
    # The executor returns early for a declarative skip, so there is no record.
    run.resolved_capabilities = None

    result = run_gate(baseline, run)

    assert result.outcome is GateOutcome.PASSED
    assert RegressionKind.CAPABILITY_UNRESOLVED not in kinds(result)


def test_a_scenario_that_became_skipped_is_still_a_coverage_loss() -> None:
    """The distinction the exemption above must not blur."""
    baseline = make_baseline()  # applicable=True
    run = make_run(classification=Classification.NOT_APPLICABLE)
    run.not_applicable_reason = "target no longer advertises the tool"

    result = run_gate(baseline, run)

    assert RegressionKind.SILENTLY_SKIPPED in kinds(result)
