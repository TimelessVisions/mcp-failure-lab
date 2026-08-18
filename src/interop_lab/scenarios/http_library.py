"""Streamable HTTP scenarios.

Deliberately small. Four scenarios that produce trustworthy artifacts are worth
more than twenty that do not, and the official conformance suite already covers
the happy path — these exist to add what it does not: header/body disagreement,
timing attribution, and connection-level recovery.
"""

from __future__ import annotations

from interop_lab.injection.faults import Boundary, Fault, FaultKind, FaultTrigger
from interop_lab.models.core import (
    Applicability,
    ScenarioCategory,
    ScenarioDefinition,
    ScenarioNature,
    SecurityCategory,
    Severity,
)
from interop_lab.profiles.registry import Role, Transport
from interop_lab.scenarios.model import AssertionKind, AssertionSpec, Scenario, Step, StepKind

SCHEMA_2026 = "MCP 2026-07-28 schema.json"
HTTP_ONLY = [Transport.STREAMABLE_HTTP]
SERVERS = [Role.SERVER, Role.FIXTURE]

PROFILE_2026 = "mcp-2026-07-28"
PROFILE_2025 = "mcp-2025-11-25"

#: The dated transport binding these scenarios are read from. Cited rather than
#: the schema, because the schema describes messages and none of the
#: requirements below are about a message: they are about HTTP methods, status
#: codes, headers and session lifetime.
TRANSPORT_2025 = "MCP 2025-11-25 Streamable HTTP transport binding"
TRANSPORT_2026 = "MCP 2026-07-28 Streamable HTTP transport binding"
#: Cited only where *both* dated bindings state the requirement independently.
#: Verified clause by clause against the retrieved documents — see
#: research/protocol-notes.md. Naming both when only one says it would assert a
#: document that does not govern the target being measured.
TRANSPORT_BOTH = "MCP 2025-11-25 and 2026-07-28 Streamable HTTP transport bindings"


def _http_2025(*, transport_features: dict[str, object] | None = None) -> Applicability:
    """Scenarios about the 2025 stateful HTTP lifecycle.

    Restricted to 2025-11-25 deliberately. 2026-07-28 removed `initialize`
    entirely, so "what does the server do before initialization" is not a
    question that revision can be asked — and asserting it there would be
    asserting a requirement outside its dated profile, which is the one error
    this project treats as unforgivable.
    """
    return Applicability(
        profiles=[PROFILE_2025],
        max_spec_version="2025-11-25",
        transports=HTTP_ONLY,
        roles=SERVERS,
        requires_transport_features=dict(transport_features or {}),
    )


def _http_any(tools: list[str] | None = None) -> Applicability:
    return Applicability(
        profiles=[PROFILE_2025, PROFILE_2026],
        transports=HTTP_ONLY,
        roles=SERVERS,
        requires_tools=tools or [],
    )


def _http_2026(tools: list[str] | None = None) -> Applicability:
    return Applicability(
        profiles=[PROFILE_2026],
        min_spec_version="2026-07-28",
        transports=HTTP_ONLY,
        roles=SERVERS,
        requires_tools=tools or [],
    )


def http_baseline() -> Scenario:
    """A valid request over HTTP, appropriate to whichever profile applies."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="baseline.http_tools_list",
            title="tools/list over Streamable HTTP returns a well-formed result",
            category=ScenarioCategory.BASELINE,
            nature=ScenarioNature.NORMATIVE,
            purpose=(
                "Establish the HTTP path works before anything hostile runs. The "
                "official conformance suite covers this shape too; it is kept here as "
                "the harness's own positive control, not as a competing check."
            ),
            applicability=_http_any(),
            severity=Severity.HIGH,
            expected_outcome="HTTP 200 with a JSON-RPC result whose `tools` member is an array.",
            normative_source="ListToolsResult (present in both profiles)",
        ),
        steps=[
            Step(
                step_id="list",
                kind=StepKind.REQUEST,
                method="tools/list",
                optional=False,
                assertions=[
                    AssertionSpec(
                        assertion_id="is_result",
                        kind=AssertionKind.RESPONSE_IS_RESULT,
                        description="tools/list returns a result over HTTP",
                    ),
                    AssertionSpec(
                        assertion_id="id_matches",
                        kind=AssertionKind.RESPONSE_ID_MATCHES,
                        description="response id equals request id",
                        normative_source="JSON-RPC 2.0 §5",
                    ),
                    AssertionSpec(
                        assertion_id="tools_is_array",
                        kind=AssertionKind.RESULT_FIELD_TYPE,
                        path="tools",
                        expected_type="array",
                        description="result.tools is an array",
                    ),
                ],
            )
        ],
    )


def http_header_body_mismatch() -> Scenario:
    """A routing header that disagrees with the body must be rejected.

    This is the scenario the 2026-07-28 revision made possible: gateways route
    and authorize on `Mcp-Method` without parsing the body, so a server that
    accepts a disagreeing header lets a request be authorized as one method and
    executed as another.
    """
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="version.http_header_body_mismatch",
            title="Mcp-Method header disagreeing with the body is rejected",
            category=ScenarioCategory.VERSION_HEADER,
            nature=ScenarioNature.SECURITY_RELEVANT,
            purpose=(
                "2026-07-28 lets intermediaries route and authorize on headers alone. "
                "If a server executes the body method while an intermediary saw the "
                "header method, authorization and execution disagree."
            ),
            applicability=_http_2026(tools=["echo"]),
            severity=Severity.HIGH,
            security_category=SecurityCategory.HEADER_BODY_AUTHORIZATION_SPLIT,
            security_limitations=[
                "Asserted against local fixtures only. No third-party implementation "
                "is named or evaluated by this scenario.",
                "That a server accepts a disagreeing header is established here. That "
                "an intermediary in front of it would authorize on the header is an "
                "assumption about a deployment this harness does not measure.",
            ],
            expected_outcome=(
                "HTTP 400 with JSON-RPC error code -32020, preserving the request id."
            ),
            normative_source=(
                f"{SCHEMA_2026}: HeaderMismatchError, code = -32020 — 'Returned when a "
                "server rejects a request because the values in the HTTP headers do not "
                "match the corresponding values in the request body... For HTTP, the "
                "response status code MUST be 400 Bad Request.'"
            ),
            rationale=(
                "Detection is asserted against local fixtures only. No third-party "
                "implementation is named or evaluated by this scenario."
            ),
        ),
        steps=[
            Step(
                step_id="mismatch",
                kind=StepKind.REQUEST,
                method="tools/list",
                optional=False,
                # The body says tools/list; the header claims tools/call.
                http_header_overrides={"Mcp-Method": "tools/call"},
                assertions=[
                    AssertionSpec(
                        assertion_id="is_error",
                        kind=AssertionKind.RESPONSE_IS_ERROR,
                        description="a header/body disagreement is rejected",
                        normative_source=f"{SCHEMA_2026}: HeaderMismatchError",
                    ),
                    AssertionSpec(
                        assertion_id="code_32020",
                        kind=AssertionKind.ERROR_CODE_EQUALS,
                        expected=-32020,
                        description="error code is -32020",
                        normative_source=f"{SCHEMA_2026}: HeaderMismatchError.code",
                    ),
                    AssertionSpec(
                        assertion_id="id_preserved",
                        kind=AssertionKind.RESPONSE_ID_MATCHES,
                        description="the rejection preserves the request id",
                        normative_source="JSON-RPC 2.0 §5",
                    ),
                ],
            )
        ],
    )


def http_header_body_mismatch_noisy() -> Scenario:
    """The same defect, wrapped in material that genuinely can be reduced.

    ``version.http_header_body_mismatch`` is already minimal: one step, one
    constitutive header, a two-byte body. That makes it a poor demonstration of
    reduction, and manufacturing one would be dishonest. This variant carries
    deliberate, safe noise — unrelated preceding steps, unused body fields, and
    a header explicitly nominated as reducible — so the minimiser has something
    real to remove while the constitutive mismatch must survive.
    """
    padding = {
        "unused_note": "this field has no effect on the outcome and should be removed",
        "unused_count": 4096,
        "unused_list": [1, 2, 3, 4, 5, 6, 7, 8],
        "nested": {"deep": {"deeper": "also irrelevant"}},
    }
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="version.http_header_body_mismatch_noisy",
            title="Header/body mismatch surrounded by reducible noise",
            category=ScenarioCategory.VERSION_HEADER,
            nature=ScenarioNature.SECURITY_RELEVANT,
            purpose=(
                "Demonstrate that automatic reduction works over HTTP when reducible "
                "material exists, without overstating the already-minimal variant."
            ),
            applicability=_http_2026(tools=["echo"]),
            severity=Severity.HIGH,
            security_category=SecurityCategory.HEADER_BODY_AUTHORIZATION_SPLIT,
            security_limitations=[
                "The same defect as the minimal variant, wrapped in reducible noise. "
                "It exists to exercise minimisation and establishes nothing further "
                "about any implementation.",
            ],
            expected_outcome=(
                "HTTP 400 with JSON-RPC error code -32020. The minimiser should strip "
                "the preceding steps, the unused body fields and the reducible header, "
                "while preserving the Mcp-Method mismatch that causes the failure."
            ),
            normative_source=(
                f"{SCHEMA_2026}: HeaderMismatchError, code = -32020 — 'For HTTP, the "
                "response status code MUST be 400 Bad Request.'"
            ),
        ),
        steps=[
            # Optional warm-up traffic the minimiser is free to delete.
            Step(
                step_id="warmup_list",
                kind=StepKind.REQUEST,
                method="tools/list",
                optional=True,
                params=dict(padding),
            ),
            Step(
                step_id="warmup_call",
                kind=StepKind.REQUEST,
                method="tools/call",
                params={"name": "echo", "arguments": {"text": "warm"}, **padding},
                optional=True,
            ),
            Step(
                step_id="mismatch",
                kind=StepKind.REQUEST,
                method="tools/list",
                optional=False,
                params=dict(padding),
                http_header_overrides={
                    # Constitutive: this is the condition under test.
                    "Mcp-Method": "tools/call",
                    # Decorative: nominated below as safe to remove.
                    "X-Interop-Lab-Note": "irrelevant-to-the-failure",
                },
                reducible_headers=["X-Interop-Lab-Note"],
                assertions=[
                    AssertionSpec(
                        assertion_id="is_error",
                        kind=AssertionKind.RESPONSE_IS_ERROR,
                        description="a header/body disagreement is rejected",
                        normative_source=f"{SCHEMA_2026}: HeaderMismatchError",
                    ),
                    AssertionSpec(
                        assertion_id="code_32020",
                        kind=AssertionKind.ERROR_CODE_EQUALS,
                        expected=-32020,
                        description="error code is -32020",
                        normative_source=f"{SCHEMA_2026}: HeaderMismatchError.code",
                    ),
                ],
            ),
        ],
    )


def http_slow_body() -> Scenario:
    """Headers arrive promptly but the body stalls."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="timing.http_slow_body",
            title="Response body does not complete within the deadline",
            category=ScenarioCategory.TIMING_CANCELLATION,
            nature=ScenarioNature.RESILIENCE,
            purpose=(
                "A stalled body is invisible to any check that only waits for a status "
                "line. This proves the harness separates header timing from body "
                "completion rather than reporting one undifferentiated timeout."
            ),
            applicability=_http_2026(tools=["echo"]),
            severity=Severity.MEDIUM,
            expected_outcome=(
                "Against a prompt target, a complete result. Against a stalling target, "
                "a body-completion timeout distinct from a connect or header timeout."
            ),
        ),
        steps=[
            Step(
                step_id="slow",
                kind=StepKind.REQUEST,
                method="tools/call",
                params={"name": "echo", "arguments": {"text": "slow"}},
                optional=False,
                timeout_s=2.5,
                assertions=[
                    AssertionSpec(
                        assertion_id="body_completes",
                        kind=AssertionKind.RESPONSE_IS_RESULT,
                        description="the response body completes within the deadline",
                        on_fail="TIMEOUT",
                    )
                ],
            )
        ],
    )


def http_connection_reset() -> Scenario:
    """The connection is reset before any response."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="recovery.http_connection_reset",
            title="Connection reset is detected and the next request is attempted",
            category=ScenarioCategory.PROCESS_RECOVERY,
            nature=ScenarioNature.RESILIENCE,
            purpose=(
                "Record partial response state, connection failure, and whether the "
                "next request succeeds — the difference between a transient reset and "
                "a wedged server."
            ),
            applicability=_http_2026(tools=["echo"]),
            severity=Severity.MEDIUM,
            expected_outcome=(
                "Against a conforming target, both requests succeed. Against a "
                "resetting target, the reset is recorded and the recovery attempt is "
                "reported honestly rather than as a hang."
            ),
        ),
        steps=[
            Step(
                step_id="reset",
                kind=StepKind.REQUEST,
                method="tools/list",
                optional=False,
                timeout_s=3.0,
                timeout_is_expected=True,
                assertions=[
                    AssertionSpec(
                        assertion_id="reset_observed",
                        kind=AssertionKind.RESPONSE_IS_RESULT,
                        description="the server answers rather than resetting",
                        on_fail="RECOVERY_FAILURE",
                    )
                ],
            ),
            Step(
                step_id="after_reset",
                kind=StepKind.REQUEST,
                method="tools/list",
                optional=False,
                timeout_s=3.0,
                timeout_is_expected=True,
                assertions=[
                    AssertionSpec(
                        assertion_id="recovers",
                        kind=AssertionKind.RESPONSE_IS_RESULT,
                        description="the server answers the next request after a reset",
                        on_fail="RECOVERY_FAILURE",
                    )
                ],
            ),
        ],
    )


def http_malformed_body() -> Scenario:
    """Truncated JSON in an HTTP body must produce a parse error, not a crash.

    The stdio library already covers malformed input, but stdio-only: its
    applicability is restricted to that transport, and the two are not the same
    test. A framed stdio transport can defensibly discard input it cannot parse;
    an HTTP request has an unambiguous response slot, and a server that answers
    a 200 with a result to a body it never parsed is a different defect entirely.

    Present here so the contained path can prove that byte-exact malformed
    payloads reach the target unchanged — the property that the compiled-plan
    design most easily breaks, because a JSON round trip anywhere between the
    scenario and the socket would silently repair the truncation.
    """
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="wire.http_malformed_body",
            title="Truncated JSON body produces a parse error and the server survives",
            category=ScenarioCategory.WIRE_INTEGRITY,
            nature=ScenarioNature.RESILIENCE,
            purpose=(
                "A hostile or buggy client must not be able to wedge a server with an "
                "unparseable body. Over HTTP the response slot is unambiguous, so the "
                "parse error is a normative expectation rather than a divergence."
            ),
            applicability=_http_any(),
            severity=Severity.HIGH,
            expected_outcome=(
                "HTTP 400 with JSON-RPC error code -32700, and the next well-formed "
                "request answered normally."
            ),
            normative_source="JSON-RPC 2.0 §5.1 Parse error (-32700)",
        ),
        steps=[
            Step(
                step_id="malformed",
                kind=StepKind.RAW,
                # Truncated mid-object: valid UTF-8, invalid JSON. It must arrive
                # at the target exactly as written, which is why the compiled
                # plan carries it as an exact text body rather than as an object.
                raw_payload='{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{',
                optional=False,
                timeout_s=5.0,
                assertions=[
                    AssertionSpec(
                        assertion_id="parse_error_returned",
                        kind=AssertionKind.ERROR_CODE_EQUALS,
                        expected=-32700,
                        description="a malformed body is answered with -32700",
                        normative_source="JSON-RPC 2.0 §5.1",
                    )
                ],
            ),
            # The property that actually matters: the server is not wedged.
            Step(
                step_id="recover",
                kind=StepKind.REQUEST,
                method="tools/list",
                optional=False,
                assertions=[
                    AssertionSpec(
                        assertion_id="recovers",
                        kind=AssertionKind.RESPONSE_IS_RESULT,
                        description="the server answers normally after malformed input",
                        on_fail="RECOVERY_FAILURE",
                    )
                ],
            ),
        ],
    )


def http_dropped_request() -> Scenario:
    """Negative control: a harness-dropped request is not a target defect."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="timing.http_dropped_request",
            title="Harness-dropped HTTP request is attributed to the injected fault",
            category=ScenarioCategory.TIMING_CANCELLATION,
            nature=ScenarioNature.RESILIENCE,
            purpose="Prove HTTP timeout attribution does not blame a blameless target.",
            applicability=_http_2026(),
            severity=Severity.INFO,
            expected_outcome="No response, attributed to the injected drop.",
        ),
        steps=[
            Step(
                step_id="dropped",
                kind=StepKind.REQUEST,
                method="tools/list",
                optional=False,
                timeout_s=2.0,
                timeout_is_expected=True,
                assertions=[
                    AssertionSpec(
                        assertion_id="no_response",
                        kind=AssertionKind.NO_RESPONSE,
                        description="a dropped request receives no response",
                    )
                ],
            )
        ],
        faults=[
            Fault(
                fault_id="drop_first_http_request",
                boundary=Boundary.OUTBOUND_MESSAGE,
                kind=FaultKind.DROP,
                trigger=FaultTrigger(occurrence=0),
            )
        ],
    )


# ------------------------------------------------- Streamable HTTP semantics
#
# These ask what a server does with requests that are not message-carrying POSTs:
# a GET, a bad Origin, an unsupported protocol version header, an incomplete
# Accept. None of them is a JSON-RPC question, so each cites the dated transport
# binding rather than a schema — and where the binding places its requirement on
# the *client*, the scenario says so and refuses to convert server behaviour into
# a server defect.


def http_notification_accepted() -> Scenario:
    """A notification POSTed over HTTP is accepted at the transport level."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="transport.http_notification_accepted",
            title="A POSTed notification is accepted with a bodyless success status",
            category=ScenarioCategory.WIRE_INTEGRITY,
            nature=ScenarioNature.NORMATIVE,
            purpose=(
                "The transport half of the notification rule. JSON-RPC says the server "
                "must not reply; the HTTP binding says what it does instead, and a "
                "client that treats a 202 as a failed request will retry a message the "
                "server already accepted."
            ),
            applicability=_http_any(),
            severity=Severity.MEDIUM,
            expected_outcome=(
                "HTTP 202 Accepted with no body. The binding names 202 for a POST "
                "carrying only notifications or responses; a 200 with an empty body is "
                "recorded as a divergence rather than a violation, because it is the "
                "same observable outcome for a client that reads the body."
            ),
            normative_source=(
                f"{TRANSPORT_BOTH}: 'If the input consists solely of (any number of) "
                "JSON-RPC responses or notifications: If the server accepts the input, "
                "the server MUST return HTTP status code 202 Accepted with no body.'"
            ),
            rationale=(
                "Official conformance coverage: the conformance suite exercises "
                "notification delivery against a server it controls. Failure Lab "
                "addition: the status code asserted directly, against third-party "
                "implementations under containment, with the JSON-RPC and transport "
                "requirements graded separately."
            ),
        ),
        steps=[
            Step(
                step_id="notify",
                kind=StepKind.NOTIFICATION,
                method="notifications/cancelled",
                params={"requestId": 999_999},
                optional=False,
                timeout_s=5.0,
                timeout_is_expected=True,
                assertions=[
                    AssertionSpec(
                        assertion_id="accepted_status",
                        kind=AssertionKind.HTTP_STATUS_IN,
                        expected_statuses=[202],
                        description="the POST is answered with 202 Accepted",
                        normative_source=f"{TRANSPORT_BOTH}: 202 Accepted with no body",
                        # Divergence, not violation: 200-with-no-body is
                        # indistinguishable to a client that reads the body, and
                        # this scenario exists to measure the difference before
                        # anyone calls it a defect.
                        on_fail="BEHAVIORAL_DIVERGENCE",
                    ),
                    AssertionSpec(
                        assertion_id="no_response_object",
                        kind=AssertionKind.NO_JSONRPC_RESPONSE,
                        description="no JSON-RPC response object is returned",
                        normative_source="JSON-RPC 2.0 §4.1: the Server MUST NOT reply",
                    ),
                ],
            )
        ],
    )


def http_invalid_origin() -> Scenario:
    """A clearly invalid, synthetic Origin header.

    No DNS lookup happens and no external host is contacted: the value is a
    literal that resolves to nothing, and it is sent to the same local endpoint
    every other scenario uses.
    """
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="security.http_invalid_origin",
            title="A request carrying an unrecognised Origin is rejected",
            category=ScenarioCategory.HTTP_AUTHZ,
            nature=ScenarioNature.SECURITY_RELEVANT,
            purpose=(
                "A locally bound MCP server is reachable from any page the user's "
                "browser happens to load. Origin validation is what stops a website "
                "from driving a server running on the same machine."
            ),
            applicability=_http_any(),
            severity=Severity.HIGH,
            security_category=SecurityCategory.ORIGIN_VALIDATION,
            security_limitations=[
                "The requirement is normative and the failure is recorded as a "
                "SPEC_VIOLATION. That a MUST was violated is established here; that "
                "the violation is exploitable is not, and the two are reported "
                "separately.",
                "Measured against one pinned build launched through one entry point "
                "under this harness's containment. Whether Origin protection is "
                "absent, opt-in, enabled by a different entry point, or delegated to "
                "a deployment layer was not established by this scenario.",
                "The request originates from the harness, not from a browser. No "
                "same-origin policy, no DNS resolution and no rebinding step is "
                "involved, so nothing here demonstrates a reachable attack path.",
            ],
            expected_outcome=(
                "HTTP 403 Forbidden. The binding requires servers to validate Origin "
                "and names 403 for the rejection. A server that answers 400 has still "
                "rejected the request and is recorded as divergent rather than "
                "insecure; a server that answers 200 has not validated Origin at all, "
                "which is what this scenario is for."
            ),
            normative_source=(
                f"{TRANSPORT_BOTH}: 'Servers MUST validate the Origin header on all "
                "incoming connections to prevent DNS rebinding attacks.'"
            ),
            rationale=(
                "Failure Lab addition: hostile-but-synthetic input under containment. "
                "The Origin value names a host that does not exist, no lookup is "
                "performed, and nothing leaves the machine."
            ),
        ),
        steps=[
            Step(
                step_id="bad_origin",
                kind=StepKind.HTTP_REQUEST,
                http_method="POST",
                http_body_text=('{"jsonrpc":"2.0","id":9101,"method":"tools/list","params":{}}'),
                http_header_overrides={
                    # A literal that resolves to nothing. Deliberately not a real
                    # domain: a scenario that named one would put a third party's
                    # host into every artifact this produces.
                    "Origin": "https://interop-lab-invalid-origin.invalid",
                },
                optional=False,
                timeout_s=5.0,
                timeout_is_expected=True,
                assertions=[
                    AssertionSpec(
                        assertion_id="forbidden",
                        kind=AssertionKind.HTTP_STATUS_IN,
                        expected_statuses=[403],
                        description="an unrecognised Origin is rejected with 403",
                        normative_source=f"{TRANSPORT_BOTH}: Servers MUST validate Origin",
                        # SPEC_VIOLATION, because the cited requirement is a MUST
                        # and the target did not meet it. That is a conformance
                        # fact and it is the primary outcome.
                        #
                        # This previously mapped to SECURITY_RELEVANT, which read
                        # as the more careful choice and was the less accurate
                        # one: it announced that the behaviour matters for
                        # exposure while silently dropping the finding that a
                        # dated MUST had been violated. Both are true, so both
                        # are recorded — the security reading below, on its own
                        # dimension, where it displaces nothing.
                        on_fail="SPEC_VIOLATION",
                        security_relevant=True,
                        security_category=SecurityCategory.ORIGIN_VALIDATION,
                    )
                ],
            )
        ],
    )


def http_get_stream_or_405() -> Scenario:
    """GET the endpoint: an event stream, or Method Not Allowed. Both conform."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="transport.http_get_stream_or_405",
            title="GET on the endpoint opens an event stream or is refused with 405",
            category=ScenarioCategory.WIRE_INTEGRITY,
            nature=ScenarioNature.NORMATIVE,
            purpose=(
                "The binding permits two answers and a client has to handle both. A "
                "server answering anything else — a 200 with a JSON body, a 404 — "
                "leaves a client that opened the stream with no way to tell whether "
                "server-initiated messages are available."
            ),
            # 2025-11-25 only, and the restriction is load-bearing rather than
            # cautious. 2026-07-28 removed the standalone GET stream altogether
            # ("None of these mechanisms are part of this revision") and tells a
            # server meeting one to answer 405. Accepting 200 as conforming there
            # would pass a server the newer binding says must refuse — asserting
            # the 2025 alternative against a revision that does not offer it.
            applicability=_http_2025(),
            severity=Severity.MEDIUM,
            expected_outcome=(
                "Either a 200 whose content type is text/event-stream, or 405 Method "
                "Not Allowed. Both are conforming and neither is preferred here.\n\n"
                "The probe stops after the response headers. That is a deliberate "
                "limit, not an oversight: a conforming stream never completes, so "
                "reading to the end would hang against a correct server. Nothing in "
                "this scenario establishes that a stream *works* — no event is "
                "consumed, no Last-Event-ID resumption is attempted, and no "
                "server-initiated message is awaited."
            ),
            normative_source=(
                f"{TRANSPORT_2025}: 'The client MAY issue an HTTP GET to the MCP "
                "endpoint... The server MUST either return Content-Type: "
                "text/event-stream in response to this HTTP GET, or else return HTTP "
                "405 Method Not Allowed.'"
            ),
            rationale=(
                "Failure Lab addition: the alternative is asserted as an alternative. "
                "A check that demanded one of the two would report a conforming "
                "implementation as defective for making the other legal choice."
            ),
        ),
        steps=[
            Step(
                step_id="get_endpoint",
                kind=StepKind.HTTP_REQUEST,
                http_method="GET",
                # Headers only. See expected_outcome: a conforming stream does
                # not end, so a probe that read the body would hang on success.
                http_abort_after_headers=True,
                optional=False,
                timeout_s=5.0,
                timeout_is_expected=True,
                assertions=[
                    AssertionSpec(
                        assertion_id="stream_or_method_not_allowed",
                        kind=AssertionKind.HTTP_STATUS_IN,
                        expected_statuses=[200, 405],
                        description="the GET is answered with a stream or with 405",
                        normative_source=(
                            f"{TRANSPORT_2025}: text/event-stream or 405 Method Not Allowed"
                        ),
                        on_fail="BEHAVIORAL_DIVERGENCE",
                    )
                ],
            )
        ],
    )


def http_unsupported_protocol_version() -> Scenario:
    """An explicitly unsupported MCP-Protocol-Version on a post-handshake request."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="version.http_unsupported_protocol_version",
            title="An unsupported MCP-Protocol-Version header is rejected",
            category=ScenarioCategory.VERSION_HEADER,
            nature=ScenarioNature.NORMATIVE,
            purpose=(
                "The header is how a 2025 client states which revision it is speaking "
                "on every request after the handshake. A server that ignores an "
                "unsupported value answers in a revision the client cannot read."
            ),
            applicability=_http_2025(),
            severity=Severity.MEDIUM,
            expected_outcome=(
                "HTTP 400 Bad Request. The binding names 400 for an invalid or "
                "unsupported value in this header."
            ),
            normative_source=(
                f"{TRANSPORT_2025}: 'If the server receives a request with an invalid "
                "or unsupported MCP-Protocol-Version, it MUST respond with 400 Bad "
                "Request.'"
            ),
            rationale=(
                "Failure Lab addition: sent after a correct handshake, so an "
                "uninitialized-session error cannot be mistaken for version rejection."
            ),
        ),
        steps=[
            Step(
                step_id="unsupported_version",
                kind=StepKind.HTTP_REQUEST,
                http_method="POST",
                http_body_text=('{"jsonrpc":"2.0","id":9102,"method":"tools/list","params":{}}'),
                http_header_overrides={"MCP-Protocol-Version": "1999-01-01"},
                optional=False,
                timeout_s=5.0,
                timeout_is_expected=True,
                assertions=[
                    AssertionSpec(
                        assertion_id="bad_request",
                        kind=AssertionKind.HTTP_STATUS_IN,
                        expected_statuses=[400],
                        description="an unsupported protocol version is refused with 400",
                        normative_source=f"{TRANSPORT_2025}: MUST respond with 400 Bad Request",
                        on_fail="BEHAVIORAL_DIVERGENCE",
                    )
                ],
            )
        ],
    )


def http_accept_incomplete() -> Scenario:
    """An Accept header naming only one of the two required media types.

    Read this scenario's classification carefully. The binding places its
    requirement on what a *client* advertises — it does not say what a server
    must do when a client advertises less. So neither acceptance nor rejection is
    a server defect, and this scenario is a differential probe rather than a
    normative one. It exists because the difference is real, is load-bearing for
    interoperability, and has already cost this project once: the harness sent
    `Accept: application/json` until an external implementation answered 406, and
    that was a defect in the harness.
    """
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="transport.http_accept_incomplete",
            title="An Accept header naming only application/json",
            category=ScenarioCategory.WIRE_INTEGRITY,
            nature=ScenarioNature.DIFFERENTIAL,
            purpose=(
                "Implementations differ on whether they enforce the client-side Accept "
                "requirement, and a client that gets this wrong fails against some "
                "servers and not others. Measuring the difference is worth doing; "
                "calling either answer wrong is not."
            ),
            applicability=_http_any(),
            severity=Severity.LOW,
            expected_outcome=(
                "Either an ordinary answer (200), or a refusal (406 Not Acceptable, or "
                "400). All three are recorded as an expected difference between "
                "implementations.\n\n"
                "No outcome here is a SPEC_VIOLATION. The binding says a client 'MUST "
                "include an Accept header, listing both application/json and "
                "text/event-stream'. That is a requirement on the client. It states no "
                "server obligation for the case where the client did not, so no dated "
                "source supports grading a server's choice — and inventing one would "
                "be exactly the error this project refuses."
            ),
            normative_source=(
                f"{TRANSPORT_BOTH}: 'The client MUST include an Accept header, listing "
                "both application/json and text/event-stream as supported content "
                "types.' A client-side requirement, cited here to establish that the "
                "server-side obligation is absent rather than overlooked."
            ),
            rationale=(
                "Differential rationale: no normative server obligation exists, so "
                "this compares implementations rather than judging them. The harness's "
                "own past defect — sending application/json alone and being answered "
                "406 — is why the difference is recorded rather than assumed away."
            ),
        ),
        steps=[
            Step(
                step_id="narrow_accept",
                kind=StepKind.HTTP_REQUEST,
                http_method="POST",
                http_body_text=('{"jsonrpc":"2.0","id":9103,"method":"tools/list","params":{}}'),
                http_header_overrides={"Accept": "application/json"},
                optional=False,
                timeout_s=5.0,
                timeout_is_expected=True,
                assertions=[
                    AssertionSpec(
                        assertion_id="answered_or_refused",
                        kind=AssertionKind.HTTP_STATUS_IN,
                        expected_statuses=[200, 202, 400, 406],
                        description=(
                            "the server answers or refuses; both are recorded, neither "
                            "is graded as a defect"
                        ),
                        normative_source=(
                            f"{TRANSPORT_BOTH}: the Accept requirement is stated for "
                            "clients; no server obligation is defined for this case"
                        ),
                        on_fail="EXPECTED_DIFFERENCE",
                    )
                ],
            )
        ],
    )


# ------------------------------------------------ 2025 stateful HTTP lifecycle
#
# Restricted to 2025-11-25 throughout. 2026-07-28 removed `initialize` and the
# session model with it, so none of these questions can be asked of that revision
# and asserting them there would assert a requirement outside its dated profile.


def http_request_before_initialize() -> Scenario:
    """An ordinary request sent before the handshake."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="lifecycle.http_request_before_initialize",
            title="An operation before initialization does not succeed",
            category=ScenarioCategory.STATEFUL_LIFECYCLE,
            nature=ScenarioNature.NORMATIVE,
            purpose=(
                "The 2025 lifecycle exists so both sides agree on a revision and a "
                "capability set before anything is done. A server that performs "
                "operations regardless has made the handshake optional in practice, "
                "and a client that skipped it never learns what it is talking to."
            ),
            applicability=_http_2025(),
            severity=Severity.MEDIUM,
            expected_outcome=(
                "The operation does not succeed: either a JSON-RPC error, or an HTTP "
                "status outside the 2xx range.\n\n"
                "The assertion is deliberately about the *outcome* rather than about a "
                "particular code. The 2025 revision requires the handshake but does "
                "not define an error code for performing an operation without one, so "
                "demanding a specific value would be inventing a requirement. What can "
                "be asserted is that the operation did not happen."
            ),
            normative_source=(
                f"{TRANSPORT_2025} and MCP 2025-11-25 lifecycle: 'The client MUST "
                "initiate this phase by sending an initialize request' before other "
                "requests. No error code is specified for the violation, which is why "
                "none is asserted."
            ),
            rationale=(
                "Failure Lab addition: the handshake is suppressed for this scenario "
                "only, which is the sole way to ask the question at all."
            ),
        ),
        # The one thing that makes this scenario possible, and the reason it is
        # its own scenario rather than a step in another one.
        suppress_profile_handshake=True,
        steps=[
            Step(
                step_id="premature_request",
                kind=StepKind.REQUEST,
                method="tools/list",
                optional=False,
                timeout_s=5.0,
                timeout_is_expected=True,
                assertions=[
                    AssertionSpec(
                        assertion_id="not_performed",
                        kind=AssertionKind.RESPONSE_IS_ERROR,
                        description=(
                            "the server does not return a successful result for an "
                            "operation requested before initialization"
                        ),
                        normative_source=(
                            "MCP 2025-11-25 lifecycle: initialization precedes other requests"
                        ),
                    )
                ],
            )
        ],
    )


def http_repeated_initialize() -> Scenario:
    """A second initialize on an already-initialized session."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="lifecycle.http_repeated_initialize",
            title="A second initialize on an initialized session",
            category=ScenarioCategory.STATEFUL_LIFECYCLE,
            nature=ScenarioNature.DIFFERENTIAL,
            purpose=(
                "A client that reconnects, or a proxy that replays a handshake, can "
                "send this. Implementations differ on whether they re-handshake, "
                "refuse, or start a second session, and each choice means something "
                "different for the session identifier the client already holds."
            ),
            applicability=_http_2025(),
            severity=Severity.LOW,
            expected_outcome=(
                "Either a second initialize result, or a JSON-RPC error refusing it. "
                "Both are recorded as an expected difference.\n\n"
                "Not graded as a violation in either direction. The 2025 revision "
                "requires the client to begin with initialize; it does not state what "
                "a server must do when a client sends a second one, so there is no "
                "dated requirement to hold anyone to. The value here is the "
                "comparison, and the constraint asserted is the one that does hold: "
                "whatever the server answers, it must still be a correlated JSON-RPC "
                "message and the session must still work afterwards."
            ),
            normative_source=(
                "MCP 2025-11-25 lifecycle. Cited to establish that no requirement "
                "governs a repeated initialize, rather than to assert one."
            ),
            rationale=(
                "Differential rationale: a real client behaviour with no specified "
                "server obligation. Comparing three implementations on it is "
                "informative; grading them on it would be inventing a rule."
            ),
        ),
        steps=[
            Step(
                step_id="second_initialize",
                kind=StepKind.REQUEST,
                method="initialize",
                params={
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "interop-lab", "version": "0.1.0"},
                },
                optional=False,
                timeout_s=5.0,
                timeout_is_expected=True,
                assertions=[
                    AssertionSpec(
                        assertion_id="id_matches",
                        kind=AssertionKind.RESPONSE_ID_MATCHES,
                        description=("whatever the server answers is correlated to the request"),
                        normative_source=(
                            "JSON-RPC 2.0 §5: the response id 'MUST be the same as the "
                            "value of the id member in the Request Object. If there was "
                            "an error in detecting the id in the Request Object (e.g. "
                            "Parse error/Invalid Request), it MUST be Null.'"
                        ),
                        # A divergence, not a violation, and the reason is in the
                        # citation above. A server that refuses a repeated
                        # initialize as an Invalid Request may return a null id
                        # under that carve-out — even though the id was plainly
                        # detectable, since the request was well-formed and the
                        # refusal is semantic rather than structural. Two
                        # defensible readings of one sentence is what
                        # BEHAVIORAL_DIVERGENCE is for, and grading it as a
                        # defect would pick a winner the text does not.
                        on_fail="BEHAVIORAL_DIVERGENCE",
                    )
                ],
            ),
            Step(
                step_id="session_still_usable",
                kind=StepKind.REQUEST,
                method="tools/list",
                optional=False,
                assertions=[
                    AssertionSpec(
                        assertion_id="recovers",
                        kind=AssertionKind.RESPONSE_IS_RESULT,
                        description="the session still works after a repeated initialize",
                        on_fail="RECOVERY_FAILURE",
                    )
                ],
            ),
        ],
    )


def http_request_after_delete() -> Scenario:
    """Terminate the session, then use the identifier it just released."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="lifecycle.http_request_after_delete",
            title="A request using a terminated session identifier is refused",
            category=ScenarioCategory.STATEFUL_LIFECYCLE,
            nature=ScenarioNature.NORMATIVE,
            purpose=(
                "A session identifier that keeps working after the client released it "
                "is a credential with no revocation. Anything that captured it — a "
                "log, a proxy, a crash dump — can go on using it."
            ),
            # Applicable only where a session identifier was actually observed.
            # A server that issues none has nothing to terminate, and running
            # this against one would manufacture a failure out of a choice
            # 2025-11-25 explicitly permits.
            applicability=_http_2025(transport_features={"session_identifier_issued": True}),
            severity=Severity.HIGH,
            expected_outcome=(
                "The DELETE is accepted, and the following request carrying the "
                "terminated identifier is refused with HTTP 404 Not Found.\n\n"
                "A server that answers 405 to the DELETE has not terminated anything — "
                "the binding permits refusing client-initiated termination — so this "
                "scenario accepts 405 on the DELETE step and grades only what follows "
                "when termination did happen."
            ),
            normative_source=(
                f"{TRANSPORT_2025}: 'Clients that no longer need a particular session "
                "SHOULD send an HTTP DELETE to the MCP endpoint... Servers MAY respond "
                "to this with 405 Method Not Allowed. ... If the server terminates the "
                "session, the client MUST start a new session; a request to a "
                "terminated session MUST be responded to with HTTP 404 Not Found.'"
            ),
            rationale=(
                "Failure Lab addition: a controlled lifecycle — establish, capture, "
                "terminate, reuse — rather than a single exchange. The permitted "
                "alternative (405) is accepted rather than reported as a defect."
            ),
        ),
        steps=[
            Step(
                step_id="delete_session",
                kind=StepKind.HTTP_REQUEST,
                http_method="DELETE",
                optional=False,
                timeout_s=5.0,
                timeout_is_expected=True,
                assertions=[
                    AssertionSpec(
                        assertion_id="terminated_or_refused",
                        kind=AssertionKind.HTTP_STATUS_IN,
                        # 405 is the binding's own permitted alternative and is
                        # accepted here rather than reported as a defect.
                        expected_statuses=[200, 202, 204, 405],
                        description="the server terminates the session or refuses to",
                        normative_source=(
                            f"{TRANSPORT_2025}: servers MAY respond with 405 Method Not Allowed"
                        ),
                        on_fail="BEHAVIORAL_DIVERGENCE",
                    )
                ],
            ),
            Step(
                step_id="reuse_terminated_session",
                kind=StepKind.HTTP_REQUEST,
                http_method="POST",
                http_body_text=('{"jsonrpc":"2.0","id":9104,"method":"tools/list","params":{}}'),
                optional=False,
                timeout_s=5.0,
                timeout_is_expected=True,
                assertions=[
                    AssertionSpec(
                        assertion_id="terminated_session_refused",
                        kind=AssertionKind.HTTP_STATUS_IN,
                        expected_statuses=[404],
                        description=(
                            "a request carrying the terminated identifier is refused with 404"
                        ),
                        normative_source=(
                            f"{TRANSPORT_2025}: a request to a terminated session MUST "
                            "be responded to with HTTP 404 Not Found"
                        ),
                        # Not SPEC_VIOLATION outright: a server that refused the
                        # DELETE with 405 never terminated the session, and its
                        # 200 here is entirely correct. The two steps are read
                        # together by a reviewer, and the classification says
                        # "these implementations differ" rather than "this one
                        # is wrong".
                        on_fail="BEHAVIORAL_DIVERGENCE",
                    )
                ],
            ),
        ],
    )


HTTP_SCENARIOS: dict[str, Scenario] = {
    scenario.scenario_id: scenario
    for scenario in (
        http_baseline(),
        http_header_body_mismatch(),
        http_header_body_mismatch_noisy(),
        http_slow_body(),
        http_connection_reset(),
        http_malformed_body(),
        http_dropped_request(),
        http_notification_accepted(),
        http_invalid_origin(),
        http_get_stream_or_405(),
        http_unsupported_protocol_version(),
        http_accept_incomplete(),
        http_request_before_initialize(),
        http_repeated_initialize(),
        http_request_after_delete(),
    )
}

HTTP_SUITES: dict[str, list[str]] = {
    "http-smoke": ["baseline.http_tools_list"],
    "http-core": [
        "baseline.http_tools_list",
        "version.http_header_body_mismatch",
    ],
    "http-hostile": [
        "version.http_header_body_mismatch",
        "version.http_header_body_mismatch_noisy",
        "timing.http_slow_body",
        "recovery.http_connection_reset",
        "wire.http_malformed_body",
        "timing.http_dropped_request",
    ],
    # Transport-binding semantics: methods, statuses and headers rather than
    # messages. Every one cites the dated transport binding.
    "http-transport": [
        "transport.http_notification_accepted",
        "security.http_invalid_origin",
        "transport.http_get_stream_or_405",
        "version.http_unsupported_protocol_version",
        "transport.http_accept_incomplete",
    ],
    # The 2025 stateful session model, which 2026-07-28 removed entirely.
    "http-lifecycle": [
        "lifecycle.http_request_before_initialize",
        "lifecycle.http_repeated_initialize",
        "lifecycle.http_request_after_delete",
    ],
    "http-all": list(HTTP_SCENARIOS),
}
