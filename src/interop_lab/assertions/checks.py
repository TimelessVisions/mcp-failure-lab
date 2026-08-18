"""Assertion evaluation against captured wire events.

Each assertion states what the dated specification requires, what was observed,
and which events prove it. Nothing here guesses: an assertion that cannot be
decided returns ``INCONCLUSIVE`` rather than passing.
"""

from __future__ import annotations

from typing import Any

from interop_lab.events.model import WireEvent
from interop_lab.models.core import AssertionResult, AssertionStatus, Classification
from interop_lab.scenarios.model import AssertionKind, AssertionSpec

_MISSING = object()

_TYPE_NAMES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def lookup(payload: Any, path: str) -> Any:
    """Resolve a dotted path, returning the ``_MISSING`` sentinel when absent."""
    cursor = payload
    for part in path.split("."):
        if not part:
            continue
        if isinstance(cursor, dict) and part in cursor:
            cursor = cursor[part]
        elif isinstance(cursor, list) and part.isdigit() and int(part) < len(cursor):
            cursor = cursor[int(part)]
        else:
            return _MISSING
    return cursor


def _classification(spec: AssertionSpec) -> Classification:
    try:
        return Classification(spec.on_fail)
    except ValueError:  # pragma: no cover - guarded by config validation
        return Classification.SPEC_VIOLATION


def _result(
    spec: AssertionSpec,
    status: AssertionStatus,
    expected: str,
    actual: str,
    event: WireEvent | None,
    detail: str | None = None,
) -> AssertionResult:
    return AssertionResult(
        assertion_id=spec.assertion_id,
        status=status,
        expected=expected,
        actual=actual,
        evidence_seq=[event.seq] if event is not None else [],
        normative_source=spec.normative_source,
        classification=_classification(spec) if status is AssertionStatus.FAIL else None,
        detail=detail,
        # Carried from the spec regardless of outcome. The result records what
        # the assertion *is* about; whether that mattered on this run is decided
        # by the classification travelling beside it.
        security_relevant=spec.security_relevant,
        security_category=spec.security_category,
    )


def evaluate(
    spec: AssertionSpec,
    *,
    event: WireEvent | None,
    request_id: str | int | None,
    timed_out: bool,
    timeout_expected: bool = False,
) -> AssertionResult:
    """Evaluate one assertion against the response event for a step.

    ``timeout_expected`` distinguishes two genuinely different situations that an
    earlier version of this harness conflated:

    * The step was designed to tolerate silence, so "no response arrived" is a
      *decided observation*. An assertion that expected a response has failed.
    * The step timed out unexpectedly, so the run was aborted mid-flight and we
      genuinely cannot tell what the target would have said. That is
      ``INCONCLUSIVE``.

    Reporting the first case as inconclusive hid a real behavioural divergence.
    """
    if spec.kind is AssertionKind.NO_RESPONSE:
        if event is None:
            return _result(spec, AssertionStatus.PASS, "no response", "no response", None)
        return _result(
            spec,
            AssertionStatus.FAIL,
            "no response",
            f"response received: {event.message!r}",
            event,
        )

    if spec.kind is AssertionKind.NO_JSONRPC_RESPONSE:
        # Transport-level acknowledgement is not a JSON-RPC response. Over HTTP
        # something always comes back — an empty-bodied 202 is the conforming
        # answer to a notification — so the question is whether a *response
        # object* was correlated, not whether bytes arrived.
        if event is None:
            return _result(
                spec, AssertionStatus.PASS, "no JSON-RPC response", "nothing arrived", None
            )
        message = event.message
        if not isinstance(message, dict) or not ("result" in message or "error" in message):
            observed = (
                f"HTTP {event.http_status} with no JSON-RPC response object"
                if event.http_status is not None
                else "no JSON-RPC response object"
            )
            return _result(spec, AssertionStatus.PASS, "no JSON-RPC response", observed, event)
        return _result(
            spec,
            AssertionStatus.FAIL,
            "no JSON-RPC response",
            f"a response object was correlated to a notification: {message!r}",
            event,
        )

    if spec.kind is AssertionKind.HTTP_STATUS_IN:
        if event is None or event.http_status is None:
            return _result(
                spec,
                AssertionStatus.INCONCLUSIVE,
                f"HTTP status in {spec.expected_statuses}",
                "no HTTP status was recorded for this step",
                event,
                detail=(
                    "the step produced no HTTP response, so the transport-level "
                    "requirement cannot be decided either way"
                ),
            )
        if event.http_status in spec.expected_statuses:
            return _result(
                spec,
                AssertionStatus.PASS,
                f"HTTP status in {spec.expected_statuses}",
                str(event.http_status),
                event,
            )
        return _result(
            spec,
            AssertionStatus.FAIL,
            f"HTTP status in {spec.expected_statuses}",
            str(event.http_status),
            event,
        )

    if timed_out or event is None:
        if timeout_expected:
            return _result(
                spec,
                AssertionStatus.FAIL,
                spec.description or spec.kind.value,
                "no response was sent at all within the deadline",
                None,
                detail=(
                    "the step tolerated silence, so absence of a response is an "
                    "observation rather than an unknown"
                ),
            )
        return _result(
            spec,
            AssertionStatus.INCONCLUSIVE,
            spec.description or spec.kind.value,
            "no response was received before the deadline",
            None,
            detail="assertion undecidable without a response",
        )

    if spec.kind is AssertionKind.VALID_JSON:
        if event.message is not None:
            return _result(
                spec, AssertionStatus.PASS, "a single valid JSON object", "valid JSON object", event
            )
        return _result(
            spec,
            AssertionStatus.FAIL,
            "a single valid JSON object",
            event.parse_error or "unparseable output",
            event,
        )

    if event.message is None:
        return _result(
            spec,
            AssertionStatus.FAIL,
            spec.description or spec.kind.value,
            f"output was not a JSON object: {event.parse_error}",
            event,
        )

    message = event.message

    match spec.kind:
        case AssertionKind.RESPONSE_IS_RESULT:
            if "result" in message and "error" not in message:
                return _result(spec, AssertionStatus.PASS, "a result response", "result", event)
            if "result" in message and "error" in message:
                return _result(
                    spec,
                    AssertionStatus.FAIL,
                    "exactly one of result or error",
                    "both result and error were present",
                    event,
                )
            return _result(
                spec,
                AssertionStatus.FAIL,
                "a result response",
                f"error response: {message.get('error')!r}",
                event,
            )

        case AssertionKind.RESPONSE_IS_ERROR:
            if "error" in message:
                return _result(spec, AssertionStatus.PASS, "an error response", "error", event)
            if "result" in message:
                return _result(
                    spec, AssertionStatus.FAIL, "an error response", "result response", event
                )
            # Neither. Reporting this as "result response" was wrong and
            # actively misleading: a server that answers an invalid request with
            # an unsolicited log notification has not returned a result, and an
            # artifact saying it did sends a reader looking for a result that is
            # not in the trace. What was received is named instead.
            method = message.get("method")
            observed = (
                f"a {method!r} message carrying neither result nor error"
                if isinstance(method, str)
                else "a message carrying neither result nor error"
            )
            return _result(
                spec,
                AssertionStatus.FAIL,
                "an error response",
                observed,
                event,
                detail=(
                    "no JSON-RPC response object was correlated to this request; the "
                    "message recorded here is what arrived on the channel instead"
                ),
            )

        case AssertionKind.ERROR_CODE_EQUALS:
            code = event.error_code()
            if code == spec.expected:
                return _result(
                    spec, AssertionStatus.PASS, f"error code {spec.expected}", str(code), event
                )
            return _result(
                spec,
                AssertionStatus.FAIL,
                f"error code {spec.expected}",
                f"error code {code!r}",
                event,
            )

        case AssertionKind.ERROR_CODE_IN:
            code = event.error_code()
            if code in spec.expected_codes:
                return _result(
                    spec,
                    AssertionStatus.PASS,
                    f"error code in {spec.expected_codes}",
                    str(code),
                    event,
                )
            return _result(
                spec,
                AssertionStatus.FAIL,
                f"error code in {spec.expected_codes}",
                f"error code {code!r}",
                event,
            )

        case AssertionKind.RESPONSE_ID_MATCHES:
            if event.rpc_id == request_id:
                return _result(
                    spec, AssertionStatus.PASS, f"id == {request_id!r}", repr(event.rpc_id), event
                )
            return _result(
                spec,
                AssertionStatus.FAIL,
                f"id == {request_id!r}",
                f"id == {event.rpc_id!r}",
                event,
                detail="a response carrying the wrong id cannot be correlated by a client",
            )

        case AssertionKind.RESULT_HAS_FIELD:
            assert spec.path is not None
            value = lookup(message.get("result"), spec.path)
            if value is not _MISSING:
                return _result(
                    spec, AssertionStatus.PASS, f"result.{spec.path} present", "present", event
                )
            return _result(
                spec, AssertionStatus.FAIL, f"result.{spec.path} present", "absent", event
            )

        case AssertionKind.RESULT_FIELD_TYPE:
            assert spec.path is not None and spec.expected_type is not None
            value = lookup(message.get("result"), spec.path)
            if value is _MISSING:
                return _result(
                    spec,
                    AssertionStatus.FAIL,
                    f"result.{spec.path} of type {spec.expected_type}",
                    "field absent",
                    event,
                )
            expected_type = _TYPE_NAMES.get(spec.expected_type)
            if expected_type is None:
                return _result(
                    spec,
                    AssertionStatus.INCONCLUSIVE,
                    spec.expected_type,
                    "unknown type name in assertion",
                    event,
                )
            # bool is a subclass of int; do not let True satisfy "integer".
            ok = isinstance(value, expected_type) and not (
                spec.expected_type in ("integer", "number") and isinstance(value, bool)
            )
            if ok:
                return _result(
                    spec,
                    AssertionStatus.PASS,
                    f"result.{spec.path} of type {spec.expected_type}",
                    type(value).__name__,
                    event,
                )
            return _result(
                spec,
                AssertionStatus.FAIL,
                f"result.{spec.path} of type {spec.expected_type}",
                f"{type(value).__name__}",
                event,
            )

        case AssertionKind.RESULT_FIELD_EQUALS:
            assert spec.path is not None
            value = lookup(message.get("result"), spec.path)
            if value == spec.expected:
                return _result(
                    spec,
                    AssertionStatus.PASS,
                    f"result.{spec.path} == {spec.expected!r}",
                    repr(value),
                    event,
                )
            return _result(
                spec,
                AssertionStatus.FAIL,
                f"result.{spec.path} == {spec.expected!r}",
                "field absent" if value is _MISSING else repr(value),
                event,
            )

        case _:  # pragma: no cover - exhaustive above
            return _result(
                spec, AssertionStatus.INCONCLUSIVE, spec.kind.value, "unhandled assertion", event
            )
