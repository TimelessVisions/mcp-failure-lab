"""stdio transport with full wire capture and boundary fault injection.

The harness acts as the MCP client and treats the target as an untrusted
subprocess: its stdout is protocol traffic, its stderr is diagnostics only, and
its process tree is terminated on the way out regardless of how the run ended.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from interop_lab.artifacts.portability import resolve_executable_tokens
from interop_lab.containment import docker
from interop_lab.containment.docker import ContainmentFailedError, ContainmentUnavailableError
from interop_lab.containment.model import (
    ContainerRuntimeFingerprint,
    ContainerSpec,
    ContainmentOutcome,
    ContainmentRecord,
    ExecutionMode,
)
from interop_lab.containment.resolution import ImageResolutionError, ResolvedImage
from interop_lab.events.model import Channel, Direction, WireEvent, WireTrace
from interop_lab.injection.faults import Boundary, Fault, FaultKind, apply_outbound_fault
from interop_lab.models.core import ProcessOutcome, TargetDefinition, TimeoutCause
from interop_lab.runner.images import ExecutionContext, verify_started_container
from interop_lab.security.redaction import redact_text, scrub_environment
from interop_lab.transports.base import TransportTimeoutError

#: How long to wait for the daemon to finish reaping a container before
#: calling it a leak. Removal is asynchronous.
CONTAINER_REAP_TIMEOUT_S = 10.0

#: Guard against a hostile target streaming unbounded output.
MAX_CAPTURED_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 4 * 1024 * 1024

__all__ = ["MAX_CAPTURED_BYTES", "MAX_LINE_BYTES", "StdioSession", "TransportTimeoutError"]


class StdioSession:
    """A single launched target plus the trace of everything that crossed the wire."""

    def __init__(
        self,
        target: TargetDefinition,
        *,
        run_id: str,
        faults: list[Fault] | None = None,
        cwd: Path | None = None,
        context: ExecutionContext | None = None,
    ) -> None:
        self.target = target
        self.run_id = run_id
        self.faults = faults or []
        self.cwd = cwd
        #: Supplied by the strategy. A host target never needs it; a contained
        #: one cannot start without it, and says so rather than improvising.
        self.context = context
        self.trace = WireTrace(run_id=run_id)
        self.process: asyncio.subprocess.Process | None = None
        self.outcome = ProcessOutcome()
        self.faults_applied: list[str] = []
        #: Populated by start(); None only if start() was never reached.
        self.containment: ContainmentRecord | None = None
        self.container_name: str | None = None

        self._seq = 0
        self._t0 = time.perf_counter_ns()
        self._outbound_index = 0
        self._inbound_index = 0
        self._captured_bytes = 0
        self._stderr_task: asyncio.Task[None] | None = None
        self._next_rpc_id = 1
        # Faults belong to the scenario, not to setup. Enabled by begin_scenario().
        self._faults_active = False

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
            await self._start_contained()
        else:
            await self._start_on_host()
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _start_on_host(self) -> None:
        env = scrub_environment(self.target.env_allowlist, self.target.env)
        declared = Path(self.target.working_dir) if self.target.working_dir else None
        working_dir = self.cwd or declared

        self.containment = ContainmentRecord(
            mode=ExecutionMode.HOST,
            provenance=self.target.provenance,
            outcome=ContainmentOutcome.NOT_CONTAINED_BY_DESIGN,
            stated_limits=[
                "This target ran as an ordinary local process. Nothing isolated it "
                "beyond process-group cleanup and a scrubbed environment."
            ],
        )
        self._record(
            direction=Direction.LIFECYCLE,
            channel=Channel.PROCESS,
            # Log the portable form, not the resolved machine path.
            note=f"spawn {' '.join(self.target.launch_command)}",
        )
        command = resolve_executable_tokens(self.target.launch_command)
        self.process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(working_dir) if working_dir else None,
            # New process group so the whole tree can be signalled on cleanup.
            start_new_session=True,
            limit=MAX_LINE_BYTES,
        )

    async def _start_contained(self) -> None:
        """Launch inside a container, then verify the container is what was asked for.

        Every failure path here raises. There is no branch that continues on the
        host: a run recorded as contained must have been contained, or the
        artifact is a false statement about how the evidence was produced.
        """
        spec = self.target.container
        if spec is None:  # pragma: no cover - the model validator forbids this
            raise ContainmentUnavailableError(
                f"target {self.target.target_id!r} requests containment without a spec"
            )

        if self.context is None:
            raise ContainmentUnavailableError(
                f"target {self.target.target_id!r} requests containment but this session "
                "was given no execution context, so the image it should run cannot be "
                "resolved. Every path that launches a container is handed one; this is "
                "a wiring error in the harness, not a fact about the target."
            )

        # Checked before Docker: a locally built target with no build resolution
        # is a configuration error on any machine, and reporting a daemon
        # problem instead would send someone to fix the wrong thing.
        try:
            self.context.resolver.require_resolvable(self.target)
        except ImageResolutionError as exc:
            self.containment = ContainmentRecord(
                mode=ExecutionMode.CONTAINER,
                provenance=self.target.provenance,
                outcome=ContainmentOutcome.CONTAINMENT_UNAVAILABLE,
                requested=spec,
                unavailable_reason=str(exc),
            )
            raise ContainmentUnavailableError(str(exc)) from exc

        try:
            base = await docker.ensure_available()
        except ContainmentUnavailableError as exc:
            self.containment = ContainmentRecord(
                mode=ExecutionMode.CONTAINER,
                provenance=self.target.provenance,
                outcome=ContainmentOutcome.CONTAINMENT_UNAVAILABLE,
                requested=spec,
                unavailable_reason=str(exc),
            )
            raise

        # Resolved before anything is started. For a registry source this is the
        # pinned digest; for a local build it is an image ID from the build
        # resolution, already checked to exist in this engine. A failure here
        # means the harness does not know what it would be running.
        try:
            resolved = await self.context.resolver.resolve(self.target)
        except ImageResolutionError as exc:
            self.containment = ContainmentRecord(
                mode=ExecutionMode.CONTAINER,
                provenance=self.target.provenance,
                outcome=ContainmentOutcome.CONTAINMENT_UNAVAILABLE,
                requested=spec,
                unavailable_reason=str(exc),
            )
            raise ContainmentUnavailableError(str(exc)) from exc

        # Containers do not inherit the harness's environment at all; only the
        # explicitly declared values cross the boundary.
        scrubbed = scrub_environment([], self.target.env)
        env = {k: v for k, v in scrubbed.items() if k in self.target.env}
        self.container_name = f"{docker.RESOURCE_PREFIX}-{self.run_id}"[:63]
        argv = docker.build_run_argv(
            spec,
            image_reference=resolved.launch_reference,
            container_name=self.container_name,
            command=self.target.launch_command,
            env=env,
            # Resolved here rather than stored: the absolute path is needed by
            # the runtime but must never reach an artifact.
            repo_root=str(self.context.repo_root),
        )

        self._record(
            direction=Direction.LIFECYCLE,
            channel=Channel.PROCESS,
            note=(
                f"container start {resolved.launch_reference} "
                f"({resolved.source_kind.value}) network={spec.network.value}"
            ),
        )

        self.process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={},
            start_new_session=True,
            limit=MAX_LINE_BYTES,
        )

        # Record what was *asked for* alongside what was observed, so the record
        # carries both halves of the comparison rather than only its result.
        base = base.model_copy(update={"requested_repo_digest": resolved.repository_digest})
        observed, weaknesses = await self._verify_containment(spec, base, resolved)

        # The identity of the *running* container, folded in before a single
        # protocol byte is written to its stdin. start() returns to the strategy,
        # which then performs the handshake; nothing has been sent yet.
        resolved = verify_started_container(resolved, observed.observed_image_id)

        outcome = (
            ContainmentOutcome.CONTAINMENT_WEAKER_THAN_REQUESTED
            if weaknesses
            else ContainmentOutcome.CONTAINED
        )
        self.containment = ContainmentRecord(
            mode=ExecutionMode.CONTAINER,
            provenance=self.target.provenance,
            outcome=outcome,
            requested=spec,
            observed=observed,
            resolved_image=resolved,
            weaknesses=weaknesses,
            stated_limits=docker.stated_limits(spec),
        )
        if weaknesses:
            raise ContainmentFailedError(
                "the container did not have every requested control, so this run cannot "
                "be cited as evidence about the target: " + "; ".join(weaknesses)
            )

    async def _verify_containment(
        self, spec: ContainerSpec, base: ContainerRuntimeFingerprint, resolved: ResolvedImage
    ) -> tuple[ContainerRuntimeFingerprint, list[str]]:
        """Wait for the container to exist, then read its real configuration back."""
        deadline = time.monotonic() + spec.startup_timeout_s
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                observed = await docker.observe(self.container_name or "", base=base)
            except ContainmentFailedError as exc:
                last_error = exc
                await asyncio.sleep(0.2)
                continue
            return observed, docker.compare(spec, observed, resolved=resolved)

        self.containment = ContainmentRecord(
            mode=ExecutionMode.CONTAINER,
            provenance=self.target.provenance,
            outcome=ContainmentOutcome.CONTAINMENT_FAILED,
            requested=spec,
            resolved_image=resolved,
            unavailable_reason=str(last_error) if last_error else "container never appeared",
        )
        raise ContainmentFailedError(
            f"container {self.container_name} did not start within {spec.startup_timeout_s}s: "
            f"{last_error}"
        )

    async def close(self) -> None:
        """Terminate the whole process tree and record whether cleanup succeeded."""
        process = self.process
        if process is None:
            # A container may exist even when no client process does, if start()
            # failed between `docker run` and the readiness check.
            await self._reclaim_container()
            return

        if process.returncode is None:
            self.outcome.killed_by_harness = True
            await self._terminate_tree(process)

        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task

        for stream in (process.stdin,):
            if stream is not None and not stream.is_closing():
                stream.close()
                with contextlib.suppress(Exception):
                    await stream.wait_closed()

        # Killing the local `docker run` client does not necessarily stop the
        # container, and a surviving container is a cleanup failure exactly as a
        # surviving process tree is.
        await self._reclaim_container()

        code = process.returncode
        self.outcome.exit_code = code
        if code is not None and code < 0:
            self.outcome.signal = -code

        self._record(
            direction=Direction.LIFECYCLE,
            channel=Channel.PROCESS,
            note=f"exit code={code} killed_by_harness={self.outcome.killed_by_harness}",
        )

    async def _reclaim_container(self) -> None:
        """Remove the container, recording a leak rather than ignoring it.

        Removal is asynchronous: a container started with ``--rm`` is reaped by
        the daemon after it exits, and one just given ``rm --force`` can still
        appear in ``docker ps --all`` while its state is ``Removing``. Declaring
        a leak on the first look would report a harness cleanup failure against
        targets that were in fact reclaimed correctly — which under the rules
        here is the worst kind of wrong answer, a harness fault attributed to the
        run.
        """
        if self.container_name is None:
            return
        with contextlib.suppress(Exception):
            await docker.remove_container(self.container_name)

        deadline = time.monotonic() + CONTAINER_REAP_TIMEOUT_S
        while True:
            code, out, _ = await docker.inspect_state(self.container_name)
            if code != 0 or not out.strip():
                return
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.2)

        self.outcome.cleanup_ok = False
        self.outcome.cleanup_detail = (
            f"container {self.container_name} still present "
            f"{CONTAINER_REAP_TIMEOUT_S}s after `docker rm --force`"
        )

    async def _terminate_tree(self, process: asyncio.subprocess.Process) -> None:
        """SIGTERM the process group, escalate to SIGKILL, then verify it is gone."""
        try:
            pgid = os.getpgid(process.pid)
        except (ProcessLookupError, PermissionError, AttributeError):
            pgid = None

        def signal_group(sig: signal.Signals) -> None:
            if pgid is not None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pgid, sig)
            else:  # pragma: no cover - fallback for platforms without process groups
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

    # ---------------------------------------------------------------- capture

    def _record(
        self,
        *,
        direction: Direction,
        channel: Channel,
        raw: bytes | None = None,
        message: dict[str, Any] | None = None,
        parse_error: str | None = None,
        fault_applied: str | None = None,
        note: str | None = None,
    ) -> WireEvent:
        text: str | None = None
        redacted = False
        if raw is not None:
            decoded = raw.decode("utf-8", errors="replace")
            text, redacted = redact_text(decoded)

        event = WireEvent(
            seq=self._seq,
            monotonic_ns=time.perf_counter_ns() - self._t0,
            wall_clock=datetime.now(UTC).isoformat(),
            direction=direction,
            channel=channel,
            transport="stdio",
            raw_sha256=WireEvent.digest(raw) if raw is not None else None,
            raw_len=len(raw) if raw is not None else None,
            raw_text=text,
            message=message,
            parse_error=parse_error,
            fault_applied=fault_applied,
            note=note,
            redacted=redacted,
        )
        self._seq += 1
        self.trace.append(event)
        return event

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
        except (asyncio.CancelledError, ValueError):
            raise
        except Exception as exc:  # pragma: no cover - defensive
            self._record(
                direction=Direction.LIFECYCLE,
                channel=Channel.PROCESS,
                note=f"stderr drain failed: {exc!r}",
            )

    # ----------------------------------------------------------------- rpc io

    def next_rpc_id(self) -> int:
        value = self._next_rpc_id
        self._next_rpc_id += 1
        return value

    def begin_scenario(self) -> None:
        """Arm faults and start counting occurrences at the scenario's first message.

        Fault triggers address the nth message at a boundary. Setup traffic — the
        stateful `initialize` handshake — is not part of the scenario. If faults
        applied to it, ``occurrence: 0`` would mean "the first scenario request"
        on a stateless profile and "the initialize request" on a stateful one:
        the same fault definition would inject at a different place depending on
        the dated profile, and the resulting failure would be attributed to the
        wrong thing entirely.
        """
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

    async def send(self, message: dict[str, Any]) -> None:
        """Serialise and send one JSON-RPC message, applying outbound faults."""
        process = self.process
        if process is None or process.stdin is None:
            raise RuntimeError("session not started")

        raw = json.dumps(message).encode()
        method = message.get("method") if isinstance(message.get("method"), str) else None
        fault = self._select_fault(Boundary.OUTBOUND_MESSAGE, self._outbound_index, method)
        self._outbound_index += 1

        payload: bytes | None = raw
        fault_id: str | None = None
        if fault is not None:
            fault_id = fault.fault_id
            self.faults_applied.append(fault.fault_id)
            if fault.kind is FaultKind.DELAY:
                await asyncio.sleep(fault.delay_ms / 1000)
            elif fault.kind is FaultKind.TERMINATE_PROCESS:
                # Attribute the death to the harness. Without this the exit
                # signal looks exactly like a spontaneous crash, and the run is
                # classified CRASH — blaming the target for a kill we performed.
                self.outcome.killed_by_harness = True
                await self._terminate_tree(process)
                self._record(
                    direction=Direction.LIFECYCLE,
                    channel=Channel.PROCESS,
                    fault_applied=fault.fault_id,
                    note=f"process terminated by fault {fault.fault_id}",
                )
                return
            else:
                payload = apply_outbound_fault(raw, message, fault)

        if payload is None:
            self._record(
                direction=Direction.OUTBOUND,
                channel=Channel.STDIN,
                message=message,
                fault_applied=fault_id,
                note="message dropped by fault",
            )
            return

        # Reflect what actually went on the wire, not what we intended to send.
        try:
            sent_message = json.loads(payload)
            parse_error = None
        except json.JSONDecodeError as exc:
            sent_message = None
            parse_error = f"harness sent intentionally malformed JSON: {exc}"

        self._record(
            direction=Direction.OUTBOUND,
            channel=Channel.STDIN,
            raw=payload,
            message=sent_message if isinstance(sent_message, dict) else None,
            parse_error=parse_error,
            fault_applied=fault_id,
        )
        if not await self._write(process, payload):
            raise TransportTimeoutError(
                TimeoutCause.TARGET_NO_RESPONSE,
                "target closed its input stream; it cannot receive this request",
            )

        if fault is not None and fault.kind is FaultKind.DUPLICATE:
            await self._write(process, payload)
            self._record(
                direction=Direction.OUTBOUND,
                channel=Channel.STDIN,
                raw=payload,
                message=sent_message if isinstance(sent_message, dict) else None,
                fault_applied=fault.fault_id,
                note="duplicate emitted by fault",
            )

    @property
    def supports_raw(self) -> bool:
        """stdio is byte-framed, so arbitrary malformed payloads can be sent."""
        return True

    async def _write(self, process: asyncio.subprocess.Process, payload: bytes) -> bool:
        """Write one framed message, reporting a dead pipe as an observation.

        A target that has exited closes its stdin. That is a fact about the
        target, not a bug in the harness, so it is recorded on the trace and
        surfaced through the normal timeout path rather than raising out of the
        executor as HARNESS_ERROR.
        """
        stdin = process.stdin
        if stdin is None:
            return False
        try:
            stdin.write(payload + b"\n")
            await stdin.drain()
        except (ConnectionResetError, BrokenPipeError, RuntimeError) as exc:
            self._record(
                direction=Direction.LIFECYCLE,
                channel=Channel.PROCESS,
                note=f"write to target failed ({type(exc).__name__}); process is gone",
            )
            return False
        return True

    async def send_raw(self, payload: bytes) -> None:
        """Send literal bytes without JSON-RPC construction.

        Used by wire-integrity scenarios where the payload is deliberately not a
        valid message; the harness records that it was intentional.
        """
        process = self.process
        if process is None or process.stdin is None:
            raise RuntimeError("session not started")

        self._outbound_index += 1
        self._record(
            direction=Direction.OUTBOUND,
            channel=Channel.STDIN,
            raw=payload,
            note="raw payload sent verbatim by scenario",
        )
        if not await self._write(process, payload):
            raise TransportTimeoutError(
                TimeoutCause.TARGET_NO_RESPONSE,
                "target closed its input stream; it cannot receive this payload",
            )

    async def receive(self, *, timeout_s: float) -> WireEvent:
        """Read one newline-delimited message from stdout within the deadline."""
        process = self.process
        if process is None or process.stdout is None:
            raise RuntimeError("session not started")

        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout_s)
        except TimeoutError as exc:
            alive = process.returncode is None
            cause = TimeoutCause.TARGET_NO_RESPONSE if alive else TimeoutCause.TRANSPORT_DEADLINE
            self._record(
                direction=Direction.LIFECYCLE,
                channel=Channel.PROCESS,
                note=f"no response within {timeout_s}s (process_alive={alive})",
            )
            raise TransportTimeoutError(
                cause, f"no response within {timeout_s}s (process_alive={alive})"
            ) from exc

        if not line:
            self._record(
                direction=Direction.LIFECYCLE,
                channel=Channel.PROCESS,
                note="stdout closed before a response was received",
            )
            raise TransportTimeoutError(
                TimeoutCause.TARGET_NO_RESPONSE, "stdout closed before a response"
            )

        self._captured_bytes += len(line)
        stripped = line.strip()

        fault = self._select_fault(Boundary.INBOUND_MESSAGE, self._inbound_index, None)
        self._inbound_index += 1
        fault_id: str | None = None
        if fault is not None:
            fault_id = fault.fault_id
            self.faults_applied.append(fault.fault_id)
            if fault.kind is FaultKind.TRUNCATE:
                stripped = stripped[: fault.keep_bytes]
            elif fault.kind is FaultKind.CORRUPT_JSON and fault.replacement is not None:
                stripped = fault.replacement.encode()
            elif fault.kind is FaultKind.DELAY:
                await asyncio.sleep(fault.delay_ms / 1000)

        message: dict[str, Any] | None = None
        parse_error: str | None = None
        try:
            decoded = json.loads(stripped)
            if isinstance(decoded, dict):
                message = decoded
            else:
                parse_error = f"top-level JSON value is {type(decoded).__name__}, not an object"
        except json.JSONDecodeError as exc:
            parse_error = f"invalid JSON on stdout: {exc}"

        return self._record(
            direction=Direction.INBOUND,
            channel=Channel.STDOUT,
            raw=stripped,
            message=message,
            parse_error=parse_error,
            fault_applied=fault_id,
        )

    async def request(
        self, message: dict[str, Any], *, timeout_s: float, expect_id: str | int | None = None
    ) -> WireEvent:
        """Send a request and return the first response carrying the matching id.

        Notifications and unrelated traffic are captured but skipped, so an
        unsolicited notification cannot masquerade as the response.
        """
        await self.send(message)
        deadline = time.monotonic() + timeout_s
        want = expect_id if expect_id is not None else message.get("id")

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportTimeoutError(
                    TimeoutCause.TARGET_NO_RESPONSE,
                    f"no response for id={want!r} within {timeout_s}s",
                )
            event = await self.receive(timeout_s=remaining)
            if event.message is None:
                return event  # Malformed output is itself the observation.
            if "id" not in event.message:
                continue  # Notification: captured, not an answer.
            if want is None or event.rpc_id == want:
                return event
