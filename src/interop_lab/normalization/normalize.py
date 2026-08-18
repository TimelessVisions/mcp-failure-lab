"""Trace normalisation for differential comparison.

Only documented volatile fields are normalised. The raw trace is never mutated —
normalisation produces a separate view, so a reviewer can always see what was
actually on the wire.

The tests in ``tests/unit/test_normalization.py`` exist specifically to prove
that normalisation does not hide wrong error codes, wrong ids, missing required
members, malformed results, crashes, or recovery failures.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from interop_lab.events.model import Direction, WireEvent, WireTrace

#: Fields whose values are volatile by design and carry no semantic weight.
VOLATILE_RESULT_PATHS: tuple[str, ...] = (
    # Cache hints are advisory and implementation-specific.
    "ttlMs",
    # Server identity differs by construction between implementations.
    "_meta.io.modelcontextprotocol/serverInfo",
    "serverInfo",
)

#: Top-level members replaced with a placeholder rather than dropped, so that
#: *presence* remains comparable even when the value is volatile.
VOLATILE_PLACEHOLDER = "<normalized>"


def _normalize_id(value: Any) -> Any:
    """Request ids are allocated by the harness and differ run to run.

    Correlation is checked by a dedicated assertion, not by the differ, so the
    differ normalises ids to positional tokens.
    """
    if isinstance(value, int | str):
        return VOLATILE_PLACEHOLDER
    return value


def _strip_path(payload: dict[str, Any], path: str) -> None:
    parts = path.split(".")
    cursor: Any = payload
    for part in parts[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            return
        cursor = cursor[part]
    if isinstance(cursor, dict) and parts[-1] in cursor:
        cursor[parts[-1]] = VOLATILE_PLACEHOLDER


def normalize_message(message: dict[str, Any]) -> dict[str, Any]:
    """Return a semantically comparable copy of a JSON-RPC message."""
    normalized = copy.deepcopy(message)

    if "id" in normalized:
        normalized["id"] = _normalize_id(normalized["id"])

    result = normalized.get("result")
    if isinstance(result, dict):
        for path in VOLATILE_RESULT_PATHS:
            _strip_path(result, path)

    # Error *messages* are prose and differ between implementations; the code is
    # the semantic part and is deliberately left untouched.
    error = normalized.get("error")
    if isinstance(error, dict) and "message" in error:
        error["message"] = VOLATILE_PLACEHOLDER

    return normalized


#: Response/request header names whose values change every run but whose
#: presence is still evidence. Replaced, never dropped.
VOLATILE_HEADERS: frozenset[str] = frozenset(
    {
        "date",
        "server",
        ":authority",
        ":first_byte_ms",
        ":body_complete_ms",
        "content-length",
    }
)

#: Headers that are normative and must survive untouched. Listed explicitly so
#: a future addition to VOLATILE_HEADERS cannot silently blind the comparison.
NORMATIVE_HEADERS: frozenset[str] = frozenset(
    {
        "mcp-protocol-version",
        "mcp-method",
        "mcp-name",
        "content-type",
        ":status",
        ":method",
        ":path",
    }
)

_PORT_PATTERN = re.compile(r"(127\.0\.0\.1|localhost|\[::1\]):\d+")
_RUN_ID_PATTERN = re.compile(r"__[0-9a-f]{8}\b")
_PID_PATTERN = re.compile(r"\bpid[= ]\d+", re.IGNORECASE)
_DURATION_PATTERN = re.compile(r"\b\d+(?:\.\d+)?(ms|s)\b")
_PORT_URL_PATTERN = re.compile(r"(http://(?:127\.0\.0\.1|localhost|\[::1\])):\d+")


def normalize_text(text: str) -> str:
    """Strip volatile runtime detail from a free-text note.

    Ephemeral ports, durations, pids and run ids differ on every run. Leaving
    them in makes two identical runs look different, which defeats the purpose
    of a normalised trace.
    """
    text = _PORT_URL_PATTERN.sub(r"\1:<port>", text)
    text = _PORT_PATTERN.sub(r"\1:<port>", text)
    text = _RUN_ID_PATTERN.sub("__<runid>", text)
    text = _PID_PATTERN.sub("pid=<pid>", text)
    return _DURATION_PATTERN.sub("<duration>", text)


def normalize_headers(headers: dict[str, str]) -> dict[str, str]:
    """Replace volatile header values while keeping every header name visible."""
    normalized: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in NORMATIVE_HEADERS:
            normalized[name] = value
        elif lowered in VOLATILE_HEADERS:
            # Server is split: it identifies the implementation (semantic) but
            # its version suffix is runtime detail. Keep the product token only.
            if lowered == "server":
                normalized[name] = value.split("/")[0]
            else:
                normalized[name] = VOLATILE_PLACEHOLDER
        else:
            normalized[name] = normalize_text(value)
    return normalized


def normalize_event(event: WireEvent) -> WireEvent:
    normalized = event.model_copy(deep=True)
    if normalized.message is not None:
        normalized.message = normalize_message(normalized.message)
    if normalized.headers is not None:
        normalized.headers = normalize_headers(normalized.headers)
    if normalized.note is not None:
        normalized.note = normalize_text(normalized.note)
    # Timing and byte-level identity are volatile between runs by definition.
    normalized.monotonic_ns = 0
    normalized.wall_clock = VOLATILE_PLACEHOLDER
    normalized.raw_sha256 = None
    normalized.raw_text = None
    normalized.raw_len = None
    return normalized


def normalize_trace(trace: WireTrace) -> WireTrace:
    """Normalise protocol traffic, dropping diagnostics that are not comparable.

    stderr content is implementation-specific chatter and is excluded from the
    comparison; it remains in the raw trace as evidence.
    """
    events = [
        normalize_event(event)
        for event in trace.events
        if event.direction in (Direction.OUTBOUND, Direction.INBOUND)
    ]
    # Renumber so an extra diagnostic line on one side does not shift every
    # subsequent sequence number and manufacture a false difference.
    for index, event in enumerate(events):
        event.seq = index
    # The run id itself is volatile; a normalised trace must be comparable
    # across runs, so it carries a stable placeholder instead.
    return WireTrace(run_id="<normalized>", events=events)
