"""Lower probe output into the canonical ``WireEvent`` model.

The probe writes a flat, dependency-free JSON shape because it cannot import
this package. This module is the single place that translates it, so the
assertion engine, the normaliser, the differential engine and the minimiser all
see contained HTTP traffic in exactly the form they already understand.

Two rules govern the translation:

* **Nothing is invented.** A field absent from the probe's output stays absent
  here. The canonical event is a view of the evidence, not a reconstruction of
  what probably happened.
* **Nothing transport-specific is dropped to fit.** Status, method, path,
  phase, step id and both timing measurements have first-class homes on
  ``WireEvent``; they are not flattened into a header map or a prose note.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from interop_lab.events.model import Channel, Direction, WireEvent, WireTrace
from interop_lab.security.redaction import redact_headers, redact_text

TRANSPORT = "streamable_http"

#: Probe fields that are represented by a dedicated ``WireEvent`` member. Any
#: probe field outside this set and outside ``_NOTE_FIELDS`` would be silently
#: lost, so ``unmapped_fields`` reports it rather than letting it disappear.
_MAPPED = frozenset(
    {
        "seq",
        "at_ms",
        "direction",
        "phase",
        "step_id",
        "method",
        "path",
        "status",
        "headers",
        "body",
        "body_bytes",
        "body_sha256",
        "body_truncated",
        "message",
        "parse_error",
        "first_byte_ms",
        "body_complete_ms",
        "timeout_cause",
        "transport_error",
        "note",
    }
)

#: Probe fields that carry a fact with no dedicated member. They are appended to
#: ``note`` so they remain in the artifact instead of being discarded.
_NOTE_FIELDS = (
    "timed_out",
    "aborted",
    "captured",
    "capture_missed",
    "reused_connection",
    "plan_error",
    "skipped",
    "output_truncated",
)


def unmapped_fields(events: list[dict[str, Any]]) -> list[str]:
    """Probe fields this module would drop. Empty is the only acceptable value.

    Asserted in the tests: when the probe grows a field, the mapping has to grow
    with it, or the evidence quietly stops containing something the probe went
    to the trouble of recording.
    """
    seen: set[str] = set()
    for event in events:
        seen.update(event)
    return sorted(seen - _MAPPED - set(_NOTE_FIELDS))


def _direction(raw: str) -> Direction:
    match raw:
        case "outbound":
            return Direction.OUTBOUND
        case "inbound":
            return Direction.INBOUND
        case "diagnostic":
            return Direction.DIAGNOSTIC
        case _:
            return Direction.LIFECYCLE


def _note_for(event: dict[str, Any]) -> str | None:
    parts: list[str] = []
    note = event.get("note")
    if isinstance(note, str) and note:
        parts.append(note)
    for field in _NOTE_FIELDS:
        if field in event and event[field] not in (None, False):
            parts.append(f"{field}={event[field]}")
    return "; ".join(parts) or None


def to_wire_event(
    event: dict[str, Any], *, seq: int, started_at: datetime, fault_applied: str | None = None
) -> WireEvent:
    """Translate one probe event."""
    at_ms = float(event.get("at_ms") or 0.0)
    headers = event.get("headers")
    safe_headers: dict[str, str] | None = None
    redacted = False
    if isinstance(headers, dict):
        safe_headers, redacted = redact_headers({str(k): str(v) for k, v in headers.items()})

    body = event.get("body")
    raw_text: str | None = None
    if isinstance(body, str):
        raw_text, body_redacted = redact_text(body)
        redacted = redacted or body_redacted

    # The probe hashes the exact bytes it sent or received, before any decoding
    # or redaction. That hash is the integrity anchor, so it is preferred over
    # anything recomputed here from a lossy text rendering.
    digest = event.get("body_sha256")
    if not isinstance(digest, str) and isinstance(body, str):
        digest = hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()

    message = event.get("message")
    status = event.get("status")

    return WireEvent(
        seq=seq,
        monotonic_ns=int(at_ms * 1_000_000),
        wall_clock=(started_at + timedelta(milliseconds=at_ms)).isoformat(),
        direction=_direction(str(event.get("direction", "lifecycle"))),
        channel=Channel.HTTP,
        transport=TRANSPORT,
        raw_sha256=digest if isinstance(digest, str) else None,
        raw_len=int(event["body_bytes"]) if isinstance(event.get("body_bytes"), int) else None,
        raw_text=raw_text,
        message=message if isinstance(message, dict) else None,
        parse_error=(
            str(event["parse_error"]) if isinstance(event.get("parse_error"), str) else None
        ),
        headers=safe_headers,
        fault_applied=fault_applied,
        note=_note_for(event),
        redacted=redacted,
        phase=str(event["phase"]) if isinstance(event.get("phase"), str) else None,
        step_id=str(event["step_id"]) if isinstance(event.get("step_id"), str) else None,
        http_method=str(event["method"]) if isinstance(event.get("method"), str) else None,
        http_path=str(event["path"]) if isinstance(event.get("path"), str) else None,
        http_status=int(status) if isinstance(status, int) else None,
        first_byte_ms=(
            float(event["first_byte_ms"])
            if isinstance(event.get("first_byte_ms"), int | float)
            else None
        ),
        body_complete_ms=(
            float(event["body_complete_ms"])
            if isinstance(event.get("body_complete_ms"), int | float)
            else None
        ),
        body_truncated=bool(event["body_truncated"]) if "body_truncated" in event else None,
        timeout_cause=(
            str(event["timeout_cause"]) if isinstance(event.get("timeout_cause"), str) else None
        ),
        transport_error=(
            str(event["transport_error"]) if isinstance(event.get("transport_error"), str) else None
        ),
    )


def build_trace(
    run_id: str,
    events: list[dict[str, Any]],
    *,
    started_at: datetime | None = None,
    lifecycle: list[WireEvent] | None = None,
    fault_by_step: dict[str, str] | None = None,
) -> WireTrace:
    """Assemble a canonical trace from harness lifecycle events plus probe events.

    Lifecycle events come first and keep the sequence numbers they were given:
    they record what the harness did to obtain the evidence (network created,
    containers started, containment observed) and belong in the same ordered log
    as the traffic, not in a separate file a reader has to correlate by hand.
    """
    trace = WireTrace(run_id=run_id)
    start = started_at or datetime.now(UTC)
    seq = 0
    for event in lifecycle or []:
        trace.append(event.model_copy(update={"seq": seq}))
        seq += 1

    faults = fault_by_step or {}
    for event in events:
        step_id = event.get("step_id")
        applied = faults.get(str(step_id)) if isinstance(step_id, str) else None
        # A fault is attributed to the outbound event it altered. Attaching it to
        # the response as well would double-count it in every report that
        # summarises faults by event.
        if event.get("direction") != "outbound":
            applied = None
        trace.append(to_wire_event(event, seq=seq, started_at=start, fault_applied=applied))
        seq += 1
    return trace


def responses_by_step(trace: WireTrace) -> dict[str, WireEvent]:
    """The inbound event for each scenario step, keyed by step id.

    Scenario phase only: a setup response must never be graded by a scenario
    assertion, and a cleanup response must never be mistaken for one either.
    """
    found: dict[str, WireEvent] = {}
    for event in trace.events:
        if event.direction is not Direction.INBOUND:
            continue
        if event.phase != "scenario" or event.step_id is None:
            continue
        found.setdefault(event.step_id, event)
    return found


def step_timed_out(trace: WireTrace, step_id: str) -> tuple[bool, str | None, str | None]:
    """Whether a step ended without a response, and why.

    Returns ``(failed, timeout_cause, transport_error)``. A connection reset and
    an expired deadline are both "no response arrived", but they are different
    observations about the target and the classification depends on which.
    """
    for event in trace.events:
        if event.step_id != step_id or event.phase != "scenario":
            continue
        if event.direction is Direction.LIFECYCLE and (
            event.timeout_cause is not None or event.transport_error is not None
        ):
            return True, event.timeout_cause, event.transport_error
    return False, None, None
