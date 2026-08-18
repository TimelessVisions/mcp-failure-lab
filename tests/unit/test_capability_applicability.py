"""Applicability is decided from what a target advertises, not what it declares.

The defect these tests pin down was real and was found by pointing the harness at
external code:

* `server-everything` declared ``expected_tools: []`` while advertising thirteen
  tools. Every tool-requiring scenario was reported NOT_APPLICABLE against a
  server that could have run all of them — a silent loss of coverage that looked
  exactly like correct behaviour.
* `mcp-go-everything` declared ``sampleLLM``, which the pinned v0.58.0 example
  leaves commented out. A scenario requiring it would have been reported
  applicable, run, and failed, attributing to the server a tool that release
  does not have.

Both directions are covered below, because fixing only one of them would leave
the architecture half-corrected.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from interop_lab.capabilities.model import (
    CapabilityResolutionStatus,
    ResolvedTargetCapabilities,
    ToolDescriptor,
)
from interop_lab.capabilities.resolve import (
    apply_declared_fallback,
    capabilities_from_trace,
    discovery_scenario,
    unresolved,
)
from interop_lab.events.model import Channel, Direction, WireEvent, WireTrace
from interop_lab.models.core import (
    Classification,
    ProcessOutcome,
    ScenarioNature,
    TargetDefinition,
    ToolDeclarationPolicy,
)
from interop_lab.runner.executor import ScenarioExecutor
from interop_lab.runner.strategy import ExecutionOutcome
from interop_lab.scenarios.library import get_scenario

# --------------------------------------------------------------- helpers


def make_target(**overrides: Any) -> TargetDefinition:
    base: dict[str, Any] = {
        "target_id": "fixture.capability",
        "role": "fixture",
        "implementation": "capability test double",
        "language": "python",
        "version": "0.1.0",
        "launch_command": ["${PYTHON}", "fixtures/reference_server.py"],
        "transport": "stdio",
        "protocol_profile": "mcp-2025-11-25",
    }
    base.update(overrides)
    return TargetDefinition.model_validate(base)


def message_event(seq: int, message: dict[str, Any]) -> WireEvent:
    return WireEvent(
        seq=seq,
        monotonic_ns=seq,
        wall_clock="2026-08-13T00:00:00+00:00",
        direction=Direction.INBOUND,
        channel=Channel.STDOUT,
        transport="stdio",
        message=message,
    )


def trace_with(tools: list[str], *, negotiated: str = "2025-11-25") -> WireTrace:
    """A discovery trace as the transport would record one."""
    return WireTrace(
        run_id="discovery",
        events=[
            message_event(
                0,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "protocolVersion": negotiated,
                        "capabilities": {"tools": {"listChanged": True}},
                        "serverInfo": {"name": "double", "version": "1.0.0"},
                    },
                },
            ),
            message_event(
                1,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {
                        "tools": [
                            {"name": name, "inputSchema": {"type": "object"}} for name in tools
                        ]
                    },
                },
            ),
        ],
    )


def resolved_with(tools: list[str], target: TargetDefinition) -> ResolvedTargetCapabilities:
    return capabilities_from_trace(trace_with(tools), target)


class _InertStrategy:
    """A strategy that launches nothing and reports nothing.

    These tests are about the applicability decision, which the executor makes
    before any traffic. When a scenario *is* applicable the executor proceeds, so
    the stub has to return a well-formed empty outcome rather than refuse — a
    stub that raised would make "the scenario ran" indistinguishable from a bug.
    """

    name = "inert-stub"
    executed: bool

    def __init__(self) -> None:
        self.executed = False

    async def execute(self, *args: Any, **kwargs: Any) -> ExecutionOutcome:
        self.executed = True
        return ExecutionOutcome(
            trace=WireTrace(run_id="stub"),
            process=ProcessOutcome(exit_code=0),
            assertions=[],
        )


def run(scenario_id: str, target: TargetDefinition, capabilities: Any) -> Any:
    """Execute one scenario with capabilities supplied, and no real target."""
    executor = ScenarioExecutor(
        artifacts_dir=Path("/tmp/capability-tests"),
        strategy=_InertStrategy(),
        capabilities=capabilities,
    )
    result, _ = asyncio.run(executor.run(get_scenario(scenario_id), target, persist=False))
    return result


def ran(scenario_id: str, target: TargetDefinition, capabilities: Any) -> bool:
    """Whether the executor got past applicability and asked the strategy to run."""
    strategy = _InertStrategy()
    executor = ScenarioExecutor(
        artifacts_dir=Path("/tmp/capability-tests"),
        strategy=strategy,
        capabilities=capabilities,
    )
    asyncio.run(executor.run(get_scenario(scenario_id), target, persist=False))
    return strategy.executed


# ------------------------------------------------- parsing a discovery trace


def test_tools_are_read_from_the_trace_not_from_the_target_file() -> None:
    target = make_target(expected_tools=["declared_only"])
    record = resolved_with(["echo", "add"], target)

    assert record.status is CapabilityResolutionStatus.RESOLVED
    assert record.tool_names == ["add", "echo"]
    assert record.declared_tools == ["declared_only"]
    assert record.protocol_profile_negotiated == "2025-11-25"
    assert record.server_name == "double"


def test_a_trace_with_no_tools_list_is_a_failed_resolution_not_an_empty_one() -> None:
    """ "No tools" and "we never found out" are different claims."""
    target = make_target()
    record = capabilities_from_trace(WireTrace(run_id="discovery"), target)

    assert record.status is CapabilityResolutionStatus.DISCOVERY_FAILED
    assert record.is_usable is False


def test_schema_hashes_are_stable_across_key_ordering() -> None:
    """Otherwise every run would look like a tool-surface change."""
    target = make_target()
    first = ToolDescriptor.from_advertised(
        {"name": "echo", "inputSchema": {"a": 1, "b": 2, "type": "object"}}
    )
    second = ToolDescriptor.from_advertised(
        {"name": "echo", "inputSchema": {"type": "object", "b": 2, "a": 1}}
    )
    assert first.input_schema_sha256 == second.input_schema_sha256
    assert resolved_with(["echo"], target).tools[0].input_schema_sha256 is not None


# ------------------------------------------------- the two real-world defects


def test_an_omitted_but_observed_tool_makes_its_scenario_applicable() -> None:
    """The `server-everything` case: declared nothing, advertised everything."""
    target = make_target(expected_tools=[])
    capabilities = resolved_with(["echo"], target)

    assert ran("baseline.tool_call", target, capabilities), (
        "a scenario was skipped against a target that advertises the tool it needs, "
        "because the target file happened not to mention it"
    )


def test_a_declared_but_absent_tool_does_not_make_its_scenario_applicable() -> None:
    """The `mcp-go-everything` case: declared `sampleLLM`, does not register it."""
    target = make_target(expected_tools=["echo"])
    capabilities = resolved_with(["something_else"], target)

    result = run("baseline.tool_call", target, capabilities)

    assert result.classification is Classification.NOT_APPLICABLE
    assert result.not_applicable_reason is not None
    assert "does not advertise" in result.not_applicable_reason
    assert "something_else" in result.not_applicable_reason


def test_the_applicability_reason_names_what_was_actually_advertised() -> None:
    """A reason a reader cannot check is not much of a reason."""
    target = make_target(expected_tools=["echo"])
    result = run("baseline.tool_call", target, resolved_with(["add", "subtract"], target))

    assert result.not_applicable_reason is not None
    assert "add" in result.not_applicable_reason
    assert "subtract" in result.not_applicable_reason


# ------------------------------------------------- unresolved is not a verdict


def test_an_unresolved_prerequisite_is_inconclusive_never_not_applicable() -> None:
    """NOT_APPLICABLE would state a reason nobody observed."""
    target = make_target(expected_tools=["echo"])
    capabilities = unresolved(
        target,
        status=CapabilityResolutionStatus.DISCOVERY_FAILED,
        detail="the target never answered",
    )

    result = run("baseline.tool_call", target, capabilities)

    assert result.classification is Classification.INCONCLUSIVE
    assert result.classification is not Classification.NOT_APPLICABLE
    assert result.classification is not Classification.PASS
    assert result.harness_error is not None
    assert "capability resolution is discovery_failed" in result.harness_error


def test_failed_discovery_never_becomes_a_pass() -> None:
    target = make_target(expected_tools=["echo"])
    for status in (
        CapabilityResolutionStatus.DISCOVERY_FAILED,
        CapabilityResolutionStatus.CONTAINMENT_UNUSABLE,
        CapabilityResolutionStatus.NO_STRATEGY,
        CapabilityResolutionStatus.NOT_ATTEMPTED,
    ):
        capabilities = unresolved(target, status=status, detail="unavailable")
        result = run("baseline.tool_call", target, capabilities)
        assert result.classification is Classification.INCONCLUSIVE, status


def test_a_scenario_with_no_tool_prerequisite_is_unaffected_by_failed_discovery() -> None:
    """Discovery failing must not block scenarios that never needed it."""
    target = make_target(expected_tools=[])
    capabilities = unresolved(
        target, status=CapabilityResolutionStatus.DISCOVERY_FAILED, detail="none"
    )
    scenario = get_scenario("wire.malformed_json")
    assert not scenario.definition.applicability.requires_tools

    assert ran("wire.malformed_json", target, capabilities)


# ------------------------------------------------- capability drift is visible


def test_the_fingerprint_changes_when_the_tool_surface_changes() -> None:
    target = make_target()
    before = resolved_with(["echo"], target).fingerprint()
    after = resolved_with(["echo", "add"], target).fingerprint()

    assert before.digest != after.digest


def test_the_fingerprint_changes_when_a_tool_schema_changes_but_its_name_does_not() -> None:
    """The drift a name-only fingerprint cannot see."""
    target = make_target()
    first = capabilities_from_trace(
        WireTrace(
            run_id="d",
            events=[
                message_event(
                    0, {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-11-25"}}
                ),
                message_event(
                    1,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]},
                    },
                ),
            ],
        ),
        target,
    )
    second = capabilities_from_trace(
        WireTrace(
            run_id="d",
            events=[
                message_event(
                    0, {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-11-25"}}
                ),
                message_event(
                    1,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {
                            "tools": [
                                {
                                    "name": "echo",
                                    "inputSchema": {"type": "object", "required": ["text"]},
                                }
                            ]
                        },
                    },
                ),
            ],
        ),
        target,
    )

    assert first.tool_names == second.tool_names
    assert first.fingerprint().digest != second.fingerprint().digest


def test_the_fingerprint_is_stable_for_an_unchanged_surface() -> None:
    """A fingerprint that churns would be ignored within a week."""
    target = make_target()
    assert resolved_with(["a", "b"], target).fingerprint().digest == (
        resolved_with(["b", "a"], target).fingerprint().digest
    )


# ------------------------------------------------- declared/observed mismatch


def test_a_mismatch_is_recorded_in_both_directions_without_overwriting_either() -> None:
    target = make_target(expected_tools=["declared_only", "shared"])
    record = resolved_with(["shared", "advertised_only"], target)

    assert record.declared_but_not_advertised == ["declared_only"]
    assert record.advertised_but_not_declared == ["advertised_only"]
    assert record.has_declaration_mismatch
    # Neither side was replaced by the other.
    assert record.declared_tools == ["declared_only", "shared"]
    assert record.tool_names == ["advertised_only", "shared"]


def test_the_default_policy_warns_rather_than_failing_the_run() -> None:
    target = make_target(expected_tools=["nope"])
    assert target.tool_declaration_policy is ToolDeclarationPolicy.WARN

    assert ran("wire.malformed_json", target, resolved_with(["echo"], target))


def test_the_fail_policy_treats_a_mismatch_as_a_configuration_error() -> None:
    target = make_target(expected_tools=["nope"], tool_declaration_policy="fail")

    result = run("wire.malformed_json", target, resolved_with(["echo"], target))

    assert result.classification is Classification.HARNESS_ERROR
    assert result.harness_error is not None
    assert "target-file error" in result.harness_error


# ------------------------------------------------- the declared fallback


def test_a_local_fixture_that_cannot_be_discovered_may_fall_back() -> None:
    """A fixture that resets every connection can never complete a handshake."""
    target = make_target(expected_tools=["echo"], allow_declared_tool_fallback=True)
    failed = unresolved(target, status=CapabilityResolutionStatus.DISCOVERY_FAILED, detail="reset")

    record = apply_declared_fallback(failed, target)

    assert record.status is CapabilityResolutionStatus.DECLARED_FALLBACK
    assert record.is_usable
    assert record.tool_names == ["echo"]
    assert any("declared, not observed" in limit for limit in record.limitations)


def test_the_fallback_is_not_applied_to_a_target_that_did_not_ask_for_it() -> None:
    target = make_target(expected_tools=["echo"])
    failed = unresolved(target, status=CapabilityResolutionStatus.DISCOVERY_FAILED, detail="reset")

    assert apply_declared_fallback(failed, target) is failed


def test_the_fallback_never_overrides_a_successful_discovery() -> None:
    target = make_target(expected_tools=["declared"], allow_declared_tool_fallback=True)
    observed = resolved_with(["actually_advertised"], target)

    record = apply_declared_fallback(observed, target)

    assert record.tool_names == ["actually_advertised"]
    assert record.status is CapabilityResolutionStatus.RESOLVED


def test_an_external_target_may_never_fall_back_to_a_declaration() -> None:
    """The guarantee that makes observed resolution worth having."""
    with pytest.raises(ValueError, match="observed or the scenario is INCONCLUSIVE"):
        make_target(
            target_id="external.pretend",
            provenance="external",
            execution_mode="container",
            container={
                "image_source": {
                    "kind": "registry",
                    "repository": "example/server",
                    "repository_digest": "sha256:" + "a" * 64,
                }
            },
            allow_declared_tool_fallback=True,
        )


# ------------------------------------------------- discovery is not a test


def test_the_discovery_scenario_can_never_produce_a_verdict() -> None:
    """It has no assertions, so there is nothing for it to fail."""
    scenario = discovery_scenario()

    assert scenario.definition.nature is ScenarioNature.INFORMATIONAL
    assert sum(len(step.assertions) for step in scenario.steps) == 0


def test_the_discovery_scenario_is_not_part_of_the_battery() -> None:
    """It would otherwise appear in every count and every report."""
    from interop_lab.scenarios.library import ALL_SCENARIOS, SUITES

    assert discovery_scenario().scenario_id not in ALL_SCENARIOS
    for suite in SUITES.values():
        assert discovery_scenario().scenario_id not in suite


# ---------------------------------------- the fingerprint is stored, not derived


def test_a_resolved_record_carries_its_own_fingerprint() -> None:
    """A method is not evidence.

    The fingerprint was derivable long before it was persisted, which meant an
    artifact recorded the tool *names* an applicability decision rested on and
    nothing about the schemas behind them. A reader of a stored record could not
    tell a tool that kept its name while changing its input contract from one
    that had not changed at all.
    """
    target = make_target()
    record = resolved_with(["echo"], target)

    assert record.capability_fingerprint == record.fingerprint().digest
    # And it survives serialisation, which is the entire point.
    restored = ResolvedTargetCapabilities.model_validate(record.model_dump(mode="json"))
    assert restored.capability_fingerprint == record.capability_fingerprint


def test_an_unresolved_record_carries_no_fingerprint() -> None:
    """Hashing an empty record would turn ignorance into something citable."""
    record = unresolved(
        make_target(),
        status=CapabilityResolutionStatus.DISCOVERY_FAILED,
        detail="the target never answered",
    )

    assert record.capability_fingerprint is None


def test_a_record_whose_digest_was_edited_does_not_load() -> None:
    """A stored record and its digest cannot be allowed to disagree quietly."""
    target = make_target()
    payload = resolved_with(["echo"], target).model_dump(mode="json")
    payload["capability_fingerprint"] = "0" * 64

    with pytest.raises(ValueError, match="disagree"):
        ResolvedTargetCapabilities.model_validate(payload)


def test_an_unresolved_record_may_not_assert_a_fingerprint() -> None:
    payload = unresolved(
        make_target(),
        status=CapabilityResolutionStatus.DISCOVERY_FAILED,
        detail="nothing was observed",
    ).model_dump(mode="json")
    payload["capability_fingerprint"] = "0" * 64

    with pytest.raises(ValueError, match="never observed"):
        ResolvedTargetCapabilities.model_validate(payload)


def test_repeated_identical_discovery_produces_the_same_fingerprint() -> None:
    """Otherwise every repetition would report drift that is not drift."""
    target = make_target()
    first = resolved_with(["echo", "add"], target)
    second = resolved_with(["add", "echo"], target)

    assert first.capability_fingerprint == second.capability_fingerprint


def test_reordered_schema_keys_do_not_change_the_fingerprint() -> None:
    """A server is free to emit the same schema with its members in any order."""
    ordered = ToolDescriptor.from_advertised(
        {"name": "echo", "inputSchema": {"type": "object", "required": ["text"], "title": "Echo"}}
    )
    shuffled = ToolDescriptor.from_advertised(
        {"name": "echo", "inputSchema": {"title": "Echo", "required": ["text"], "type": "object"}}
    )

    assert ordered.input_schema_sha256 == shuffled.input_schema_sha256


def test_serverinfo_prose_does_not_change_the_fingerprint() -> None:
    """A renamed or re-versioned server advertises the same surface.

    Deliberate: the fingerprint gates applicability, and a server's own name for
    itself decides nothing about which requirements apply. Putting it here would
    make the value churn for a reason carrying no consequence.
    """
    target = make_target()

    def record_named(name: str, version: str) -> ResolvedTargetCapabilities:
        return capabilities_from_trace(
            WireTrace(
                run_id="d",
                events=[
                    message_event(
                        0,
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "result": {
                                "protocolVersion": "2025-11-25",
                                "capabilities": {"tools": {}},
                                "serverInfo": {"name": name, "version": version},
                            },
                        },
                    ),
                    message_event(
                        1,
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "result": {"tools": [{"name": "echo", "inputSchema": {}}]},
                        },
                    ),
                ],
            ),
            target,
        )

    assert (
        record_named("double", "1.0.0").capability_fingerprint
        == record_named("renamed", "9.9.9").capability_fingerprint
    )


def test_the_negotiated_profile_changes_the_fingerprint() -> None:
    """It decides which dated requirements apply, so it is part of the identity."""
    target = make_target()
    before = capabilities_from_trace(trace_with(["echo"], negotiated="2025-11-25"), target)
    after = capabilities_from_trace(trace_with(["echo"], negotiated="2026-07-28"), target)

    assert before.capability_fingerprint != after.capability_fingerprint


def test_observed_transport_features_are_part_of_the_fingerprint() -> None:
    """They gate applicability, so a change in them is a change in the surface."""
    target = make_target()
    plain = capabilities_from_trace(trace_with(["echo"]), target)
    featured = capabilities_from_trace(
        trace_with(["echo"]), target, transport_features={"transport": "stdio"}
    )

    assert plain.capability_fingerprint != featured.capability_fingerprint


def test_a_declared_fallback_record_still_carries_a_fingerprint() -> None:
    """The substitution turns an unusable record into a usable one.

    ``model_copy`` skips validators, so a copy would have carried the fallback
    tool list with no digest describing it — a usable record with nothing to
    check an applicability decision against.
    """
    target = make_target(expected_tools=["echo"], allow_declared_tool_fallback=True)
    failed = unresolved(
        target,
        status=CapabilityResolutionStatus.DISCOVERY_FAILED,
        detail="the fixture resets every connection",
    )

    substituted = apply_declared_fallback(failed, target)

    assert substituted.status is CapabilityResolutionStatus.DECLARED_FALLBACK
    assert substituted.capability_fingerprint == substituted.fingerprint().digest
