"""Version awareness: the gate that stops false positives across spec revisions."""

from __future__ import annotations

import pytest

from interop_lab.models.core import Applicability
from interop_lab.profiles.registry import (
    PROFILE_2025_11_25,
    PROFILE_2026_07_28,
    Lifecycle,
    Role,
    Transport,
    get_profile,
)
from interop_lab.scenarios.library import ALL_SCENARIOS
from interop_lab.transports.client import build_request


def test_profiles_differ_on_lifecycle() -> None:
    assert PROFILE_2025_11_25.lifecycle is Lifecycle.STATEFUL_INITIALIZE
    assert PROFILE_2026_07_28.lifecycle is Lifecycle.STATELESS_SELF_DESCRIBING
    assert PROFILE_2025_11_25.requires_initialize
    assert not PROFILE_2026_07_28.requires_initialize


def test_2026_removes_initialize_and_adds_discover() -> None:
    assert "initialize" in PROFILE_2026_07_28.removed_methods
    assert "server/discover" in PROFILE_2026_07_28.added_methods


def test_unknown_profile_fails_loudly() -> None:
    with pytest.raises(KeyError, match="unknown protocol profile"):
        get_profile("mcp-1999-01-01")


def test_2026_request_carries_required_meta() -> None:
    request = build_request(PROFILE_2026_07_28, rpc_id=1, method="tools/list")
    meta = request["params"]["_meta"]
    assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
    assert "io.modelcontextprotocol/clientCapabilities" in meta


def test_2025_request_does_not_invent_a_meta_envelope() -> None:
    """Sending 2026-only members to a 2025 target would be a harness-authored defect."""
    request = build_request(PROFILE_2025_11_25, rpc_id=1, method="tools/list")
    assert "_meta" not in request.get("params", {})


def test_scenario_restricted_to_2026_is_not_applicable_to_2025() -> None:
    applicability = Applicability(profiles=["mcp-2026-07-28"], min_spec_version="2026-07-28")
    reason = applicability.reason_not_applicable(
        spec_version="2025-11-25",
        profile_id="mcp-2025-11-25",
        transport=Transport.STDIO,
        role=Role.SERVER,
        available_tools=[],
    )
    assert reason is not None


def test_missing_tool_marks_scenario_not_applicable() -> None:
    applicability = Applicability(requires_tools=["slow_echo"])
    reason = applicability.reason_not_applicable(
        spec_version="2026-07-28",
        profile_id="mcp-2026-07-28",
        transport=Transport.STDIO,
        role=Role.SERVER,
        available_tools=["echo"],
    )
    assert reason is not None and "slow_echo" in reason


def test_result_type_scenario_is_gated_to_2026_only() -> None:
    """The single most important gate: asserting resultType against a 2025 target
    would be a false positive, because that schema has no such member."""
    scenario = ALL_SCENARIOS["version.result_type_required"]
    assert scenario.definition.applicability.profiles == ["mcp-2026-07-28"]
    reason = scenario.definition.applicability.reason_not_applicable(
        spec_version="2025-11-25",
        profile_id="mcp-2025-11-25",
        transport=Transport.STDIO,
        role=Role.SERVER,
        available_tools=["echo"],
    )
    assert reason is not None


def test_every_scenario_declares_applicability_and_purpose() -> None:
    for scenario_id, scenario in ALL_SCENARIOS.items():
        definition = scenario.definition
        assert definition.purpose, f"{scenario_id} has no stated purpose"
        assert definition.applicability.profiles, f"{scenario_id} does not gate on a profile"
        assert scenario.steps, f"{scenario_id} has no steps"


def test_normative_scenarios_cite_a_source() -> None:
    """A normative claim without a citation is an opinion."""
    for scenario_id, scenario in ALL_SCENARIOS.items():
        if scenario.definition.nature.value != "normative":
            continue
        assert scenario.definition.normative_source, f"{scenario_id} asserts without citation"


def test_a_citation_covers_every_profile_the_scenario_asserts_against() -> None:
    """Rule 1, at the level of the citation rather than the assertion.

    A scenario applicable to 2025-11-25 that cites only the 2026-07-28 schema is
    asserting a document that does not govern the target being measured. This
    was a real defect: `wire.unknown_method` demanded -32601 of a 2025-11-25
    target citing the 2026 schema, which does not enumerate the JSON-RPC error
    codes at all in the 2025-11-25 revision.

    A citation is acceptable for a 2025-11-25 target when it names that revision
    or cites the JSON-RPC base specification both revisions are built on.
    """
    problems: list[str] = []
    for scenario_id, scenario in ALL_SCENARIOS.items():
        profiles = {str(getattr(p, "value", p)) for p in scenario.definition.applicability.profiles}
        if "mcp-2025-11-25" not in profiles:
            continue

        citations = [scenario.definition.normative_source]
        citations += [
            assertion.normative_source
            for step in scenario.steps
            for assertion in step.assertions
            if assertion.normative_source
        ]
        for citation in citations:
            if not citation:
                continue
            covers_2025 = "2025-11-25" in citation or "JSON-RPC" in citation
            if "2026-07-28" in citation and not covers_2025:
                problems.append(f"{scenario_id}: {citation!r}")

    assert not problems, (
        "these scenarios assert against mcp-2025-11-25 while citing only a "
        "2026-07-28 source:\n  " + "\n  ".join(problems)
    )
