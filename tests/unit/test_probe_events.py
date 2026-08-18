"""Lowering probe output into canonical wire events.

The risk this guards against is quiet loss. The probe records more about an HTTP
exchange than a stdio event has room for — status, method, path, phase, step id,
two separate timing measurements, a truncation flag. If the mapping silently
drops any of them to fit the existing schema, the artifact still validates and
still looks complete, and the missing facts are the ones a reader would need to
tell a stalled body from a dead server.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from interop_lab.events.model import Channel, Direction, WireEvent
from interop_lab.probe.events import (
    build_trace,
    responses_by_step,
    step_timed_out,
    to_wire_event,
    unmapped_fields,
)

STARTED = datetime(2026, 1, 1, tzinfo=UTC)


def inbound_event(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "seq": 4,
        "at_ms": 12.5,
        "direction": "inbound",
        "phase": "scenario",
        "step_id": "list",
        "method": "POST",
        "path": "/mcp",
        "status": 200,
        "headers": {"Content-Type": "application/json"},
        "body": '{"jsonrpc":"2.0","id":1,"result":{}}',
        "body_bytes": 36,
        "body_sha256": "a" * 64,
        "body_truncated": False,
        "message": {"jsonrpc": "2.0", "id": 1, "result": {}},
        "first_byte_ms": 3.25,
        "body_complete_ms": 11.5,
    }
    base.update(overrides)
    return base


def test_every_http_fact_survives_the_translation() -> None:
    """Each of these used to have nowhere to go, or lived in a pseudo-header."""
    event = to_wire_event(inbound_event(), seq=0, started_at=STARTED)

    assert event.direction is Direction.INBOUND
    assert event.channel is Channel.HTTP
    assert event.transport == "streamable_http"
    assert event.phase == "scenario"
    assert event.step_id == "list"
    assert event.http_method == "POST"
    assert event.http_path == "/mcp"
    assert event.http_status == 200
    assert event.first_byte_ms == 3.25
    assert event.body_complete_ms == 11.5
    assert event.body_truncated is False
    assert event.raw_len == 36
    assert event.message == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_the_probes_own_hash_of_the_exact_bytes_is_preferred() -> None:
    """The hash covers what crossed the wire, before decoding and redaction."""
    event = to_wire_event(inbound_event(), seq=0, started_at=STARTED)
    assert event.raw_sha256 == "a" * 64


def test_a_status_line_is_not_smuggled_into_the_header_map() -> None:
    """A pseudo-header is indistinguishable from one the target actually sent."""
    event = to_wire_event(inbound_event(), seq=0, started_at=STARTED)
    assert event.headers is not None
    assert ":status" not in event.headers
    assert ":first_byte_ms" not in event.headers
    assert set(event.headers) == {"Content-Type"}


def test_a_timeout_carries_its_cause_and_the_header_timing() -> None:
    """The distinction between a stalled body and a dead server must survive."""
    event = to_wire_event(
        {
            "seq": 5,
            "at_ms": 2500.0,
            "direction": "lifecycle",
            "phase": "scenario",
            "step_id": "slow",
            "status": 200,
            "first_byte_ms": 0.9,
            "body_truncated": True,
            "note": "body-completion timeout after 2.5s (headers arrived at 1ms)",
            "timed_out": True,
            "timeout_cause": "target_no_response",
        },
        seq=0,
        started_at=STARTED,
    )
    assert event.timeout_cause == "target_no_response"
    assert event.first_byte_ms == 0.9
    assert event.body_truncated is True
    assert "body-completion timeout" in (event.note or "")
    assert "timed_out=True" in (event.note or "")


def test_a_transport_error_is_recorded_as_such() -> None:
    event = to_wire_event(
        {
            "seq": 1,
            "at_ms": 5.0,
            "direction": "lifecycle",
            "phase": "scenario",
            "step_id": "reset",
            "transport_error": "ConnectionError: peer closed the connection",
        },
        seq=0,
        started_at=STARTED,
    )
    assert event.transport_error is not None
    assert "ConnectionError" in event.transport_error


def test_a_field_with_no_dedicated_member_reaches_the_note_rather_than_vanishing() -> None:
    event = to_wire_event(
        {"seq": 1, "at_ms": 1.0, "direction": "lifecycle", "captured": "mcp_session_id"},
        seq=0,
        started_at=STARTED,
    )
    assert "captured=mcp_session_id" in (event.note or "")


def test_the_mapping_reports_any_probe_field_it_would_drop() -> None:
    """The guard that keeps the probe and this module from drifting apart.

    When the probe grows a field, this fails until the mapping grows with it.
    Without it, a new probe field simply stops appearing in artifacts and
    nothing anywhere says so.
    """
    assert unmapped_fields([inbound_event()]) == []
    assert unmapped_fields([inbound_event(a_new_probe_field=1)]) == ["a_new_probe_field"]


def test_lifecycle_events_are_ordered_before_the_traffic_they_enabled() -> None:
    lifecycle = [
        WireEvent(
            seq=99,
            monotonic_ns=0,
            wall_clock=STARTED.isoformat(),
            direction=Direction.LIFECYCLE,
            channel=Channel.PROCESS,
            transport="streamable_http",
            note="isolated network created",
        )
    ]
    trace = build_trace("run", [inbound_event()], started_at=STARTED, lifecycle=lifecycle)

    assert [e.seq for e in trace.events] == [0, 1]
    assert trace.events[0].note == "isolated network created"
    assert trace.events[1].direction is Direction.INBOUND


def test_a_fault_is_attributed_to_the_request_it_altered_not_the_response() -> None:
    """Attaching it to both would double-count every fault in every report."""
    events = [
        inbound_event(direction="outbound", status=None),
        inbound_event(),
    ]
    trace = build_trace("run", events, started_at=STARTED, fault_by_step={"list": "corrupt"})

    outbound, inbound = trace.events
    assert outbound.fault_applied == "corrupt"
    assert inbound.fault_applied is None


def test_only_scenario_responses_are_offered_to_the_assertion_engine() -> None:
    """A setup response graded by a scenario assertion is a fabricated result."""
    events = [
        inbound_event(phase="setup", step_id="__setup_initialize"),
        inbound_event(phase="scenario", step_id="list"),
        inbound_event(phase="cleanup", step_id="__cleanup_delete_session"),
    ]
    trace = build_trace("run", events, started_at=STARTED)

    responses = responses_by_step(trace)
    assert set(responses) == {"list"}


def test_the_first_response_for_a_step_wins() -> None:
    events = [
        inbound_event(step_id="list", status=200),
        inbound_event(step_id="list", status=500),
    ]
    trace = build_trace("run", events, started_at=STARTED)
    assert responses_by_step(trace)["list"].http_status == 200


def test_a_step_failure_reports_which_kind_it_was() -> None:
    timeout_trace = build_trace(
        "run",
        [
            {
                "seq": 1,
                "at_ms": 1.0,
                "direction": "lifecycle",
                "phase": "scenario",
                "step_id": "slow",
                "timeout_cause": "target_no_response",
            }
        ],
        started_at=STARTED,
    )
    failed, cause, error = step_timed_out(timeout_trace, "slow")
    assert failed and cause == "target_no_response" and error is None

    reset_trace = build_trace(
        "run",
        [
            {
                "seq": 1,
                "at_ms": 1.0,
                "direction": "lifecycle",
                "phase": "scenario",
                "step_id": "reset",
                "transport_error": "ConnectionError",
            }
        ],
        started_at=STARTED,
    )
    failed, cause, error = step_timed_out(reset_trace, "reset")
    assert failed and error == "ConnectionError"


def test_a_step_that_answered_is_not_reported_as_failed() -> None:
    trace = build_trace("run", [inbound_event()], started_at=STARTED)
    assert step_timed_out(trace, "list") == (False, None, None)


def test_a_trace_of_probe_events_round_trips_through_jsonl() -> None:
    """The canonical trace is the artifact; it has to survive being written out."""
    trace = build_trace("run", [inbound_event()], started_at=STARTED)
    text = trace.to_jsonl()
    restored = type(trace).from_jsonl("run", text)
    assert restored.events[0].http_status == 200
    assert restored.events[0].first_byte_ms == 3.25
    assert restored.events[0].phase == "scenario"
