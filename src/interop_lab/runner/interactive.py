"""The interactive strategy: a live session, one step at a time.

This is the behaviour the harness has always had, moved behind the strategy
boundary without a change in semantics. It covers every combination where the
harness genuinely holds the connection:

* stdio on the host,
* stdio in a container (the harness attaches to the container's pipes),
* Streamable HTTP on the host.

It does **not** cover a contained HTTP target, and cannot: there is no socket
from the host to reach one.
"""

from __future__ import annotations

from typing import Any

from interop_lab.assertions.checks import evaluate
from interop_lab.events.model import WireEvent
from interop_lab.injection.faults import Fault
from interop_lab.models.core import AssertionResult, TargetDefinition
from interop_lab.profiles.registry import STREAMABLE_HTTP_ACCEPT, ProtocolProfile, Transport
from interop_lab.runner.images import ExecutionContext
from interop_lab.runner.strategy import ExecutionOutcome
from interop_lab.scenarios.model import Scenario, Step, StepKind
from interop_lab.transports.base import TransportSession, TransportTimeoutError
from interop_lab.transports.client import build_notification, build_request, initialize_params
from interop_lab.transports.http.session import StreamableHttpSession
from interop_lab.transports.stdio.session import StdioSession

#: Case-insensitive, because HTTP field names are and servers differ.
SESSION_ID_HEADER = "mcp-session-id"


def session_id_from(event: WireEvent | None) -> str | None:
    """The session identifier an initialize response carried, if any."""
    if event is None or not event.headers:
        return None
    for name, value in event.headers.items():
        if name.lower() == SESSION_ID_HEADER and value:
            return value
    return None


class TransportUnsupportedError(Exception):
    """This session cannot express the step, and nothing is substituted for it.

    Refused rather than approximated, for the same reason the plan compiler
    refuses a step it cannot express: a step quietly turned into a different one
    produces evidence about traffic the scenario never described.
    """


class InteractiveSessionStrategy:
    """Drive a ``TransportSession`` step by step."""

    name = "interactive_session"

    def _make_session(
        self,
        target: TargetDefinition,
        *,
        run_id: str,
        faults: list[Fault],
        context: ExecutionContext,
    ) -> TransportSession:
        match target.transport:
            case Transport.STDIO:
                # The context reaches the container launch: a contained stdio
                # target has to resolve its image before the first byte, and a
                # session that had to find the resolver for itself is a session
                # that can find the wrong one.
                return StdioSession(target, run_id=run_id, faults=faults, context=context)
            case Transport.STREAMABLE_HTTP:
                return StreamableHttpSession(target, run_id=run_id, faults=faults)

    async def execute(
        self,
        scenario: Scenario,
        target: TargetDefinition,
        *,
        run_id: str,
        profile: ProtocolProfile,
        context: ExecutionContext,
    ) -> ExecutionOutcome:
        assertions: list[AssertionResult] = []
        timed_out = False
        timeout_cause = None
        process_alive_at_timeout = False
        expected_timeout = False
        harness_error: str | None = None

        session = self._make_session(target, run_id=run_id, faults=scenario.faults, context=context)
        try:
            async with session:
                # Profiles with a stateful lifecycle require the handshake before
                # any ordinary traffic. Stateless profiles must not receive one.
                if profile.requires_initialize:
                    # Setup uses the target's declared budget, not the scenario's.
                    # A scenario deadline may be deliberately tight to test timing;
                    # applying it to process startup measures the runtime's cold
                    # start instead of the behaviour under test.
                    await self._handshake(
                        session,
                        profile=profile,
                        timeout_s=max(target.timeout_s, scenario.timeout_s),
                    )

                # Fault occurrence indices count scenario traffic only, so a
                # fault definition means the same thing on stateful and
                # stateless profiles alike.
                session.begin_scenario()

                for step in scenario.steps:
                    step_timeout = step.timeout_s or scenario.timeout_s
                    event: WireEvent | None = None
                    request_id: str | int | None = None
                    step_timed_out = False

                    try:
                        event, request_id = await self._execute_step(
                            session, step, profile=profile, timeout_s=step_timeout
                        )
                    except TransportTimeoutError as exc:
                        step_timed_out = True
                        timeout_cause = exc.cause
                        proc = getattr(session, "process", None)
                        process_alive_at_timeout = proc is not None and proc.returncode is None
                        if step.timeout_is_expected:
                            expected_timeout = True
                        else:
                            timed_out = True

                    for spec in step.assertions:
                        assertions.append(
                            evaluate(
                                spec,
                                event=event,
                                request_id=request_id,
                                timed_out=step_timed_out,
                                timeout_expected=step.timeout_is_expected,
                            )
                        )

                    if timed_out:
                        break  # A hung target invalidates every later step.
        except Exception as exc:
            harness_error = f"{type(exc).__name__}: {exc}"

        return ExecutionOutcome(
            trace=session.trace,
            process=session.outcome,
            assertions=assertions,
            faults_applied=session.faults_applied,
            containment=getattr(session, "containment", None),
            timed_out=timed_out,
            timeout_cause=timeout_cause,
            process_alive_at_timeout=process_alive_at_timeout,
            expected_timeout=expected_timeout,
            harness_error=harness_error,
        )

    async def _handshake(
        self, session: TransportSession, *, profile: ProtocolProfile, timeout_s: float
    ) -> None:
        """Perform the stateful `initialize` exchange for pre-2026 profiles.

        Handshake traffic is captured in the trace like any other traffic, but it
        is setup rather than the scenario under test, so it carries no assertions.
        """
        rpc_id = session.next_rpc_id()
        request = build_request(
            profile, rpc_id=rpc_id, method="initialize", params=initialize_params(profile)
        )
        event = await session.request(request, timeout_s=timeout_s, expect_id=rpc_id)

        # 2025-11-25 says a server MAY assign a session identifier here, and that
        # a client which received one MUST send it back. Both halves matter: it
        # is captured when present and simply absent otherwise, because requiring
        # it would fabricate a failure against a server that assigns none.
        issued = session_id_from(event)
        if issued is not None and hasattr(session, "session_id"):
            session.session_id = issued  # type: ignore[attr-defined]

        await session.send(
            build_notification(profile, method="notifications/initialized", params={})
        )

    async def _execute_step(
        self, session: TransportSession, step: Step, *, profile: ProtocolProfile, timeout_s: float
    ) -> tuple[WireEvent | None, str | int | None]:
        # Header overrides are scoped to the step that asked for them.
        overrides: dict[str, str] | None = getattr(session, "header_overrides", None)
        if overrides is not None:
            overrides.clear()
            if step.kind is not StepKind.HTTP_REQUEST:
                # An HTTP_REQUEST step assembles its own header set below,
                # including the removals a derived set cannot express.
                overrides.update(step.http_header_overrides)

        match step.kind:
            case StepKind.REQUEST:
                assert step.method is not None
                rpc_id = session.next_rpc_id()
                message = build_request(
                    profile, rpc_id=rpc_id, method=step.method, params=step.params
                )
                event = await session.request(message, timeout_s=timeout_s, expect_id=rpc_id)
                return event, rpc_id

            case StepKind.NOTIFICATION:
                assert step.method is not None
                message = build_notification(profile, method=step.method, params=step.params)
                notify: Any = getattr(session, "notify", None)
                if notify is not None:
                    # A request/response transport answers the POST itself, and
                    # that answer is the evidence: an empty-bodied 202 is what a
                    # conforming server returns to a notification. Reaching for
                    # `receive` here asked for an unsolicited inbound message,
                    # which Streamable HTTP does not have.
                    return await notify(message, timeout_s=timeout_s), None
                await session.send(message)
                # On a streaming transport a notification must not be answered;
                # a short read is what proves it.
                try:
                    event = await session.receive(timeout_s=min(timeout_s, 1.0))
                except TransportTimeoutError:
                    return None, None
                return event, None

            case StepKind.RAW:
                assert step.raw_payload is not None
                payload = step.raw_payload.encode()
                request_raw: Any = getattr(session, "request_raw", None)
                if request_raw is not None:
                    # Request/response transports correlate the answer themselves.
                    return await request_raw(payload, timeout_s=timeout_s), None
                await session.send_raw(payload)
                return await session.receive(timeout_s=timeout_s), None

            case StepKind.HTTP_REQUEST:
                exchange: Any = getattr(session, "http_exchange", None)
                if exchange is None:
                    raise TransportUnsupportedError(
                        f"step {step.step_id!r} describes a transport-level HTTP exchange, "
                        f"and this session speaks {type(session).__name__}. The scenario's "
                        "applicability restricts it to Streamable HTTP, so reaching here "
                        "means the gate was bypassed rather than that the step is wrong."
                    )
                # Session-id and routing headers are supplied here rather than
                # inside the session, so a scenario asking what happens without
                # one gets a request that genuinely lacks it.
                headers: dict[str, str] = {"Accept": STREAMABLE_HTTP_ACCEPT}
                if step.http_body_text is not None:
                    headers["Content-Type"] = "application/json"
                if "MCP-Protocol-Version" in profile.required_http_headers:
                    headers["MCP-Protocol-Version"] = profile.spec_version
                session_id = getattr(session, "session_id", None)
                if session_id is not None and step.http_send_session_id:
                    headers["Mcp-Session-Id"] = session_id
                headers.update(step.http_header_overrides)
                for name in step.http_header_removals:
                    headers.pop(name, None)
                event = await exchange(
                    method=step.http_method,
                    path=step.http_path,
                    headers=headers,
                    body=(
                        step.http_body_text.encode() if step.http_body_text is not None else None
                    ),
                    timeout_s=timeout_s,
                    abort_after_headers=step.http_abort_after_headers,
                )
                return event, None

            case StepKind.EXPECT_MESSAGE:
                event = await session.receive(timeout_s=timeout_s)
                return event, None
