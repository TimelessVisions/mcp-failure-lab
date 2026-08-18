"""Streamable HTTP transport with full wire capture and boundary fault injection.

Localhost only, by construction. The session launches a local fixture process,
waits for it to advertise its port, and speaks JSON-RPC over HTTP POST while
recording every request, response and lifecycle fact.

Timing is captured at three points — connect, first byte, body complete — so a
slow-header stall can be told apart from a slow-body stall, and both from a dead
process. Collapsing those into one "timeout" is how a harness ends up blaming a
target for the wrong thing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import time
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlparse

from interop_lab.artifacts.portability import resolve_executable_tokens
from interop_lab.containment.docker import ContainmentUnavailableError
from interop_lab.containment.model import (
    ContainmentOutcome,
    ContainmentRecord,
    ExecutionMode,
)
from interop_lab.events.model import Channel, Direction, WireEvent, WireTrace
from interop_lab.injection.faults import Boundary, Fault, FaultKind, apply_outbound_fault
from interop_lab.models.core import ProcessOutcome, TargetDefinition, TimeoutCause
from interop_lab.profiles.registry import STREAMABLE_HTTP_ACCEPT
from interop_lab.security.redaction import redact_headers, redact_text, scrub_environment
from interop_lab.transports.base import TransportTimeoutError

READY_PATTERN = re.compile(r"INTEROP_FIXTURE_READY\s+(\S+)")

MAX_BODY_CAPTURE = 256 * 1024
MAX_CAPTURED_BYTES = 8 * 1024 * 1024

#: Only these hosts may ever be contacted. Enforced before any socket is opened.
ALLOWED_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class UnauthorizedTargetError(Exception):
    """Raised when a target URL points anywhere other than an allowed local host."""


def assert_local(url: str) -> None:
    """Refuse any URL that is not explicitly authorised localhost.

    This is checked before a connection is attempted, not after, so a
    misconfigured target cannot send hostile traffic to a third party.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnauthorizedTargetError(f"unsupported scheme in target URL: {url!r}")
    host = parsed.hostname or ""
    if host not in ALLOWED_HOSTS:
        raise UnauthorizedTargetError(
            f"refusing to contact non-local host {host!r}; only {sorted(ALLOWED_HOSTS)} "
            "are permitted, and hostile scenarios must never leave this machine"
        )


class StreamableHttpSession:
    """A launched local HTTP fixture plus the trace of everything exchanged."""

    def __init__(
        self,
        target: TargetDefinition,
        *,
        run_id: str,
        faults: list[Fault] | None = None,
    ) -> None:
        self.target = target
        self.run_id = run_id
        self.faults = faults or []
        self.trace = WireTrace(run_id=run_id)
        self.process: asyncio.subprocess.Process | None = None
        self.outcome = ProcessOutcome()
        self.faults_applied: list[str] = []
        self.base_url: str | None = None
        #: Populated by start(); None only if start() was never reached.
        self.containment: ContainmentRecord | None = None

        self._seq = 0
        self._t0 = time.perf_counter_ns()
        self._outbound_index = 0
        self._inbound_index = 0
        self._captured_bytes = 0
        self._next_rpc_id = 1
        self._faults_active = False
        self._stderr_task: asyncio.Task[None] | None = None
        #: Headers the scenario wants overridden on the next request, used by
        #: header/body mismatch scenarios.
        self.header_overrides: dict[str, str] = {}
        #: The session identifier the server issued on `initialize`, if it issued
        #: one. 2025-11-25 makes it optional, so this stays None against a server
        #: that assigns none and the header is simply never sent — requiring it
        #: would fabricate a failure against a conforming implementation.
        self.session_id: str | None = None
        #: Whether the next request should carry it. A scenario about a missing
        #: or terminated identifier turns this off for one step.
        self.send_session_id: bool = True

    @property
    def supports_raw(self) -> bool:
        """HTTP bodies are length-framed; malformed payloads are sent as bodies."""
        return True

    # ------------------------------------------------------------- lifecycle

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def start(self) -> None:
        if self.target.execution_mode is ExecutionMode.CONTAINER:
            # This session is the *host* path: it launches a local process and
            # opens a socket to it. A contained HTTP target is routed to the
            # sidecar strategy long before it gets here, so reaching this line
            # means the routing was bypassed — by a caller constructing the
            # session directly, or by a future branch that forgot the rule.
            #
            # It stays a hard refusal rather than a fallback. Running the target
            # on the host anyway would write `contained` into an artifact
            # describing an ordinary local process, which is exactly the false
            # claim the containment layer exists to prevent.
            self.containment = ContainmentRecord(
                mode=ExecutionMode.CONTAINER,
                provenance=self.target.provenance,
                outcome=ContainmentOutcome.CONTAINMENT_FAILED,
                requested=self.target.container,
                unavailable_reason=(
                    "a contained Streamable HTTP target reached the host session. "
                    "Contained HTTP runs through the probe sidecar "
                    "(runner.contained_http); this session would open a host socket to "
                    "the target and record containment it never had. See "
                    "docs/containment.md."
                ),
            )
            raise ContainmentUnavailableError(self.containment.unavailable_reason)

        self.containment = ContainmentRecord(
            mode=ExecutionMode.HOST,
            provenance=self.target.provenance,
            outcome=ContainmentOutcome.NOT_CONTAINED_BY_DESIGN,
            stated_limits=[
                "This target ran as an ordinary local process. Nothing isolated it "
                "beyond process-group cleanup and a scrubbed environment."
            ],
        )
        env = scrub_environment(self.target.env_allowlist, self.target.env)
        self._record(
            direction=Direction.LIFECYCLE,
            channel=Channel.PROCESS,
            # Log the portable form, not the resolved machine path.
            note=f"spawn {' '.join(self.target.launch_command)}",
        )
        command = resolve_executable_tokens(self.target.launch_command)
        self.process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self.target.working_dir or None,
            start_new_session=True,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._await_ready()

    async def _await_ready(self) -> None:
        """Read the fixture's readiness line to learn its ephemeral port."""
        process = self.process
        if process is None or process.stdout is None:
            raise RuntimeError("session not started")
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=self.target.timeout_s)
        except TimeoutError as exc:
            raise TransportTimeoutError(
                TimeoutCause.TARGET_NO_RESPONSE,
                f"fixture did not become ready within {self.target.timeout_s}s",
            ) from exc

        match = READY_PATTERN.search(line.decode("utf-8", errors="replace"))
        if match is None:
            raise TransportTimeoutError(
                TimeoutCause.TARGET_NO_RESPONSE,
                f"fixture did not advertise a URL; first stdout line was {line!r}",
            )
        url = match.group(1)
        assert_local(url)
        self.base_url = url
        self._record(
            direction=Direction.LIFECYCLE,
            channel=Channel.HTTP,
            note=f"server ready at {url}",
        )

    async def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.returncode is None:
            self.outcome.killed_by_harness = True
            await self._terminate_tree(process)
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task

        code = process.returncode
        self.outcome.exit_code = code
        if code is not None and code < 0:
            self.outcome.signal = -code
        self._record(
            direction=Direction.LIFECYCLE,
            channel=Channel.PROCESS,
            note=f"exit code={code} killed_by_harness={self.outcome.killed_by_harness}",
        )

    async def _terminate_tree(self, process: asyncio.subprocess.Process) -> None:
        try:
            pgid = os.getpgid(process.pid)
        except (ProcessLookupError, PermissionError, AttributeError):
            pgid = None

        def signal_group(sig: signal.Signals) -> None:
            if pgid is not None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pgid, sig)
            else:  # pragma: no cover
                with contextlib.suppress(ProcessLookupError):
                    process.send_signal(sig)

        signal_group(signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
            return
        except TimeoutError:
            pass
        signal_group(signal.SIGKILL)
        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except TimeoutError:
            self.outcome.cleanup_ok = False
            self.outcome.cleanup_detail = "process survived SIGKILL"

    async def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    return
                self.outcome.stderr_bytes += len(line)
                if self._captured_bytes < MAX_CAPTURED_BYTES:
                    self._captured_bytes += len(line)
                    self._record(direction=Direction.DIAGNOSTIC, channel=Channel.STDERR, raw=line)
        except asyncio.CancelledError:
            raise

    # ---------------------------------------------------------------- capture

    def _record(
        self,
        *,
        direction: Direction,
        channel: Channel,
        raw: bytes | None = None,
        message: dict[str, Any] | None = None,
        parse_error: str | None = None,
        headers: dict[str, str] | None = None,
        fault_applied: str | None = None,
        note: str | None = None,
    ) -> WireEvent:
        text: str | None = None
        redacted = False
        if raw is not None:
            capped = raw[:MAX_BODY_CAPTURE]
            text, redacted = redact_text(capped.decode("utf-8", errors="replace"))
            if len(raw) > MAX_BODY_CAPTURE:
                text += f"\n<truncated at {MAX_BODY_CAPTURE} bytes>"

        safe_headers: dict[str, str] | None = None
        if headers is not None:
            safe_headers, header_redacted = redact_headers(headers)
            redacted = redacted or header_redacted

        event = WireEvent(
            seq=self._seq,
            monotonic_ns=time.perf_counter_ns() - self._t0,
            wall_clock=datetime.now(UTC).isoformat(),
            direction=direction,
            channel=channel,
            transport="streamable_http",
            raw_sha256=WireEvent.digest(raw) if raw is not None else None,
            raw_len=len(raw) if raw is not None else None,
            raw_text=text,
            message=message,
            parse_error=parse_error,
            headers=safe_headers,
            fault_applied=fault_applied,
            note=note,
            redacted=redacted,
            # Promoted out of the header map onto its own member. An assertion
            # about a transport-level status must not have to parse a
            # pseudo-header out of a dictionary that also holds real ones, and
            # the contained path has recorded it this way all along — a status
            # readable on one path and not the other would make the same
            # scenario mean two things.
            http_method=(headers or {}).get(":method"),
            http_path=(headers or {}).get(":path"),
            http_status=(
                int((headers or {})[":status"])
                if (headers or {}).get(":status", "").isdigit()
                else None
            ),
        )
        self._seq += 1
        self.trace.append(event)
        return event

    # ----------------------------------------------------------------- rpc io

    def next_rpc_id(self) -> int:
        value = self._next_rpc_id
        self._next_rpc_id += 1
        return value

    def begin_scenario(self) -> None:
        self._faults_active = True
        self._outbound_index = 0
        self._inbound_index = 0

    def _select_fault(self, boundary: Boundary, index: int, method: str | None) -> Fault | None:
        if not self._faults_active:
            return None
        for fault in self.faults:
            if fault.boundary is boundary and fault.should_fire(index=index, method=method):
                return fault
        return None

    def _build_headers(self, message: dict[str, Any]) -> dict[str, str]:
        """Construct the profile's routing headers from the body.

        Headers are derived from the message so that, by default, they always
        agree with it. A mismatch is something a scenario must ask for
        explicitly via ``header_overrides`` — it can never happen by accident.
        """
        from interop_lab.profiles.registry import get_profile

        profile = get_profile(self.target.protocol_profile)
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
        if self.session_id is not None and self.send_session_id:
            headers["Mcp-Session-Id"] = self.session_id

        headers.update(self.header_overrides)
        return headers

    async def send(self, message: dict[str, Any]) -> None:
        """POST a message and discard whatever comes back.

        Only meaningful for notifications, which carry no id and expect no
        answer. Requests must use ``request()`` so the response is correlated
        with the request that produced it.
        """
        if "id" in message:
            raise NotImplementedError(
                "Streamable HTTP is request/response; use request() for messages "
                "carrying an id so the response is correlated"
            )
        payload = json.dumps(message).encode()
        headers = self._build_headers(message)
        self._outbound_index += 1
        try:
            await self._exchange(
                payload, headers, timeout_s=self.target.timeout_s, fault_id=None, message=message
            )
        except TransportTimeoutError:
            # A notification that goes unanswered is not an error; the fact is
            # already on the trace.
            return

    async def notify(self, message: dict[str, Any], *, timeout_s: float) -> WireEvent | None:
        """POST a notification and return the transport-level answer.

        ``send`` discards it, which was correct while nothing asserted on it.
        A scenario about what a server does with a notification needs the status
        line — an empty-bodied 202 is the conforming outcome — and reaching for
        ``receive`` to get it raised NotImplementedError, because there is no
        unsolicited inbound channel to receive from. The answer to a POST is not
        an unsolicited message; it is the other half of the exchange.
        """
        payload = json.dumps(message).encode()
        headers = self._build_headers(message)
        self._outbound_index += 1
        try:
            return await self._exchange(
                payload, headers, timeout_s=timeout_s, fault_id=None, message=message
            )
        except TransportTimeoutError:
            # Silence is a legitimate observation about a notification, and the
            # fact is already on the trace.
            return None

    async def receive(self, *, timeout_s: float) -> WireEvent:  # pragma: no cover
        raise NotImplementedError(
            "Streamable HTTP has no unsolicited inbound channel in this profile"
        )

    async def send_raw(self, payload: bytes) -> None:  # pragma: no cover - see request_raw()
        raise NotImplementedError("use request_raw() so the response is captured")

    async def request(
        self, message: dict[str, Any], *, timeout_s: float, expect_id: str | int | None = None
    ) -> WireEvent:
        raw = json.dumps(message).encode()
        method = message.get("method") if isinstance(message.get("method"), str) else None

        fault = self._select_fault(Boundary.OUTBOUND_MESSAGE, self._outbound_index, method)
        self._outbound_index += 1
        fault_id: str | None = None
        payload: bytes | None = raw

        if fault is not None:
            fault_id = fault.fault_id
            self.faults_applied.append(fault.fault_id)
            if fault.kind is FaultKind.DELAY:
                await asyncio.sleep(fault.delay_ms / 1000)
            elif fault.kind is FaultKind.TERMINATE_PROCESS:
                self.outcome.killed_by_harness = True
                process = self.process
                if process is not None:
                    await self._terminate_tree(process)
                self._record(
                    direction=Direction.LIFECYCLE,
                    channel=Channel.PROCESS,
                    fault_applied=fault.fault_id,
                    note=f"process terminated by fault {fault.fault_id}",
                )
                raise TransportTimeoutError(
                    TimeoutCause.TARGET_NO_RESPONSE, "target terminated by injected fault"
                )
            else:
                payload = apply_outbound_fault(raw, message, fault)

        if payload is None:
            self._record(
                direction=Direction.OUTBOUND,
                channel=Channel.HTTP,
                message=message,
                fault_applied=fault_id,
                note="request dropped by fault",
            )
            raise TransportTimeoutError(
                TimeoutCause.HARNESS_DEADLINE, "request dropped by injected fault"
            )

        headers = self._build_headers(message)
        return await self._exchange(
            payload, headers, timeout_s=timeout_s, fault_id=fault_id, message=message
        )

    async def request_raw(self, payload: bytes, *, timeout_s: float) -> WireEvent:
        """POST a literal body, for wire-integrity scenarios."""
        headers = {"Content-Type": "application/json", "Accept": STREAMABLE_HTTP_ACCEPT}
        if self.session_id is not None and self.send_session_id:
            headers["Mcp-Session-Id"] = self.session_id
        headers.update(self.header_overrides)
        self._outbound_index += 1
        return await self._exchange(payload, headers, timeout_s=timeout_s, fault_id=None)

    async def http_exchange(
        self,
        *,
        method: str,
        path: str | None,
        headers: dict[str, str],
        body: bytes | None,
        timeout_s: float,
        abort_after_headers: bool = False,
    ) -> WireEvent:
        """One transport-level HTTP exchange, for the Streamable HTTP binding.

        The binding says things about GET, about DELETE, and about what a server
        does with a body under an unacceptable content type. None of those are
        expressible as "POST a JSON-RPC message", so they get a path that speaks
        HTTP rather than JSON-RPC — the same socket code, told what to send.
        """
        self._outbound_index += 1
        return await self._exchange(
            body or b"",
            headers,
            timeout_s=timeout_s,
            fault_id=None,
            http_method=method,
            path_override=path,
            send_body=body is not None,
            abort_after_headers=abort_after_headers,
        )

    async def _exchange(
        self,
        payload: bytes,
        headers: dict[str, str],
        *,
        timeout_s: float,
        fault_id: str | None,
        message: dict[str, Any] | None = None,
        http_method: str = "POST",
        path_override: str | None = None,
        send_body: bool = True,
        abort_after_headers: bool = False,
    ) -> WireEvent:
        if self.base_url is None:
            raise RuntimeError("session not started")
        assert_local(self.base_url)

        parsed = urlparse(self.base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = path_override or parsed.path or "/"

        sent_message: dict[str, Any] | None = message
        parse_error: str | None = None
        if message is None:
            try:
                decoded = json.loads(payload)
                sent_message = decoded if isinstance(decoded, dict) else None
            except json.JSONDecodeError as exc:
                parse_error = f"harness sent intentionally malformed JSON: {exc}"

        self._record(
            direction=Direction.OUTBOUND,
            channel=Channel.HTTP,
            raw=payload,
            message=sent_message,
            parse_error=parse_error,
            headers={
                **headers,
                ":method": http_method,
                ":path": path,
                ":authority": f"{host}:{port}",
            },
            fault_applied=fault_id,
        )

        t_start = time.perf_counter()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout_s
            )
        except TimeoutError as exc:
            self._record(
                direction=Direction.LIFECYCLE,
                channel=Channel.HTTP,
                note=f"connect timeout after {timeout_s}s",
            )
            raise TransportTimeoutError(
                TimeoutCause.TRANSPORT_DEADLINE, f"connect timed out after {timeout_s}s"
            ) from exc
        except OSError as exc:
            self._record(
                direction=Direction.LIFECYCLE,
                channel=Channel.HTTP,
                note=f"connect failed: {type(exc).__name__}: {exc}",
            )
            raise TransportTimeoutError(
                TimeoutCause.TARGET_NO_RESPONSE, f"connection refused or reset: {exc}"
            ) from exc

        self._record(direction=Direction.LIFECYCLE, channel=Channel.HTTP, note="connection opened")

        try:
            request_lines = [f"{http_method} {path} HTTP/1.1", f"Host: {host}:{port}"]
            request_lines.extend(
                f"{name}: {value}" for name, value in headers.items() if not name.startswith(":")
            )
            if send_body:
                request_lines.append(f"Content-Length: {len(payload)}")
            request_lines.append("Connection: close")
            head = ("\r\n".join(request_lines) + "\r\n\r\n").encode()
            writer.write(head + (payload if send_body else b""))
            await writer.drain()

            return await self._read_response(
                reader,
                timeout_s=timeout_s,
                t_start=t_start,
                fault_id=fault_id,
                abort_after_headers=abort_after_headers,
            )
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            self._record(
                direction=Direction.LIFECYCLE, channel=Channel.HTTP, note="connection closed"
            )

    async def _read_response(
        self,
        reader: asyncio.StreamReader,
        *,
        timeout_s: float,
        t_start: float,
        fault_id: str | None,
        abort_after_headers: bool = False,
    ) -> WireEvent:
        deadline = t_start + timeout_s

        # --- status line and headers ------------------------------------
        try:
            head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=max(deadline - time.perf_counter(), 0.01)
            )
        except TimeoutError as exc:
            self._record(
                direction=Direction.LIFECYCLE,
                channel=Channel.HTTP,
                note=f"header timeout after {timeout_s}s (no status line)",
            )
            raise TransportTimeoutError(
                TimeoutCause.TARGET_NO_RESPONSE, f"response headers not received in {timeout_s}s"
            ) from exc
        except (asyncio.IncompleteReadError, ConnectionResetError) as exc:
            self._record(
                direction=Direction.LIFECYCLE,
                channel=Channel.HTTP,
                note=f"connection reset before response headers: {type(exc).__name__}",
            )
            raise TransportTimeoutError(
                TimeoutCause.TARGET_NO_RESPONSE, "connection reset before any response headers"
            ) from exc

        first_byte_ms = (time.perf_counter() - t_start) * 1000
        head_text = head.decode("utf-8", errors="replace")
        status_line, _, header_block = head_text.partition("\r\n")
        parts = status_line.split(" ", 2)
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

        response_headers: dict[str, str] = {}
        for line in header_block.split("\r\n"):
            if ":" in line:
                name, _, value = line.partition(":")
                response_headers[name.strip()] = value.strip()

        # --- body -------------------------------------------------------
        length = int(response_headers.get("Content-Length") or 0)
        body = b""
        body_error: str | None = None

        if abort_after_headers:
            # The 2025 binding lets a server answer a GET with an event stream
            # that never completes. Reading to completion would hang against a
            # conforming server, and the assertion is about the status line and
            # the content type — nothing here consumes a stream or claims to.
            return self._record(
                direction=Direction.INBOUND,
                channel=Channel.HTTP,
                message=None,
                parse_error=None,
                headers={
                    **response_headers,
                    ":status": str(status),
                    ":first_byte_ms": f"{first_byte_ms:.1f}",
                    ":aborted": "after_headers",
                },
                fault_applied=fault_id,
                note="probe stopped after the response headers; no body was read",
            )

        try:
            remaining = max(deadline - time.perf_counter(), 0.01)
            if length:
                body = await asyncio.wait_for(reader.readexactly(length), timeout=remaining)
            else:
                body = await asyncio.wait_for(reader.read(MAX_BODY_CAPTURE), timeout=remaining)
        except TimeoutError:
            body_error = f"response body did not complete within {timeout_s}s"
            self._record(
                direction=Direction.LIFECYCLE,
                channel=Channel.HTTP,
                note=f"body-completion timeout after {timeout_s}s (headers arrived at "
                f"{first_byte_ms:.0f}ms)",
            )
            raise TransportTimeoutError(TimeoutCause.TARGET_NO_RESPONSE, body_error) from None
        except (asyncio.IncompleteReadError, ConnectionResetError) as exc:
            partial = getattr(exc, "partial", b"")
            self._record(
                direction=Direction.LIFECYCLE,
                channel=Channel.HTTP,
                raw=partial or None,
                note=f"connection reset mid-body after {len(partial)} bytes",
            )
            raise TransportTimeoutError(
                TimeoutCause.TARGET_NO_RESPONSE,
                f"connection reset after {len(partial)} bytes of body",
            ) from exc

        complete_ms = (time.perf_counter() - t_start) * 1000

        message: dict[str, Any] | None = None
        parse_error: str | None = None
        if body:
            try:
                decoded = json.loads(body)
                if isinstance(decoded, dict):
                    message = decoded
                else:
                    parse_error = f"top-level JSON value is {type(decoded).__name__}"
            except json.JSONDecodeError as exc:
                parse_error = f"invalid JSON in response body: {exc}"
        else:
            parse_error = "response had an empty body"

        annotated = {
            **response_headers,
            ":status": str(status),
            ":first_byte_ms": f"{first_byte_ms:.1f}",
            ":body_complete_ms": f"{complete_ms:.1f}",
        }
        return self._record(
            direction=Direction.INBOUND,
            channel=Channel.HTTP,
            raw=body,
            message=message,
            parse_error=parse_error,
            headers=annotated,
            fault_applied=fault_id,
        )
