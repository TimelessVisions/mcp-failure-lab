"""The probe-plan contract, held by both implementations at once.

There are two validators for the same document, and there have to be: the host
uses pydantic, and the probe cannot — it runs in a minimal container with no
third-party packages. Two validators are two places for a rule to live, and the
failure mode is silent: the probe accepts something the host would reject, runs
a plan nobody wrote, and produces evidence for it.

So every test here asserts against **both**, and the parity test at the bottom
compares the field lists directly.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from interop_lab.probe.plan import (
    PLAN_SCHEMA_VERSION,
    PROBE_VERSION,
    ProbeBodyMode,
    ProbeCapture,
    ProbeCaptureSource,
    ProbeCleanup,
    ProbeMethod,
    ProbePhase,
    ProbePlan,
    ProbeStep,
    ProbeSubstitution,
    ProbeSubstitutionTarget,
    ProbeVariable,
)


def _load_probe() -> Any:
    """Import the probe script by path.

    It lives under `fixtures/` and is copied into a container rather than
    installed, so there is no package to import it from. Loading it here is what
    lets the same tests exercise the code that actually runs inside the sidecar,
    instead of a copy of its rules that could drift.
    """
    path = Path("fixtures/http_probe.py")
    spec = importlib.util.spec_from_file_location("http_probe_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe_module = _load_probe()
PlanError = probe_module.PlanError


def valid_plan() -> ProbePlan:
    return ProbePlan(
        run_id="r",
        protocol_profile="mcp-2026-07-28",
        base_url="http://target:8000/",
        scenario_steps=[
            ProbeStep(
                step_id="s",
                phase=ProbePhase.SCENARIO,
                path="/mcp",
                body_mode=ProbeBodyMode.JSON,
                json_body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
        ],
    )


def as_document(plan: ProbePlan, **overrides: Any) -> dict[str, Any]:
    return {**plan.to_json_dict(), **overrides}


# ------------------------------------------------------- versions and parity


def test_the_two_implementations_agree_on_the_schema_version() -> None:
    """A probe running an older schema must not silently accept a newer plan."""
    assert probe_module.PLAN_SCHEMA_VERSION == PLAN_SCHEMA_VERSION
    assert probe_module.PROBE_VERSION == PROBE_VERSION


def test_the_two_implementations_accept_exactly_the_same_fields() -> None:
    """The parity check that makes the duplicate validator safe.

    If either side grows a member the other does not know about, the plan the
    host writes and the plan the probe runs stop being the same document.
    """
    assert set(ProbePlan.model_fields) == probe_module.PLAN_KEYS
    assert set(ProbeStep.model_fields) == probe_module.STEP_KEYS
    assert set(ProbeCapture.model_fields) == probe_module.CAPTURE_KEYS
    assert set(ProbeSubstitution.model_fields) == probe_module.SUBSTITUTION_KEYS
    assert set(ProbeVariable.model_fields) == probe_module.VARIABLE_KEYS
    assert set(ProbeCleanup.model_fields) == probe_module.CLEANUP_KEYS


def test_the_two_implementations_agree_on_every_enumeration() -> None:
    assert {m.value for m in ProbeMethod} == set(probe_module._METHODS)
    assert {p.value for p in ProbePhase} == set(probe_module._PHASES)
    assert {b.value for b in ProbeBodyMode} == set(probe_module._BODY_MODES)
    assert {s.value for s in ProbeCaptureSource} == set(probe_module._CAPTURE_SOURCES)
    assert {t.value for t in ProbeSubstitutionTarget} == set(probe_module._SUBSTITUTION_TARGETS)


# ------------------------------------------------------------ unknown fields


def test_an_unknown_plan_field_is_rejected_by_both() -> None:
    document = as_document(valid_plan(), a_future_member=1)

    with pytest.raises(ValidationError):
        ProbePlan.model_validate(document)
    with pytest.raises(PlanError) as caught:
        probe_module.validate_plan(document)
    assert "a_future_member" in str(caught.value)


def test_an_unknown_step_field_is_rejected_by_both() -> None:
    plan = valid_plan()
    document = plan.to_json_dict()
    document["scenario_steps"][0]["retry_forever"] = True

    with pytest.raises(ValidationError):
        ProbePlan.model_validate(document)
    with pytest.raises(PlanError) as caught:
        probe_module.validate_plan(document)
    assert "retry_forever" in str(caught.value)


def test_a_newer_schema_version_is_refused_by_the_probe() -> None:
    document = as_document(valid_plan(), plan_schema_version="999")
    with pytest.raises(PlanError) as caught:
        probe_module.validate_plan(document)
    assert "999" in str(caught.value)


def test_an_unknown_enum_value_is_rejected_by_both() -> None:
    document = valid_plan().to_json_dict()
    document["scenario_steps"][0]["method"] = "PATCH"

    with pytest.raises(ValidationError):
        ProbePlan.model_validate(document)
    with pytest.raises(PlanError):
        probe_module.validate_plan(document)


# --------------------------------------------------------------- invariants


def test_a_body_mode_without_its_body_is_rejected_by_both() -> None:
    document = valid_plan().to_json_dict()
    document["scenario_steps"][0]["json_body"] = None

    with pytest.raises(ValidationError):
        ProbePlan.model_validate(document)
    with pytest.raises(PlanError):
        probe_module.validate_plan(document)


def test_two_bodies_on_one_step_are_rejected_by_both() -> None:
    """Which bytes reach the wire must not depend on field order."""
    document = valid_plan().to_json_dict()
    document["scenario_steps"][0]["text_body"] = "also this"

    with pytest.raises(ValidationError):
        ProbePlan.model_validate(document)
    with pytest.raises(PlanError):
        probe_module.validate_plan(document)


def test_a_url_in_a_step_path_is_rejected_by_both() -> None:
    """A plan carrying an address would not survive replay on another machine."""
    document = valid_plan().to_json_dict()
    document["scenario_steps"][0]["path"] = "http://192.168.0.5/mcp"

    with pytest.raises(ValidationError):
        ProbePlan.model_validate(document)
    with pytest.raises(PlanError):
        probe_module.validate_plan(document)


def test_an_undeclared_variable_is_rejected_by_both() -> None:
    """A capture into an undeclared slot degrades into a silent no-op."""
    document = valid_plan().to_json_dict()
    document["scenario_steps"][0]["captures"] = [
        {"variable": "never_declared", "source": "response_header", "header": "X-Thing"}
    ]

    with pytest.raises(ValidationError):
        ProbePlan.model_validate(document)
    with pytest.raises(PlanError) as caught:
        probe_module.validate_plan(document)
    assert "never_declared" in str(caught.value)


def test_a_duplicate_step_id_is_rejected_by_both() -> None:
    """Events are keyed by step id, so a duplicate silently mixes two steps."""
    plan = valid_plan()
    document = plan.to_json_dict()
    document["scenario_steps"].append(dict(document["scenario_steps"][0]))

    with pytest.raises(ValidationError):
        ProbePlan.model_validate(document)
    with pytest.raises(PlanError):
        probe_module.validate_plan(document)


def test_a_step_in_the_wrong_phase_list_is_rejected_by_both() -> None:
    document = valid_plan().to_json_dict()
    document["scenario_steps"][0]["phase"] = "setup"

    with pytest.raises(ValidationError):
        ProbePlan.model_validate(document)
    with pytest.raises(PlanError):
        probe_module.validate_plan(document)


def test_a_capture_missing_its_selector_is_rejected_by_both() -> None:
    document = valid_plan().to_json_dict()
    document["variables"] = [{"name": "v"}]
    document["scenario_steps"][0]["captures"] = [{"variable": "v", "source": "response_header"}]

    with pytest.raises(ValidationError):
        ProbePlan.model_validate(document)
    with pytest.raises(PlanError):
        probe_module.validate_plan(document)


def test_a_valid_plan_is_accepted_by_both() -> None:
    """The negative tests above are only meaningful if the positive one holds."""
    document = valid_plan().to_json_dict()
    ProbePlan.model_validate(document)
    parsed = probe_module.validate_plan(document)
    assert parsed["scenario_steps"][0]["step_id"] == "s"
    assert parsed["plan_schema_version"] == PLAN_SCHEMA_VERSION


def test_the_probe_bounds_a_response_limit_it_is_given() -> None:
    """A plan is trusted; a target is not. The hard cap is the probe's, not the plan's."""
    document = valid_plan().to_json_dict()
    document["scenario_steps"][0]["response_body_limit_bytes"] = 1_000_000_000
    parsed = probe_module.validate_plan(document)
    assert parsed["scenario_steps"][0]["response_body_limit_bytes"] == probe_module.HARD_BODY_LIMIT


# ------------------------------------------------------------- json pointer


@pytest.mark.parametrize(
    ("pointer", "expected"),
    [
        ("", {"a": {"b": [1, 2]}}),
        ("/a", {"b": [1, 2]}),
        ("/a/b", [1, 2]),
        ("/a/b/1", 2),
    ],
)
def test_json_pointer_resolves(pointer: str, expected: Any) -> None:
    found, value = probe_module.json_pointer({"a": {"b": [1, 2]}}, pointer)
    assert found
    assert value == expected


@pytest.mark.parametrize("pointer", ["/missing", "/a/z", "/a/b/9", "not-a-pointer"])
def test_json_pointer_reports_absence_rather_than_guessing(pointer: str) -> None:
    found, _ = probe_module.json_pointer({"a": {"b": [1, 2]}}, pointer)
    assert not found


def test_json_pointer_set_refuses_to_invent_structure() -> None:
    """Creating a missing parent would mean guessing the shape of a message body."""
    document: dict[str, Any] = {"params": {}}
    assert probe_module.json_pointer_set(document, "/params/session", "abc")
    assert document["params"]["session"] == "abc"
    assert not probe_module.json_pointer_set(document, "/nope/session", "abc")
    assert "nope" not in document


def test_escaped_pointer_tokens_round_trip() -> None:
    found, value = probe_module.json_pointer({"a/b": {"c~d": 7}}, "/a~1b/c~0d")
    assert found
    assert value == 7


# ---------------------------------------------------------- the probe's main


def test_the_probe_writes_a_rejection_it_can_be_read_back_from(tmp_path: Path) -> None:
    """A refused plan still produces a result file, or the failure is invisible."""
    plan_path = tmp_path / "plan.json"
    out_dir = tmp_path / "out"
    plan_path.write_text(json.dumps(as_document(valid_plan(), whats_this=1)), encoding="utf-8")

    code = probe_module.main(["probe", "--plan", str(plan_path), "--out", str(out_dir)])
    assert code == 4
    result = json.loads((out_dir / "result.json").read_text())
    assert result["outcome"] == "plan_rejected"
    assert "whats_this" in result["detail"]
