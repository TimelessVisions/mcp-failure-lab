"""Baseline model, regression semantics, and report emitters."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from interop_lab.artifacts.portability import validate_payload
from interop_lab.ci.baseline import (
    EXIT_CONFIGURATION_ERROR,
    EXIT_HARNESS_FAILURE,
    EXIT_PASSED,
    EXIT_REGRESSION,
    BaselineDocument,
    BaselineEntry,
    DivergencePolicy,
    GateOutcome,
    GatePolicy,
    RegressionKind,
    scenario_fingerprint,
    target_fingerprint,
)
from interop_lab.ci.gate import evaluate
from interop_lab.models.core import (
    AssertionResult,
    AssertionStatus,
    Classification,
    RunResult,
    TargetDefinition,
)
from interop_lab.profiles.registry import Role, Transport
from interop_lab.reporting import junit, sarif
from interop_lab.scenarios.library import get_scenario

SCENARIO = get_scenario("version.result_type_required")


def make_target(target_id: str = "t", version: str = "1.0") -> TargetDefinition:
    return TargetDefinition(
        target_id=target_id,
        role=Role.FIXTURE,
        implementation="fixture",
        language="python",
        version=version,
        launch_command=["${PYTHON}", "fixtures/x.py"],
        transport=Transport.STDIO,
        protocol_profile="mcp-2026-07-28",
    )


def make_run(
    classification: Classification,
    *,
    target_id: str = "t",
    scenario_id: str = "version.result_type_required",
    failed: tuple[str, ...] = (),
    harness_error: str | None = None,
) -> RunResult:
    now = datetime.now(UTC).isoformat()
    return RunResult(
        run_id=f"{scenario_id}__{target_id}__deadbeef",
        scenario_id=scenario_id,
        scenario_version="1",
        target_id=target_id,
        protocol_profile="mcp-2026-07-28",
        spec_version="2026-07-28",
        transport=Transport.STDIO,
        seed=0,
        started_at=now,
        finished_at=now,
        duration_ms=1.0,
        classification=classification,
        harness_error=harness_error,
        trace_path=f"artifact://runs/{scenario_id}__{target_id}__deadbeef/wire.jsonl",
        resolved_target=make_target(target_id),
        assertions=[
            AssertionResult(
                assertion_id=name,
                status=AssertionStatus.FAIL,
                expected="e",
                actual="a",
                normative_source="MCP 2026-07-28 schema.json",
            )
            for name in failed
        ],
    )


def make_baseline(
    *,
    allowed: Classification = Classification.PASS,
    failed: tuple[str, ...] = (),
    applicable: bool = True,
    policy: GatePolicy | None = None,
    target: TargetDefinition | None = None,
) -> BaselineDocument:
    tgt = target or make_target()
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
                target_id=tgt.target_id,
                target_fingerprint=target_fingerprint(tgt),
                scenario_id=SCENARIO.scenario_id,
                scenario_version="1",
                scenario_fingerprint=scenario_fingerprint(SCENARIO),
                applicable=applicable,
                allowed_classification=allowed,
                expected_failed_assertions=list(failed),
            )
        ],
    )


# ------------------------------------------------------------------ schema


def test_baseline_rejects_unknown_fields() -> None:
    """An unknown field must never be silently reinterpreted."""
    payload = make_baseline().model_dump(mode="json")
    payload["mystery_field"] = True
    with pytest.raises(ValidationError):
        BaselineDocument.model_validate(payload)


def test_baseline_is_portable() -> None:
    assert not validate_payload(make_baseline().model_dump(mode="json"), context="baseline")


def test_target_fingerprint_changes_with_version() -> None:
    assert target_fingerprint(make_target(version="1.0")) != target_fingerprint(
        make_target(version="2.0")
    )


def test_scenario_fingerprint_is_stable_for_the_same_definition() -> None:
    assert scenario_fingerprint(SCENARIO) == scenario_fingerprint(SCENARIO)


def test_scenario_fingerprint_changes_when_steps_change() -> None:
    mutated = SCENARIO.with_steps(SCENARIO.steps[:0] + SCENARIO.steps)
    mutated = mutated.model_copy(
        update={"steps": [SCENARIO.steps[0].model_copy(update={"params": {"name": "other"}})]}
    )
    assert scenario_fingerprint(mutated) != scenario_fingerprint(SCENARIO)


# -------------------------------------------------------- regression semantics


@pytest.mark.parametrize(
    "observed",
    [
        Classification.SPEC_VIOLATION,
        Classification.CRASH,
        Classification.HANG,
        Classification.TIMEOUT,
        Classification.RECOVERY_FAILURE,
        Classification.CLEANUP_FAILURE,
    ],
)
def test_pass_to_failure_is_a_blocking_regression(observed: Classification) -> None:
    result = evaluate(
        baseline=make_baseline(allowed=Classification.PASS),
        runs=[(make_run(observed), SCENARIO, make_target())],
    )
    assert result.outcome is GateOutcome.REGRESSION
    assert result.exit_code == EXIT_REGRESSION
    assert result.blocking_regressions[0].kind is RegressionKind.NEW_FAILURE


def test_harness_error_is_never_a_target_verdict() -> None:
    result = evaluate(
        baseline=make_baseline(),
        runs=[
            (make_run(Classification.HARNESS_ERROR, harness_error="boom"), SCENARIO, make_target())
        ],
    )
    assert result.outcome is GateOutcome.HARNESS_FAILURE
    assert result.exit_code == EXIT_HARNESS_FAILURE
    assert result.harness_failures


def test_unchanged_result_passes() -> None:
    result = evaluate(
        baseline=make_baseline(allowed=Classification.PASS),
        runs=[(make_run(Classification.PASS), SCENARIO, make_target())],
    )
    assert result.outcome is GateOutcome.PASSED
    assert result.exit_code == EXIT_PASSED
    assert result.unchanged == 1


def test_approved_failure_stays_visible_and_passes() -> None:
    """A baselined synthetic failure is approved, not hidden."""
    result = evaluate(
        baseline=make_baseline(
            allowed=Classification.SPEC_VIOLATION, failed=("result_type_present",)
        ),
        runs=[
            (
                make_run(Classification.SPEC_VIOLATION, failed=("result_type_present",)),
                SCENARIO,
                make_target(),
            )
        ],
    )
    assert result.outcome is GateOutcome.PASSED
    assert result.unchanged == 1


def test_same_classification_different_assertions_is_a_regression() -> None:
    """Same label, different failing assertions is not the same finding."""
    result = evaluate(
        baseline=make_baseline(
            allowed=Classification.SPEC_VIOLATION, failed=("result_type_present",)
        ),
        runs=[
            (
                make_run(Classification.SPEC_VIOLATION, failed=("is_result",)),
                SCENARIO,
                make_target(),
            )
        ],
    )
    assert result.outcome is GateOutcome.REGRESSION
    assert result.blocking_regressions[0].kind is RegressionKind.SIGNATURE_CHANGED


def test_improvement_is_reported_and_does_not_mutate_the_baseline() -> None:
    baseline = make_baseline(allowed=Classification.SPEC_VIOLATION, failed=("x",))
    before = baseline.model_dump_json()
    result = evaluate(
        baseline=baseline, runs=[(make_run(Classification.PASS), SCENARIO, make_target())]
    )
    assert result.outcome is GateOutcome.PASSED
    assert result.improvements and result.improvements[0].observed_value == "PASS"
    assert baseline.model_dump_json() == before, "the gate must never rewrite a baseline"


def test_not_applicable_is_not_a_pass() -> None:
    result = evaluate(
        baseline=make_baseline(allowed=Classification.PASS, applicable=True),
        runs=[(make_run(Classification.NOT_APPLICABLE), SCENARIO, make_target())],
    )
    assert result.outcome is GateOutcome.REGRESSION
    assert result.blocking_regressions[0].kind is RegressionKind.SILENTLY_SKIPPED


def test_missing_result_is_never_a_pass() -> None:
    result = evaluate(baseline=make_baseline(), runs=[])
    assert result.outcome is GateOutcome.REGRESSION
    assert result.blocking_regressions[0].kind in (
        RegressionKind.MISSING_TARGET,
        RegressionKind.MISSING_SCENARIO,
    )


def test_unbaselined_failure_is_a_regression() -> None:
    result = evaluate(
        baseline=make_baseline(),
        runs=[
            (
                make_run(Classification.SPEC_VIOLATION, scenario_id="brand.new"),
                SCENARIO,
                make_target(),
            )
        ],
    )
    kinds = {r.kind for r in result.regressions}
    assert RegressionKind.UNKNOWN_RESULT in kinds


def test_target_drift_is_detected() -> None:
    baseline = make_baseline(target=make_target(version="1.0"))
    result = evaluate(
        baseline=baseline, runs=[(make_run(Classification.PASS), SCENARIO, make_target("t", "9.9"))]
    )
    assert any(r.kind is RegressionKind.TARGET_DRIFT for r in result.regressions)


def test_divergence_policy_is_configurable() -> None:
    for policy, expect_block in (
        (DivergencePolicy.FAIL, True),
        (DivergencePolicy.WARN, False),
    ):
        result = evaluate(
            baseline=make_baseline(policy=GatePolicy(new_behavioral_divergence=policy)),
            runs=[(make_run(Classification.BEHAVIORAL_DIVERGENCE), SCENARIO, make_target())],
        )
        assert bool(result.blocking_regressions) is expect_block


def test_exit_codes_are_documented_and_distinct() -> None:
    assert {EXIT_PASSED, EXIT_REGRESSION, EXIT_CONFIGURATION_ERROR, EXIT_HARNESS_FAILURE} == {
        0,
        1,
        2,
        3,
    }


# ---------------------------------------------------------------- junit


def test_junit_maps_outcomes_deliberately() -> None:
    runs = [
        make_run(Classification.PASS, scenario_id="a"),
        make_run(Classification.SPEC_VIOLATION, scenario_id="b", failed=("x",)),
        make_run(Classification.HARNESS_ERROR, scenario_id="c", harness_error="boom"),
        make_run(Classification.NOT_APPLICABLE, scenario_id="d"),
    ]
    root = ET.fromstring(junit.to_string(runs, suite_name="s"))
    cases = {c.attrib["name"]: c for c in root.findall(".//testcase")}

    assert cases["a"].find("failure") is None and cases["a"].find("skipped") is None
    assert cases["b"].find("failure") is not None
    assert cases["c"].find("error") is not None
    assert cases["d"].find("skipped") is not None
    # Skipped must never be counted as a pass.
    assert root.attrib["skipped"] == "1"
    assert root.attrib["failures"] == "1"
    assert root.attrib["errors"] == "1"


def test_junit_is_parseable_and_carries_evidence() -> None:
    runs = [make_run(Classification.SPEC_VIOLATION, failed=("result_type_present",))]
    root = ET.fromstring(junit.to_string(runs, suite_name="s"))
    props = {p.attrib["name"]: p.attrib["value"] for p in root.findall(".//property")}
    for key in ("target", "protocol_profile", "transport", "classification", "evidence"):
        assert key in props
    assert props["evidence"].startswith("artifact://")
    assert not validate_payload(props, context="junit")


# ---------------------------------------------------------------- sarif


def test_sarif_shape_and_stable_rule_ids() -> None:
    runs = [make_run(Classification.SPEC_VIOLATION, failed=("result_type_present",))]
    log = sarif.build(runs)
    assert log["version"] == "2.1.0"
    assert log["$schema"].endswith("sarif-schema-2.1.0.json")

    driver = log["runs"][0]["tool"]["driver"]
    assert {r["id"] for r in driver["rules"]} >= {
        "MCP-SPEC-VIOLATION",
        "MCP-CRASH",
        "MCP-HANG",
        "MCP-RECOVERY-FAILURE",
        "MCP-CLEANUP-FAILURE",
        "MCP-BEHAVIORAL-DIVERGENCE",
        "INTEROP-HARNESS-ERROR",
        "INTEROP-ENVIRONMENT-MISMATCH",
    }

    result = log["runs"][0]["results"][0]
    assert result["ruleId"] == "MCP-SPEC-VIOLATION"
    for key in (
        "scenarioId",
        "targetId",
        "protocolProfile",
        "transport",
        "classification",
        "normativeSource",
        "evidenceUri",
        "reproductionCommand",
        "limitations",
    ):
        assert key in result["properties"], key


def test_sarif_excludes_passes_and_skips() -> None:
    runs = [
        make_run(Classification.PASS, scenario_id="a"),
        make_run(Classification.NOT_APPLICABLE, scenario_id="b"),
        make_run(Classification.CRASH, scenario_id="c"),
    ]
    log = sarif.build(runs)
    assert len(log["runs"][0]["results"]) == 1
    assert log["runs"][0]["results"][0]["ruleId"] == "MCP-CRASH"


def test_sarif_uses_relative_uris_only() -> None:
    runs = [make_run(Classification.SPEC_VIOLATION, failed=("x",))]
    log = sarif.build(runs)
    payload = json.loads(json.dumps(log))
    # The schema URL is a legitimate https reference; paths must be relative.
    uri = payload["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"][
        "uri"
    ]
    assert not uri.startswith("/")
    assert "\\" not in uri
    assert not validate_payload(payload["runs"][0]["results"], context="sarif")
