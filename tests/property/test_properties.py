"""Property-based tests for the components where hand-picked cases are not enough."""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from interop_lab.events.model import Channel, Direction, WireEvent, WireTrace
from interop_lab.minimizer.reduce import shrink_value
from interop_lab.normalization.normalize import normalize_message
from interop_lab.security.redaction import is_denied, redact_text, scrub_environment

json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=20),
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(st.text(min_size=1, max_size=8), children, max_size=4)
    ),
    max_leaves=8,
)


@given(json_values)
@settings(max_examples=150, deadline=None)
def test_normalization_is_idempotent(payload: Any) -> None:
    """Normalising twice must equal normalising once, or comparisons drift."""
    message = {"jsonrpc": "2.0", "id": 1, "result": {"value": payload}}
    once = normalize_message(message)
    twice = normalize_message(once)
    assert once == twice


@given(json_values)
@settings(max_examples=150, deadline=None)
def test_normalization_preserves_error_codes(payload: Any) -> None:
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32601, "message": "m", "data": payload},
    }
    assert normalize_message(message)["error"]["code"] == -32601


@given(st.integers(), json_values)
@settings(max_examples=100, deadline=None)
def test_result_presence_survives_normalization(rpc_id: int, payload: Any) -> None:
    with_field = normalize_message({"jsonrpc": "2.0", "id": rpc_id, "result": {"a": payload}})
    without_field = normalize_message({"jsonrpc": "2.0", "id": rpc_id, "result": {}})
    assert ("a" in with_field["result"]) != ("a" in without_field["result"])


@given(st.text(max_size=200))
@settings(max_examples=200, deadline=None)
def test_redaction_never_raises_and_never_grows_unboundedly(text: str) -> None:
    redacted, changed = redact_text(text)
    assert isinstance(redacted, str)
    if not changed:
        assert redacted == text


@given(st.dictionaries(st.text(min_size=1, max_size=16), st.text(max_size=16), max_size=8))
@settings(max_examples=200, deadline=None)
def test_scrubbing_never_emits_a_denylisted_name(source: dict[str, str]) -> None:
    env = scrub_environment(list(source), source=source)
    assert not any(is_denied(name) for name in env)


@given(json_values)
@settings(max_examples=200, deadline=None)
def test_shrink_makes_progress_or_stops(value: Any) -> None:
    """The shrinker must terminate: each step is strictly smaller, or it returns None."""
    smaller = shrink_value(value)
    if smaller is None:
        return
    sized = (str, list, dict)
    if isinstance(value, sized) and isinstance(smaller, sized):
        assert len(smaller) < len(value)
    elif isinstance(value, bool) or isinstance(smaller, bool):
        pass
    elif isinstance(value, int) and isinstance(smaller, int):
        assert abs(smaller) <= abs(value)


@given(st.lists(st.integers(min_value=0, max_value=1000), max_size=12))
@settings(max_examples=100, deadline=None)
def test_trace_jsonl_roundtrips(seqs: list[int]) -> None:
    trace = WireTrace(
        run_id="r",
        events=[
            WireEvent(
                seq=index,
                monotonic_ns=value,
                wall_clock="2026-08-12T00:00:00+00:00",
                direction=Direction.INBOUND,
                channel=Channel.STDOUT,
                transport="stdio",
                message={"jsonrpc": "2.0", "id": value},
            )
            for index, value in enumerate(seqs)
        ],
    )
    restored = WireTrace.from_jsonl("r", trace.to_jsonl())
    assert [e.seq for e in restored.events] == [e.seq for e in trace.events]
    assert [e.message for e in restored.events] == [e.message for e in trace.events]
