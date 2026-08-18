"""Discovering what a target advertises, using the target's own execution path.

The discovery exchange runs through ``select_strategy`` — the same routing the
battery uses — for one reason: a capability record obtained under weaker
isolation than the runs it justifies would be describing a different program.
A contained stdio target is discovered in a container with no network; a
contained HTTP target is discovered through the probe sidecar on an internal
network with no published port. There is no host fallback here, exactly as
there is none in the battery.

The discovery scenario is built here rather than registered in
``scenarios.library``. It is machinery, not a test: it asserts nothing, it can
never fail a target, and it must not appear in the battery count or in any
report as a scenario that ran.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from interop_lab.capabilities.model import (
    CAPABILITY_LIMITATIONS,
    TRANSPORT_FEATURE_LIMITATIONS,
    CapabilityResolutionStatus,
    ObservedTransportFeatures,
    ResolvedTargetCapabilities,
    ToolDescriptor,
)
from interop_lab.containment.model import UNUSABLE_CONTAINMENT
from interop_lab.events.model import WireTrace
from interop_lab.models.core import (
    ScenarioCategory,
    ScenarioDefinition,
    ScenarioNature,
    TargetDefinition,
)
from interop_lab.profiles.registry import Transport, get_profile
from interop_lab.runner.images import ExecutionContext
from interop_lab.runner.strategy import StrategySelectionError, select_strategy
from interop_lab.scenarios.model import Scenario, Step, StepKind

#: Not a battery scenario id. Prefixed so it can never be confused with one in a
#: report, and excluded from the library so it cannot be selected by a suite.
DISCOVERY_SCENARIO_ID = "internal.capability_discovery"

#: HTTP field names are case-insensitive and servers differ on the spelling.
SESSION_ID_HEADER_LOWER = "mcp-session-id"


def discovery_scenario() -> Scenario:
    """The smallest exchange that reveals a target's advertised surface.

    `initialize` is performed by the transport as part of every session, so its
    response is already in the trace; only `tools/list` has to be asked for.

    No assertions. A scenario with assertions can fail, and a failing capability
    probe would put a verdict about the target into a record whose only job is
    to describe it.
    """
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id=DISCOVERY_SCENARIO_ID,
            version="1",
            title="Resolve the target's advertised capabilities",
            category=ScenarioCategory.BASELINE,
            nature=ScenarioNature.INFORMATIONAL,
            purpose=(
                "Observe the protocol revision, server identity and tool surface the "
                "target actually advertises, so applicability is decided from what is "
                "there rather than from what a target file claims."
            ),
            expected_outcome="A parsed tools/list response. Nothing here is graded.",
            rationale=(
                "Machinery, not a test. It asserts nothing and cannot produce a verdict "
                "about any target."
            ),
        ),
        steps=[
            Step(
                step_id="list_tools",
                kind=StepKind.REQUEST,
                method="tools/list",
                params={},
                optional=False,
                assertions=[],
            )
        ],
    )


def _initialize_result(trace: WireTrace) -> dict[str, Any] | None:
    """The `initialize` result, matched by request id rather than by position.

    Position is not reliable: diagnostics, notifications and lifecycle events
    are interleaved into the same trace, and their count differs by transport.
    """
    initialize_ids: set[Any] = set()
    for event in trace.events:
        message = event.message
        if not isinstance(message, dict):
            continue
        if message.get("method") == "initialize" and "id" in message:
            initialize_ids.add(_id_key(message["id"]))

    for event in trace.events:
        message = event.message
        if not isinstance(message, dict) or "result" not in message:
            continue
        if _id_key(message.get("id")) in initialize_ids:
            result = message["result"]
            return result if isinstance(result, dict) else None

    # Some paths record the handshake without the request half. A result that
    # carries protocolVersion is unambiguously an initialize result.
    for event in trace.events:
        message = event.message
        if (
            isinstance(message, dict)
            and isinstance(message.get("result"), dict)
            and ("protocolVersion" in message["result"])
        ):
            return message["result"]
    return None


def _tools_list_result(trace: WireTrace) -> dict[str, Any] | None:
    request_ids: set[Any] = set()
    for event in trace.events:
        message = event.message
        if isinstance(message, dict) and message.get("method") == "tools/list":
            request_ids.add(_id_key(message.get("id")))

    for event in trace.events:
        message = event.message
        if not isinstance(message, dict) or not isinstance(message.get("result"), dict):
            continue
        if _id_key(message.get("id")) in request_ids:
            return message["result"]

    for event in trace.events:
        message = event.message
        if (
            isinstance(message, dict)
            and isinstance(message.get("result"), dict)
            and (isinstance(message["result"].get("tools"), list))
        ):
            return message["result"]
    return None


def _id_key(value: Any) -> Any:
    """JSON-RPC ids may be a string or a number; 1 and "1" are different ids."""
    return (type(value).__name__, value)


def _initialize_response_headers(trace: WireTrace) -> dict[str, str]:
    """Headers on the response that carried the negotiated protocol version.

    Matched by content rather than by position: the handshake sits among
    lifecycle notes, diagnostics and the tools/list exchange, and their number
    differs between the interactive and contained paths.
    """
    for event in trace.events:
        message = event.message
        if (
            isinstance(message, dict)
            and isinstance(message.get("result"), dict)
            and "protocolVersion" in message["result"]
            and event.headers
        ):
            return dict(event.headers)
    return {}


def observe_transport_features(
    trace: WireTrace,
    target: TargetDefinition,
    *,
    execution_strategy: str | None = None,
) -> ObservedTransportFeatures:
    """What the handshake revealed about the transport, as opposed to the server.

    Nothing here is advertised, which is why it is not called a capability. A
    session identifier is seen on a response header; whether one exists at all is
    something only a handshake can show. Absence is recorded as absence and never
    as inability.
    """
    profile = get_profile(target.protocol_profile)
    features = ObservedTransportFeatures(
        transport=target.transport.value,
        execution_strategy=execution_strategy,
    )

    if not profile.requires_initialize:
        # A stateless profile performs no handshake, so "did it complete" has no
        # answer here. None says that; False would say it failed.
        return features

    initialize = _initialize_result(trace)
    features.initialization_completed = initialize is not None
    if initialize is not None:
        negotiated = initialize.get("protocolVersion")
        if isinstance(negotiated, str):
            features.accepted_protocol_version = negotiated

    headers = _initialize_response_headers(trace)
    if target.transport is Transport.STREAMABLE_HTTP:
        for name, value in headers.items():
            if name.lower() == SESSION_ID_HEADER_LOWER and value:
                features.session_identifier_issued = True
                features.session_identifier_header_name = name
                break
        else:
            # Only stated once a handshake was actually seen. Saying "no session
            # identifier" about an exchange that never happened would be an
            # inference dressed as an observation.
            if features.initialization_completed:
                features.session_identifier_issued = False

    return features


def capabilities_from_trace(
    trace: WireTrace,
    target: TargetDefinition,
    *,
    transport_features: dict[str, Any] | None = None,
    observed_transport_features: ObservedTransportFeatures | None = None,
) -> ResolvedTargetCapabilities:
    """Parse a discovery trace. Never raises on a hostile or partial answer.

    Everything observed is accumulated first and the record is constructed once,
    at the end. That is not a style preference: the record's fingerprint is
    computed by a model validator, which runs at construction, so a function that
    built an empty record and then filled it in field by field would store a
    digest of the empty one. ``transport_features`` is a parameter for the same
    reason — it is part of the fingerprint, so it has to be present before the
    record exists rather than assigned onto it afterwards.
    """
    profile = get_profile(target.protocol_profile)
    fields: dict[str, Any] = {
        "target_id": target.target_id,
        "protocol_profile_offered": profile.spec_version,
        "declared_tools": list(target.expected_tools),
        "discovered_at": datetime.now(UTC).isoformat(),
        "discovery_run_id": trace.run_id,
        "limitations": list(CAPABILITY_LIMITATIONS),
        "observed_transport_features": observed_transport_features,
        # Derived from the typed record when there is one, so the fingerprint
        # material and the record it describes cannot come to disagree.
        "transport_features": (
            observed_transport_features.as_fingerprint_material()
            if observed_transport_features is not None
            else dict(transport_features or {})
        ),
    }

    initialize = _initialize_result(trace)
    if initialize is not None:
        negotiated = initialize.get("protocolVersion")
        if isinstance(negotiated, str):
            fields["protocol_profile_negotiated"] = negotiated
        server_info = initialize.get("serverInfo")
        if isinstance(server_info, dict):
            for field, key in (
                ("server_name", "name"),
                ("server_version", "version"),
                ("server_title", "title"),
            ):
                value = server_info.get(key)
                if isinstance(value, str):
                    fields[field] = value
        capabilities = initialize.get("capabilities")
        if isinstance(capabilities, dict):
            fields["server_capabilities"] = capabilities
            fields["advertises_resources"] = "resources" in capabilities
            fields["advertises_prompts"] = "prompts" in capabilities
            fields["advertises_logging"] = "logging" in capabilities
            fields["advertises_completions"] = "completions" in capabilities

    tools: list[ToolDescriptor] = []
    listed = _tools_list_result(trace)
    if listed is not None:
        for raw in listed.get("tools") or []:
            if not isinstance(raw, dict):
                continue
            try:
                tools.append(ToolDescriptor.from_advertised(raw))
            except ValueError:
                # A malformed tool entry is a fact about the target, not a
                # reason to abandon the whole record. It is simply not a tool
                # any scenario can require by name.
                continue
    fields["tools"] = tools

    if listed is None:
        fields["status"] = CapabilityResolutionStatus.DISCOVERY_FAILED
        fields["status_detail"] = "the target returned no usable tools/list result"
    else:
        fields["status"] = CapabilityResolutionStatus.RESOLVED

    return ResolvedTargetCapabilities(**fields)


def unresolved(
    target: TargetDefinition,
    *,
    status: CapabilityResolutionStatus,
    detail: str,
) -> ResolvedTargetCapabilities:
    """A record that says plainly that nothing was observed."""
    profile = get_profile(target.protocol_profile)
    return ResolvedTargetCapabilities(
        target_id=target.target_id,
        protocol_profile_offered=profile.spec_version,
        declared_tools=list(target.expected_tools),
        discovered_at=datetime.now(UTC).isoformat(),
        status=status,
        status_detail=detail,
        limitations=list(CAPABILITY_LIMITATIONS),
    )


class CapabilityResolver:
    """Resolves each target once, and remembers the answer for that target.

    Cached by target id, target fingerprint **and** the resolution in force.

    The first two alone were not enough, and the reason is specific to locally
    built images. The target fingerprint is deliberately portable: it covers what
    a target is built *from*, not the image ID that came out. So two different
    builds — a rebuild after a base-image pull, a manifest carried from another
    builder — share a fingerprint while being different bits. Keyed on the
    fingerprint alone, capability evidence observed from one built image would be
    silently reused as evidence about the other, and every applicability decision
    downstream would rest on a tool surface nobody measured on that image.

    Adding the resolution identity to the key makes those two separate cache
    entries, which is what they are.
    """

    def __init__(
        self,
        *,
        artifacts_dir: Path | None = None,
        context: ExecutionContext | None = None,
    ) -> None:
        self.artifacts_dir = artifacts_dir
        self.context = context or ExecutionContext.create()
        self._cache: dict[tuple[str, str, str], ResolvedTargetCapabilities] = {}

    def cached(self, target: TargetDefinition) -> ResolvedTargetCapabilities | None:
        return self._cache.get(self.cache_key(target))

    def cache_key(self, target: TargetDefinition) -> tuple[str, str, str]:
        """What makes two capability records describe the same measurement.

        Public because it is a property worth asserting on directly: whether two
        situations share a cache entry is the whole question this class answers,
        and inferring it from call counts would be a weaker test.
        """
        from interop_lab.ci.gate import target_fingerprint

        return (target.target_id, target_fingerprint(target), self._resolution_identity(target))

    def _resolution_identity(self, target: TargetDefinition) -> str:
        """Which built image this evidence would describe.

        A registry target resolves to its pinned digest and needs nothing built,
        so the manifest in force is irrelevant to it — including it would evict
        perfectly good evidence every time an unrelated rebuild happened.
        """
        spec = target.container
        if spec is None or spec.local_build_source is None:
            return "registry-or-host"
        entry = None
        index = self.context.resolver.index
        if index is not None:
            entry = index.manifest.entry_for(target.target_id)
        image_id = entry.observed_local_image_id if entry is not None else "unresolved"
        return f"{self.context.resolution_identity}:{image_id}"

    async def resolve(self, target: TargetDefinition) -> ResolvedTargetCapabilities:
        from interop_lab.ci.gate import target_fingerprint

        key = self.cache_key(target)
        if key in self._cache:
            return self._cache[key]

        record = await self._discover(target)
        record.target_fingerprint = target_fingerprint(target)
        record.resolution_identity = key[2]
        self._cache[key] = record
        return record

    async def _discover(self, target: TargetDefinition) -> ResolvedTargetCapabilities:
        profile = get_profile(target.protocol_profile)
        scenario = discovery_scenario()
        run_id = f"{DISCOVERY_SCENARIO_ID}__{target.target_id}"

        try:
            strategy = select_strategy(target)
        except StrategySelectionError as exc:
            return unresolved(
                target,
                status=CapabilityResolutionStatus.NO_STRATEGY,
                detail=f"no strategy implements this target: {exc}",
            )

        try:
            outcome = await strategy.execute(
                scenario, target, run_id=run_id, profile=profile, context=self.context
            )
        except Exception as exc:
            return unresolved(
                target,
                status=CapabilityResolutionStatus.DISCOVERY_FAILED,
                detail=f"{type(exc).__name__}: {exc}",
            )

        containment = outcome.containment
        if containment is not None and containment.outcome in UNUSABLE_CONTAINMENT:
            # The discovery ran with different isolation than the battery will.
            # Its answers are not evidence about the contained target, and are
            # refused rather than used with a caveat.
            return unresolved(
                target,
                status=CapabilityResolutionStatus.CONTAINMENT_UNUSABLE,
                detail=(
                    f"discovery containment outcome was {containment.outcome.value}; "
                    f"{containment.unavailable_reason or 'no further detail'}"
                ),
            )

        record = capabilities_from_trace(
            outcome.trace,
            target,
            observed_transport_features=observe_transport_features(
                outcome.trace, target, execution_strategy=strategy.name
            ),
        )
        record.limitations.extend(TRANSPORT_FEATURE_LIMITATIONS)
        if self.artifacts_dir is not None:
            record.source_trace_uri = f"capabilities/{target.target_id}.trace.jsonl"
        return record


def apply_declared_fallback(
    record: ResolvedTargetCapabilities, target: TargetDefinition
) -> ResolvedTargetCapabilities:
    """Substitute the declared tool list when — and only when — that is allowed.

    Reached by exactly one kind of target: a deliberately broken local fixture
    whose defect prevents any handshake, so no discovery could ever succeed
    against it. Those fixtures exist to prove the harness detects failures, and
    an impossible precondition would delete that coverage.

    The substitution is never silent. The returned record carries
    ``DECLARED_FALLBACK``, which reads as "this decision came from a list someone
    typed" everywhere the status is shown, and the model refuses the flag
    outright on any external target.
    """
    if record.is_usable or not target.allow_declared_tool_fallback:
        return record

    # Re-validated rather than ``model_copy``-ed. The substitution turns an
    # unusable record into a usable one, and the fingerprint is computed by a
    # validator that ``model_copy`` does not run — so a copy would carry the
    # fallback tool list with no digest describing it.
    return ResolvedTargetCapabilities.model_validate(
        {
            **record.model_dump(mode="json"),
            "status": CapabilityResolutionStatus.DECLARED_FALLBACK.value,
            "status_detail": (
                f"discovery did not complete ({record.status.value}: "
                f"{record.status_detail or 'no detail'}), and this target is a local "
                "fixture permitted to fall back to its declared tool list"
            ),
            "tools": [
                ToolDescriptor(name=name).model_dump(mode="json") for name in target.expected_tools
            ],
            "limitations": [
                *record.limitations,
                "Tools here were declared, not observed. This record does not "
                "establish that the target advertises them.",
            ],
        }
    )
