"""Canonical, transport-neutral wire event format.

Every transport lowers its traffic into ``WireEvent`` so scenarios, assertions,
differential comparison, replay and minimisation never depend on whether the
bytes came from a pipe or a socket.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from interop_lab.naming import ARTIFACT_SCHEMA_VERSION


class Direction(StrEnum):
    #: Harness -> target.
    OUTBOUND = "outbound"
    #: Target -> harness.
    INBOUND = "inbound"
    #: Target diagnostics (stderr), never protocol traffic.
    DIAGNOSTIC = "diagnostic"
    #: Harness-observed lifecycle fact (spawn, exit, timeout).
    LIFECYCLE = "lifecycle"


class Channel(StrEnum):
    STDIN = "stdin"
    STDOUT = "stdout"
    STDERR = "stderr"
    HTTP = "http"
    PROCESS = "process"


class WireEvent(BaseModel):
    """One observed event on the wire, in order."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(description="Monotonic ordering within a run, starting at 0.")
    monotonic_ns: int = Field(description="perf_counter_ns relative to run start.")
    wall_clock: str = Field(description="ISO-8601 UTC.")
    direction: Direction
    channel: Channel
    transport: str

    raw_sha256: str | None = None
    raw_len: int | None = None
    #: Redacted, decoded text. May be lossy; ``raw_sha256`` is the integrity anchor.
    raw_text: str | None = None
    #: Parsed JSON-RPC message when the payload was valid JSON.
    message: dict[str, Any] | None = None
    parse_error: str | None = None

    headers: dict[str, str] | None = None
    #: Id of the fault applied at this boundary, when the harness mutated traffic.
    fault_applied: str | None = None
    #: Free-form lifecycle detail (exit code, signal, timeout cause).
    note: str | None = None
    redacted: bool = False

    # ---------------------------------------------------------- HTTP facts
    #
    # These used to be smuggled into `headers` under pseudo-names (`:status`,
    # `:first_byte_ms`) because the schema had nowhere for them. That worked
    # until something needed to *read* them: a pseudo-header is a string in a
    # map of real headers, indistinguishable from one the target actually sent.
    # They are first-class here so transport detail survives the trip into the
    # canonical model instead of being flattened to fit it.
    #
    # Every one is optional. A stdio event carries none of them, and an artifact
    # written before they existed still validates.

    #: Which part of the run produced this event: setup, scenario or cleanup.
    #: Fault occurrence indices count scenario traffic only, so a reader can
    #: check that attribution rather than take it on trust.
    phase: str | None = None
    #: The scenario or plan step this event belongs to.
    step_id: str | None = None
    http_method: str | None = None
    http_path: str | None = None
    http_status: int | None = None
    #: Milliseconds from request write to the first response byte, and to body
    #: completion. Two numbers because a stalled body and a stalled header are
    #: different defects, and one "elapsed" figure cannot tell them apart.
    first_byte_ms: float | None = None
    body_complete_ms: float | None = None
    #: True when the recorded body is a bounded prefix of what arrived.
    body_truncated: bool | None = None
    #: Whose deadline expired, when one did. Mirrors ``TimeoutCause``.
    timeout_cause: str | None = None
    #: Connection-level failure (reset, refused, closed early), as observed.
    transport_error: str | None = None

    @staticmethod
    def digest(raw: bytes) -> str:
        return hashlib.sha256(raw).hexdigest()

    @property
    def rpc_id(self) -> str | int | None:
        if self.message is None:
            return None
        value = self.message.get("id")
        return value if isinstance(value, str | int) else None

    @property
    def rpc_method(self) -> str | None:
        if self.message is None:
            return None
        value = self.message.get("method")
        return value if isinstance(value, str) else None

    @property
    def is_error_response(self) -> bool:
        return self.message is not None and "error" in self.message

    def error_code(self) -> int | None:
        if self.message is None:
            return None
        err = self.message.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            if isinstance(code, int):
                return code
        return None


class WireTrace(BaseModel):
    """An ordered event log plus the provenance needed to trust it."""

    model_config = ConfigDict(extra="forbid")

    artifact_schema_version: str = ARTIFACT_SCHEMA_VERSION
    run_id: str
    events: list[WireEvent] = Field(default_factory=list)

    def append(self, event: WireEvent) -> None:
        self.events.append(event)

    def protocol_events(self) -> list[WireEvent]:
        """Events that carry protocol traffic, excluding diagnostics and lifecycle."""
        return [e for e in self.events if e.direction in (Direction.OUTBOUND, Direction.INBOUND)]

    def responses(self) -> list[WireEvent]:
        return [e for e in self.protocol_events() if e.direction is Direction.INBOUND]

    def response_for(self, rpc_id: str | int) -> WireEvent | None:
        for event in self.responses():
            if event.rpc_id == rpc_id:
                return event
        return None

    def to_jsonl(self) -> str:
        return "\n".join(e.model_dump_json(exclude_none=True) for e in self.events) + "\n"

    @classmethod
    def from_jsonl(cls, run_id: str, text: str) -> Self:
        events = [WireEvent.model_validate_json(line) for line in text.splitlines() if line.strip()]
        return cls(run_id=run_id, events=events)
