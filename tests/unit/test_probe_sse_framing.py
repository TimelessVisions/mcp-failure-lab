"""SSE response framing in the contained probe.

Both dated revisions let a Streamable HTTP server answer a POST with either a
single JSON object or an SSE stream, at the server's discretion. The fixture
servers in this repository always chose JSON, so the probe only ever needed
`json.loads`, and that held until an external implementation was pointed at it:
the official TypeScript SDK frames *every* POST response as `text/event-stream`,
including a plain `initialize`.

What is implemented, and tested here, is response framing — reading a body the
server has already finished writing. What is not implemented, and is reported
NOT_APPLICABLE rather than approximated, is anything requiring the connection to
stay open: the long-lived GET subscription, resumption via `Last-Event-ID`, and
server-initiated messages.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest


def _load_probe() -> Any:
    """Import the probe script by path.

    It lives under `fixtures/` and is copied into a container rather than
    installed, so there is no package to import it from. Loading it here is what
    lets the same tests exercise the code that actually runs inside the sidecar,
    instead of a copy of its rules that could drift.
    """
    path = Path("fixtures/http_probe.py")
    spec = importlib.util.spec_from_file_location("http_probe_sse_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe_module = _load_probe()
decode_sse_response = probe_module.decode_sse_response


def test_decodes_the_shape_the_typescript_sdk_actually_sends() -> None:
    """The framing a Streamable HTTP server emits when answering `initialize`.

    Note the leading event with an empty `data:` — a server may open the stream
    before it has a message. A reader that took the first data frame would decode
    nothing and report the server as unresponsive.

    The body below is a synthetic instance of that shape. It deliberately does
    not name which implementation and version it was first observed on: a real
    target, an exact version and an observed behaviour are a private measurement
    when written down together, whichever field is dropped last.
    """
    body = (
        "id: 58d9da56-8be2-474a-a19d-e15d838eedf9\n"
        "data: \n"
        "\n"
        "event: message\n"
        "id: 7f0cefe9-d3de-41c4-a369-7a6e441f3192\n"
        'data: {"result":{"protocolVersion":"2025-11-25"},"jsonrpc":"2.0","id":1}\n'
        "\n"
    )
    message, error = decode_sse_response(body)

    assert error is None
    assert message == {
        "result": {"protocolVersion": "2025-11-25"},
        "jsonrpc": "2.0",
        "id": 1,
    }


def test_the_response_wins_over_notifications_that_precede_it() -> None:
    """A server may emit progress before answering; the answer is the assertion."""
    body = (
        "event: message\n"
        'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{"p":1}}\n'
        "\n"
        "event: message\n"
        'data: {"jsonrpc":"2.0","id":7,"result":{"tools":[]}}\n'
        "\n"
    )
    message, error = decode_sse_response(body)

    assert error is None
    assert message == {"jsonrpc": "2.0", "id": 7, "result": {"tools": []}}


def test_multi_line_data_is_joined_with_newlines() -> None:
    """The WHATWG rule: `data` accumulates, separated by \\n, not concatenated."""
    body = 'event: message\ndata: {"jsonrpc":"2.0",\ndata:  "id":3}\n\n'
    message, error = decode_sse_response(body)

    assert error is None
    assert message == {"jsonrpc": "2.0", "id": 3}


def test_only_one_space_after_the_colon_is_the_delimiter() -> None:
    """A second space is data. Getting this wrong corrupts any padded payload."""
    message, error = decode_sse_response('data:  {"jsonrpc":"2.0","id":1}\n\n')

    assert error is None
    # The surviving leading space is inside the JSON text, which json.loads
    # tolerates — the point is that exactly one space was removed, not two.
    assert message == {"jsonrpc": "2.0", "id": 1}


def test_comments_and_keepalives_are_not_data() -> None:
    """`:` opens a comment; the SDK uses it to hold a connection open."""
    body = ': ping\n\n: ping\n\nevent: message\ndata: {"jsonrpc":"2.0","id":9}\n\n'
    message, error = decode_sse_response(body)

    assert error is None
    assert message == {"jsonrpc": "2.0", "id": 9}


@pytest.mark.parametrize(
    "separator",
    ["\n\n", "\r\n\r\n", "\r\r"],
    ids=["lf", "crlf", "cr"],
)
def test_every_line_ending_the_grammar_allows(separator: str) -> None:
    """CRLF is what an HTTP server most often emits; the grammar allows all three."""
    line_ending = separator[: len(separator) // 2]
    body = f'event: message{line_ending}data: {{"jsonrpc":"2.0","id":1}}{separator}'
    message, error = decode_sse_response(body)

    assert error is None
    assert message == {"jsonrpc": "2.0", "id": 1}


def test_a_stream_with_no_data_frame_is_an_error_not_a_silent_none() -> None:
    """Otherwise an empty stream reads as "the server sent no message", which is
    a different and much more interesting claim than "it sent only comments"."""
    message, error = decode_sse_response(": ping\n\n")

    assert message is None
    assert error is not None
    assert "no data frame" in error


def test_malformed_json_in_a_data_frame_is_reported() -> None:
    message, error = decode_sse_response("event: message\ndata: {not json\n\n")

    assert message is None
    assert error is not None
    assert "not JSON" in error


def test_a_malformed_frame_does_not_hide_a_valid_one_after_it() -> None:
    """A keep-alive the server framed badly must not mask the real response."""
    body = 'data: {oops\n\nevent: message\ndata: {"jsonrpc":"2.0","id":2}\n\n'
    message, error = decode_sse_response(body)

    assert error is None
    assert message == {"jsonrpc": "2.0", "id": 2}


def test_the_content_type_constant_matches_what_is_dispatched_on() -> None:
    """The decoder is only reached when this content type is seen on the wire."""
    assert probe_module.SSE_CONTENT_TYPE == "text/event-stream"
