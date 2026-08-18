"""Compile a scenario into a probe plan.

Every MCP-specific decision for a contained HTTP run is made here, on the host,
in code the assertion tests already cover: which handshake a dated profile
needs, which headers a request must carry, what a fault does to the bytes. The
probe receives only the consequences.

The split matters because the probe runs next to untrusted output. If it decided
what a session id is, or which header a profile requires, those judgements would
live in an unreviewed second implementation that no assertion test ever touches.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from interop_lab.injection.faults import (
    Boundary,
    Fault,
    FaultKind,
    apply_field_fault,
)
from interop_lab.models.core import TargetDefinition
from interop_lab.probe.plan import (
    PLAN_SCHEMA_VERSION,
    PROBE_VERSION,
    ProbeAbort,
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
from interop_lab.profiles.registry import (
    STREAMABLE_HTTP_ACCEPT,
    ProtocolProfile,
    get_profile,
)
from interop_lab.scenarios.model import Scenario, Step, StepKind
from interop_lab.transports.client import build_notification, build_request, initialize_params

#: Variable holding a session identifier issued by a stateful server.
#:
#: The 2025-11-25 Streamable HTTP binding says a server **MAY** assign a session
#: id in `Mcp-Session-Id` on the initialize response, and that a client which
#: received one **MUST** include it on subsequent requests. Both halves are
#: encoded here: the capture is optional, and the substitution is optional too,
#: so a server that issues no id is not treated as defective and a plan is never
#: built on the assumption that every profile is session-based.
SESSION_ID_VARIABLE = "mcp_session_id"
SESSION_ID_HEADER = "Mcp-Session-Id"

#: A step that carries no explicit id still needs a stable one, because probe
#: events are keyed by step id and the host maps them back by it.
SETUP_INITIALIZE = "__setup_initialize"
SETUP_INITIALIZED = "__setup_initialized"
CLEANUP_DELETE_SESSION = "__cleanup_delete_session"


class PlanCompilationError(Exception):
    """A scenario cannot be expressed as a plan without changing its meaning."""


class _RpcIds:
    """Request ids, allocated exactly as the interactive path allocates them.

    Shared numbering is what lets the same scenario produce comparable evidence
    on the host and contained paths; a different sequence would make every
    differential comparison report a spurious id divergence.
    """

    def __init__(self) -> None:
        self._next = 1

    def take(self) -> int:
        value = self._next
        self._next += 1
        return value


def _routing_headers(profile: ProtocolProfile, message: dict[str, Any]) -> dict[str, str]:
    """The headers a profile requires, derived from the body.

    Derived rather than declared so they agree with the body by construction. A
    disagreement is something a scenario asks for explicitly, and therefore can
    never appear by accident.
    """
    headers = {"Content-Type": "application/json", "Accept": STREAMABLE_HTTP_ACCEPT}
    if "MCP-Protocol-Version" in profile.required_http_headers:
        headers["MCP-Protocol-Version"] = profile.spec_version
    if "Mcp-Method" in profile.required_http_headers:
        method = message.get("method")
        if isinstance(method, str):
            headers["Mcp-Method"] = method
    if "Mcp-Name" in profile.required_http_headers:
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if isinstance(name, str):
            headers["Mcp-Name"] = name
    return headers


def _session_substitution() -> ProbeSubstitution:
    return ProbeSubstitution(
        variable=SESSION_ID_VARIABLE,
        target=ProbeSubstitutionTarget.HEADER,
        header=SESSION_ID_HEADER,
        # Optional on purpose: a 2025 server that issues no session id is
        # conforming, and requiring the header would fabricate a failure.
        required=False,
    )


def _setup_steps(
    profile: ProtocolProfile, *, path: str, timeout_s: float, ids: _RpcIds
) -> list[ProbeStep]:
    """The traffic a dated profile requires before the scenario can begin.

    Verified against the profile registry, which is derived from the dated JSON
    schemas rather than from prose. A stateless profile gets no setup at all —
    sending an `initialize` to a 2026 target would be sending a method that
    revision removed.
    """
    if not profile.requires_initialize:
        return []

    initialize = build_request(
        profile, rpc_id=ids.take(), method="initialize", params=initialize_params(profile)
    )
    initialized = build_notification(profile, method="notifications/initialized", params={})

    return [
        ProbeStep(
            step_id=SETUP_INITIALIZE,
            phase=ProbePhase.SETUP,
            method=ProbeMethod.POST,
            path=path,
            headers=_routing_headers(profile, initialize),
            body_mode=ProbeBodyMode.JSON,
            json_body=initialize,
            timeout_s=timeout_s,
            captures=[
                ProbeCapture(
                    variable=SESSION_ID_VARIABLE,
                    source=ProbeCaptureSource.RESPONSE_HEADER,
                    header=SESSION_ID_HEADER,
                    required=False,
                )
            ],
        ),
        ProbeStep(
            step_id=SETUP_INITIALIZED,
            phase=ProbePhase.SETUP,
            method=ProbeMethod.POST,
            path=path,
            headers=_routing_headers(profile, initialized),
            body_mode=ProbeBodyMode.JSON,
            json_body=initialized,
            timeout_s=timeout_s,
            substitutions=[_session_substitution()],
        ),
    ]


def _cleanup(profile: ProtocolProfile, *, path: str, timeout_s: float) -> ProbeCleanup:
    """Release target-side state, when the profile has any to release.

    2025-11-25 defines `DELETE` on the endpoint as the client's way to end a
    session it was given. It is best effort: a server that never issued a
    session id gets no DELETE, because the substitution is not required and the
    step is skipped rather than sent with an empty header.
    """
    if not profile.requires_initialize:
        return ProbeCleanup(close_connections=True)

    return ProbeCleanup(
        close_connections=True,
        steps=[
            ProbeStep(
                step_id=CLEANUP_DELETE_SESSION,
                phase=ProbePhase.CLEANUP,
                method=ProbeMethod.DELETE,
                path=path,
                headers={"MCP-Protocol-Version": profile.spec_version},
                timeout_s=timeout_s,
                substitutions=[
                    ProbeSubstitution(
                        variable=SESSION_ID_VARIABLE,
                        target=ProbeSubstitutionTarget.HEADER,
                        header=SESSION_ID_HEADER,
                        # Required *here*: a DELETE without the id would ask the
                        # server to terminate an unnamed session. When no id was
                        # issued the probe records the cleanup step as skipped
                        # rather than sending it — cleanup is best effort, and a
                        # skipped teardown is not a failure of the run.
                        required=True,
                    )
                ],
            )
        ],
    )


def _revise(step: ProbeStep, **changes: Any) -> ProbeStep:
    """Rebuild a step with changes, re-running its validators.

    ``model_copy`` would skip them, and the body-mode invariant — exactly one
    body member set, matching the declared mode — is precisely the one a fault
    is most likely to break.
    """
    data = {**step.model_dump(mode="json"), **changes}
    return ProbeStep.model_validate(data)


def _apply_fault(step: ProbeStep, fault: Fault, message: dict[str, Any]) -> ProbeStep | None:
    """Fold a fault into the compiled step, returning the step or ``None`` to drop.

    Faults are compiled into the plan rather than interpreted by the probe. A
    probe that understood faults would need to understand messages, and the
    whole design rests on it not doing that.
    """
    match fault.kind:
        case FaultKind.DROP:
            return None
        case FaultKind.DELAY:
            return _revise(step, delay_before_ms=float(fault.delay_ms))
        case FaultKind.TRUNCATE:
            raw = json.dumps(message, separators=(",", ":")).encode()[: fault.keep_bytes]
            return _revise(
                step,
                body_mode=ProbeBodyMode.BASE64.value,
                json_body=None,
                text_body=None,
                base64_body=base64.b64encode(raw).decode(),
            )
        case FaultKind.CORRUPT_JSON:
            assert fault.replacement is not None
            return _revise(
                step,
                body_mode=ProbeBodyMode.TEXT.value,
                json_body=None,
                base64_body=None,
                text_body=fault.replacement,
            )
        case FaultKind.MUTATE_FIELD | FaultKind.REMOVE_FIELD:
            return _revise(step, json_body=apply_field_fault(message, fault))
        case FaultKind.TERMINATE_PROCESS:
            raise PlanCompilationError(
                f"fault {fault.fault_id!r} terminates the target process. The contained "
                "path drives the target through a container the probe cannot signal, so "
                "expressing it would change what the scenario tests. Run this scenario "
                "against a stdio or host target."
            )
        case FaultKind.DUPLICATE:
            raise PlanCompilationError(
                f"fault {fault.fault_id!r} duplicates a message. The compiled plan has no "
                "representation for it yet; it is refused rather than silently ignored."
            )


def _scenario_step(
    step: Step,
    *,
    profile: ProtocolProfile,
    path: str,
    default_timeout_s: float,
    ids: _RpcIds,
    faults: list[Fault],
    occurrence: int,
    stateful: bool,
) -> tuple[ProbeStep | None, str | int | None, str | None]:
    """Compile one scenario step. Returns ``(step, rpc_id, fault_id)``."""
    timeout_s = step.timeout_s or default_timeout_s
    substitutions = [_session_substitution()] if stateful else []

    match step.kind:
        case StepKind.REQUEST:
            assert step.method is not None
            rpc_id = ids.take()
            message = build_request(profile, rpc_id=rpc_id, method=step.method, params=step.params)
            headers = {**_routing_headers(profile, message), **step.http_header_overrides}
            compiled = ProbeStep(
                step_id=step.step_id,
                phase=ProbePhase.SCENARIO,
                method=ProbeMethod.POST,
                path=path,
                headers=headers,
                body_mode=ProbeBodyMode.JSON,
                json_body=message,
                timeout_s=timeout_s,
                substitutions=substitutions if step.http_send_session_id else [],
            )

        case StepKind.NOTIFICATION:
            assert step.method is not None
            message = build_notification(profile, method=step.method, params=step.params)
            headers = {**_routing_headers(profile, message), **step.http_header_overrides}
            compiled = ProbeStep(
                step_id=step.step_id,
                phase=ProbePhase.SCENARIO,
                method=ProbeMethod.POST,
                path=path,
                headers=headers,
                body_mode=ProbeBodyMode.JSON,
                json_body=message,
                timeout_s=timeout_s,
                substitutions=substitutions if step.http_send_session_id else [],
            )
            rpc_id = None

        case StepKind.RAW:
            assert step.raw_payload is not None
            # Byte-exact, with no JSON round trip anywhere in the path: a
            # truncated or malformed payload must arrive at the target exactly as
            # written, or the scenario is testing something else entirely.
            headers = {
                "Content-Type": "application/json",
                "Accept": STREAMABLE_HTTP_ACCEPT,
                **step.http_header_overrides,
            }
            compiled = ProbeStep(
                step_id=step.step_id,
                phase=ProbePhase.SCENARIO,
                method=ProbeMethod.POST,
                path=path,
                headers=headers,
                body_mode=ProbeBodyMode.TEXT,
                text_body=step.raw_payload,
                timeout_s=timeout_s,
                substitutions=substitutions if step.http_send_session_id else [],
            )
            message = {}
            rpc_id = None

        case StepKind.HTTP_REQUEST:
            # A transport-level exchange: whatever method, path, headers and body
            # the scenario asked for, with nothing derived from a message —
            # there may not be one. Routing headers are still supplied for a
            # body-carrying step so the request is well-formed in every respect
            # except the one under test.
            body = step.http_body_text
            headers = (
                {"Content-Type": "application/json", "Accept": STREAMABLE_HTTP_ACCEPT}
                if body is not None
                else {"Accept": STREAMABLE_HTTP_ACCEPT}
            )
            if "MCP-Protocol-Version" in profile.required_http_headers:
                headers["MCP-Protocol-Version"] = profile.spec_version
            headers.update(step.http_header_overrides)
            for name in step.http_header_removals:
                headers.pop(name, None)
            compiled = ProbeStep(
                step_id=step.step_id,
                phase=ProbePhase.SCENARIO,
                method=ProbeMethod(step.http_method),
                path=step.http_path or path,
                headers=headers,
                body_mode=ProbeBodyMode.TEXT if body is not None else ProbeBodyMode.NONE,
                text_body=body,
                timeout_s=timeout_s,
                abort=(
                    ProbeAbort.AFTER_HEADERS if step.http_abort_after_headers else ProbeAbort.NONE
                ),
                substitutions=substitutions if step.http_send_session_id else [],
            )
            message = {}
            rpc_id = None

        case StepKind.EXPECT_MESSAGE:
            raise PlanCompilationError(
                f"step {step.step_id!r} waits for an unsolicited inbound message. "
                "Streamable HTTP has no such channel in either supported profile, and "
                "the contained path does not implement SSE, so the step is refused "
                "rather than quietly turned into something else."
            )

    fault_id: str | None = None
    for fault in faults:
        if fault.boundary is not Boundary.OUTBOUND_MESSAGE:
            continue
        if not fault.should_fire(index=occurrence, method=step.method):
            continue
        fault_id = fault.fault_id
        revised = _apply_fault(compiled, fault, message)
        if revised is None:
            return None, rpc_id, fault_id
        compiled = revised
        break

    return compiled, rpc_id, fault_id


class CompiledPlan:
    """A plan plus the host-side bookkeeping needed to interpret its results."""

    def __init__(
        self,
        plan: ProbePlan,
        *,
        request_ids: dict[str, str | int | None],
        dropped_steps: dict[str, str],
        fault_by_step: dict[str, str],
        faults_applied: list[str],
    ) -> None:
        self.plan = plan
        #: step_id -> JSON-RPC id, so a response can be correlated to its step.
        self.request_ids = request_ids
        #: step_id -> fault id, for steps a fault removed from the plan entirely.
        self.dropped_steps = dropped_steps
        #: step_id -> fault id, for steps that were sent with a fault folded in.
        #: Kept apart from ``dropped_steps`` because a mutated request produces
        #: wire events to attribute the fault to and a dropped one does not.
        self.fault_by_step = fault_by_step
        self.faults_applied = faults_applied


def compile_plan(
    scenario: Scenario,
    target: TargetDefinition,
    *,
    run_id: str,
    path: str = "/mcp",
) -> CompiledPlan:
    """Compile one scenario against one target into an executable probe plan."""
    profile = get_profile(target.protocol_profile)
    # A scenario asking what an *uninitialized* server does gets no handshake,
    # and therefore no session identifier to substitute either: there is nothing
    # to substitute, and a step that asked for one would silently send an empty
    # header rather than none.
    stateful = profile.requires_initialize and not scenario.suppress_profile_handshake
    ids = _RpcIds()
    setup_timeout = max(target.timeout_s, scenario.timeout_s)

    setup = (
        _setup_steps(profile, path=path, timeout_s=setup_timeout, ids=ids)
        if not scenario.suppress_profile_handshake
        else []
    )

    request_ids: dict[str, str | int | None] = {}
    dropped: dict[str, str] = {}
    fault_by_step: dict[str, str] = {}
    faults_applied: list[str] = []
    scenario_steps: list[ProbeStep] = []

    # Occurrence counting starts here, at zero, and counts scenario traffic
    # only. A handshake must never consume the occurrence a fault reserved for
    # the first scenario request, or the same fault definition would mean two
    # different things on the two profiles.
    for occurrence, step in enumerate(scenario.steps):
        compiled, rpc_id, fault_id = _scenario_step(
            step,
            profile=profile,
            path=path,
            default_timeout_s=scenario.timeout_s,
            ids=ids,
            faults=scenario.faults,
            occurrence=occurrence,
            stateful=stateful,
        )
        request_ids[step.step_id] = rpc_id
        if fault_id is not None:
            faults_applied.append(fault_id)
        if compiled is None:
            dropped[step.step_id] = fault_id or "dropped"
            continue
        if fault_id is not None:
            fault_by_step[step.step_id] = fault_id
        scenario_steps.append(compiled)

    plan = ProbePlan(
        plan_schema_version=PLAN_SCHEMA_VERSION,
        probe_version=PROBE_VERSION,
        run_id=run_id,
        protocol_profile=target.protocol_profile,
        timeout_s=scenario.timeout_s,
        ready_timeout_s=max(target.timeout_s, 30.0),
        variables=(
            [
                ProbeVariable(
                    name=SESSION_ID_VARIABLE,
                    description=(
                        "Session id from the initialize response, when the server issues "
                        "one. 2025-11-25 makes it optional, so this is never required."
                    ),
                    required=False,
                )
            ]
            if stateful
            else []
        ),
        setup_steps=setup,
        scenario_steps=scenario_steps,
        cleanup=(
            _cleanup(profile, path=path, timeout_s=setup_timeout)
            if not scenario.suppress_profile_handshake
            else ProbeCleanup(close_connections=True)
        ),
    )
    return CompiledPlan(
        plan,
        request_ids=request_ids,
        dropped_steps=dropped,
        fault_by_step=fault_by_step,
        faults_applied=faults_applied,
    )
