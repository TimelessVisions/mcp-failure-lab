"""Normalisation must not hide real defects.

These tests exist because normalisation is the most dangerous component in a
differential harness: over-normalise and every comparison silently passes. Each
test asserts that a specific class of genuine defect survives normalisation.
"""

from __future__ import annotations

from typing import Any

import pytest

from interop_lab.events.model import Channel, Direction, WireEvent, WireTrace
from interop_lab.normalization.normalize import normalize_message, normalize_trace


def event(seq: int, message: dict[str, Any], direction: Direction = Direction.INBOUND) -> WireEvent:
    return WireEvent(
        seq=seq,
        monotonic_ns=seq * 1000,
        wall_clock="2026-08-12T00:00:00+00:00",
        direction=direction,
        channel=Channel.STDOUT,
        transport="stdio",
        message=message,
        raw_sha256="0" * 64,
    )


def test_normalizes_volatile_request_id() -> None:
    left = normalize_message({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})
    right = normalize_message({"jsonrpc": "2.0", "id": 99, "result": {"tools": []}})
    assert left == right, "differing request ids alone must not create a difference"


def test_normalizes_advisory_cache_hint() -> None:
    left = normalize_message({"jsonrpc": "2.0", "id": 1, "result": {"tools": [], "ttlMs": 60000}})
    right = normalize_message({"jsonrpc": "2.0", "id": 1, "result": {"tools": [], "ttlMs": 0}})
    assert left == right


@pytest.mark.parametrize(
    ("left_message", "right_message", "reason"),
    [
        (
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32700, "message": "a"}},
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "b"}},
            "different error codes",
        ),
        (
            {"jsonrpc": "2.0", "id": 1, "result": {"content": [], "resultType": "complete"}},
            {"jsonrpc": "2.0", "id": 1, "result": {"content": []}},
            "missing required resultType",
        ),
        (
            {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "a"}]}},
            {"jsonrpc": "2.0", "id": 1, "result": {"content": {"type": "text", "text": "a"}}},
            "content array replaced by an object",
        ),
        (
            {"jsonrpc": "2.0", "id": 1, "result": {"isError": False}},
            {"jsonrpc": "2.0", "id": 1, "result": {"isError": True}},
            "isError flipped",
        ),
        (
            {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}},
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "x"}},
            "result replaced by an error",
        ),
    ],
)
def test_meaningful_defects_survive_normalization(
    left_message: dict[str, Any], right_message: dict[str, Any], reason: str
) -> None:
    assert normalize_message(left_message) != normalize_message(right_message), (
        f"normalisation hid a real defect: {reason}"
    )


def test_error_message_prose_is_normalized_but_code_is_not() -> None:
    left = normalize_message({"jsonrpc": "2.0", "id": 1, "error": {"code": -32700, "message": "x"}})
    right = normalize_message(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32700, "message": "totally different"}}
    )
    assert left == right
    assert left["error"]["code"] == -32700


def test_normalize_trace_drops_diagnostics_and_renumbers() -> None:
    trace = WireTrace(
        run_id="r",
        events=[
            event(0, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, Direction.OUTBOUND),
            WireEvent(
                seq=1,
                monotonic_ns=1,
                wall_clock="2026-08-12T00:00:00+00:00",
                direction=Direction.DIAGNOSTIC,
                channel=Channel.STDERR,
                transport="stdio",
                raw_text="a log line",
            ),
            event(2, {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}),
        ],
    )
    normalized = normalize_trace(trace)
    assert [e.seq for e in normalized.events] == [0, 1]
    assert all(e.direction is not Direction.DIAGNOSTIC for e in normalized.events)


def test_stderr_noise_does_not_shift_protocol_sequence() -> None:
    """An extra log line on one side must not manufacture a false difference."""
    quiet = WireTrace(run_id="quiet", events=[event(0, {"jsonrpc": "2.0", "id": 1, "result": {}})])
    noisy = WireTrace(
        run_id="noisy",
        events=[
            WireEvent(
                seq=0,
                monotonic_ns=0,
                wall_clock="2026-08-12T00:00:00+00:00",
                direction=Direction.DIAGNOSTIC,
                channel=Channel.STDERR,
                transport="stdio",
                raw_text="startup banner",
            ),
            event(1, {"jsonrpc": "2.0", "id": 7, "result": {}}),
        ],
    )
    left = normalize_trace(quiet)
    right = normalize_trace(noisy)
    assert [e.message for e in left.events] == [e.message for e in right.events]
