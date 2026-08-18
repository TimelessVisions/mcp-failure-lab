"""Scenario library.

Every normative assertion cites the dated requirement it enforces. Citations
name the schema definition the requirement was read from, so a reviewer can
check the claim against the official schema rather than trusting this file.
"""

from __future__ import annotations

from interop_lab.injection.faults import Boundary, Fault, FaultKind, FaultTrigger
from interop_lab.models.core import (
    Applicability,
    ScenarioCategory,
    ScenarioDefinition,
    ScenarioNature,
    Severity,
)
from interop_lab.profiles.registry import (
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_PARSE_ERROR,
    Role,
    Transport,
)
from interop_lab.scenarios.model import AssertionKind, AssertionSpec, Scenario, Step, StepKind

PROFILE_2026 = "mcp-2026-07-28"
PROFILE_2025 = "mcp-2025-11-25"

SCHEMA_2026 = "MCP 2026-07-28 schema.json"

#: The base wire specification both dated MCP revisions are built on.
#: Cited where a requirement holds under *every* profile a scenario applies
#: to. Citing only the 2026 schema for such a requirement would assert a
#: 2026 document against a 2025-11-25 target — the 2025-11-25 schema does
#: not define the standard JSON-RPC error codes at all.
JSONRPC_ERRORS = "JSON-RPC 2.0 §5.1 Error object"

#: Both dated schemas, cited together where a scenario applies to both and
#: each document states the requirement independently. Naming only the newer
#: one would cite a document that does not govern the target being measured.
SCHEMA_BOTH = "MCP 2025-11-25 and 2026-07-28 schema.json"

STDIO_ONLY = [Transport.STDIO]
BOTH_TRANSPORTS = [Transport.STDIO, Transport.STREAMABLE_HTTP]
SERVERS = [Role.SERVER, Role.FIXTURE]


def _applicability_cross_transport(
    *, tools: list[str] | None = None, safe_invocation: bool = False
) -> Applicability:
    """Scenarios whose semantics are the same over stdio and Streamable HTTP.

    These are the ones that make the matrix a comparison rather than two separate
    reports. A JSON-RPC request-validation rule is a rule about the message, not
    about how the message arrived, so restricting it to one transport would mean
    measuring three stdio servers and one HTTP server against different
    requirements and calling the result a matrix.

    Where the *framing* genuinely changes the assertion — a notification is
    answered at the transport level over HTTP and not at all over stdio — the
    scenario says so in its expected outcome and asserts the part that holds on
    both.
    """
    return Applicability(
        profiles=[PROFILE_2025, PROFILE_2026],
        transports=BOTH_TRANSPORTS,
        roles=SERVERS,
        requires_tools=tools or [],
        requires_safe_tool_invocation=safe_invocation,
    )


def _applicability_2026(tools: list[str] | None = None) -> Applicability:
    """Scenarios that assert a requirement introduced by the 2026-07-28 revision."""
    return Applicability(
        profiles=[PROFILE_2026],
        min_spec_version="2026-07-28",
        transports=STDIO_ONLY,
        roles=SERVERS,
        requires_tools=tools or [],
    )


def _applicability_any(tools: list[str] | None = None) -> Applicability:
    """Scenarios resting on JSON-RPC or on methods present in both profiles.

    These are the scenarios that make cross-implementation comparison possible,
    because both a 2025-era and a 2026-era target can legitimately run them.
    """
    return Applicability(
        profiles=[PROFILE_2025, PROFILE_2026],
        transports=STDIO_ONLY,
        roles=SERVERS,
        requires_tools=tools or [],
    )


# ------------------------------------------------------------------- baseline


def baseline_tools_list() -> Scenario:
    """A conforming server answers tools/list with a well-formed result."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="baseline.tools_list",
            title="tools/list returns a well-formed result",
            category=ScenarioCategory.BASELINE,
            nature=ScenarioNature.NORMATIVE,
            purpose="Establish that the target answers ordinary traffic before anything hostile.",
            applicability=_applicability_any(),
            severity=Severity.HIGH,
            expected_outcome=(
                "A result response correlated by id, containing a `tools` array and the "
                "`resultType` member required by the 2026-07-28 schema."
            ),
            normative_source=f"{SCHEMA_BOTH}: ListToolsResult (required: tools)",
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
                        description="tools/list returns a result, not an error",
                        normative_source=f"{SCHEMA_BOTH}: ListToolsResult (required: tools)",
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
                        normative_source=f"{SCHEMA_BOTH}: ListToolsResult.tools",
                    ),
                ],
            )
        ],
    )


def baseline_tool_call() -> Scenario:
    """A conforming server answers tools/call with the required result envelope."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="baseline.tool_call",
            title="tools/call returns a conforming CallToolResult",
            category=ScenarioCategory.BASELINE,
            nature=ScenarioNature.NORMATIVE,
            purpose="Exercise the primary tool-invocation path end to end.",
            applicability=_applicability_any(tools=["echo"]),
            severity=Severity.HIGH,
            expected_outcome=(
                "A result whose `content` member is an array. This holds in both the "
                "2025-11-25 and 2026-07-28 schemas, which is what makes the scenario "
                "usable for cross-era comparison."
            ),
            normative_source="CallToolResult.content (present in both profiles)",
        ),
        steps=[
            Step(
                step_id="call",
                kind=StepKind.REQUEST,
                method="tools/call",
                params={"name": "echo", "arguments": {"text": "interop"}},
                optional=False,
                assertions=[
                    AssertionSpec(
                        assertion_id="is_result",
                        kind=AssertionKind.RESPONSE_IS_RESULT,
                        description="tools/call returns a result",
                    ),
                    AssertionSpec(
                        assertion_id="content_is_array",
                        kind=AssertionKind.RESULT_FIELD_TYPE,
                        path="content",
                        expected_type="array",
                        description="result.content is an array",
                        normative_source=(
                            f"{SCHEMA_BOTH}: CallToolResult.content, required in both"
                        ),
                    ),
                ],
            )
        ],
    )


def result_type_required() -> Scenario:
    """`resultType` is required on CallToolResult from 2026-07-28 onward.

    Deliberately kept separate from the cross-era baseline: asserting it against
    a 2025-11-25 target would be a false positive, since that schema has no such
    member. This is the scenario that proves version gating is load-bearing.
    """
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="version.result_type_required",
            title="CallToolResult carries the required resultType member",
            category=ScenarioCategory.VERSION_HEADER,
            nature=ScenarioNature.NORMATIVE,
            purpose=(
                "A client on 2026-07-28 uses resultType to decide how to parse the "
                "result. Its absence forces the client to guess."
            ),
            applicability=_applicability_2026(tools=["echo"]),
            severity=Severity.HIGH,
            expected_outcome="A result containing a `resultType` member.",
            normative_source=(
                f"{SCHEMA_2026}: CallToolResult.required = [content, resultType]; "
                "'Servers implementing this protocol version MUST include this field.'"
            ),
        ),
        steps=[
            Step(
                step_id="call",
                kind=StepKind.REQUEST,
                method="tools/call",
                params={"name": "echo", "arguments": {"text": "interop"}},
                optional=False,
                assertions=[
                    AssertionSpec(
                        assertion_id="is_result",
                        kind=AssertionKind.RESPONSE_IS_RESULT,
                        description="tools/call returns a result",
                    ),
                    AssertionSpec(
                        assertion_id="result_type_present",
                        kind=AssertionKind.RESULT_HAS_FIELD,
                        path="resultType",
                        description="result.resultType is present",
                        normative_source=(
                            f"{SCHEMA_2026}: CallToolResult.required includes resultType"
                        ),
                    ),
                ],
            )
        ],
    )


# ------------------------------------------------------------- wire integrity


def wire_malformed_json() -> Scenario:
    """Truncated JSON on the wire must produce a parse error, not a crash."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="wire.malformed_json",
            title="Malformed JSON produces a parse error and the server survives",
            category=ScenarioCategory.WIRE_INTEGRITY,
            nature=ScenarioNature.RESILIENCE,
            purpose="A hostile or buggy client must not be able to kill the server with bad JSON.",
            applicability=_applicability_any(),
            severity=Severity.HIGH,
            expected_outcome=(
                "The load-bearing requirement is recovery: the server must still answer "
                "the next well-formed request. Emitting a -32700 parse error is also "
                "expected by JSON-RPC 2.0, but implementations differ on whether a "
                "framed stdio transport should answer input it could not parse, so that "
                "assertion is graded as a divergence rather than a violation."
            ),
            normative_source="JSON-RPC 2.0 §5.1 Parse error (-32700)",
        ),
        steps=[
            Step(
                step_id="malformed",
                kind=StepKind.RAW,
                raw_payload='{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{',
                optional=False,
                # Silence is a legitimate observation here, not a reason to abort:
                # the interesting question is what happens on the *next* request.
                timeout_s=2.0,
                timeout_is_expected=True,
                assertions=[
                    AssertionSpec(
                        assertion_id="parse_error_returned",
                        kind=AssertionKind.ERROR_CODE_EQUALS,
                        expected=JSONRPC_PARSE_ERROR,
                        description="malformed JSON is answered with -32700",
                        normative_source="JSON-RPC 2.0 §5.1",
                        # Not SPEC_VIOLATION: see expected_outcome. Silently
                        # discarding unparseable stdio input is a defensible
                        # reading, so this is reported as an observed divergence.
                        on_fail="BEHAVIORAL_DIVERGENCE",
                    ),
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
                        description="server answers normally after malformed input",
                        on_fail="RECOVERY_FAILURE",
                    )
                ],
            ),
        ],
    )


def wire_unknown_method() -> Scenario:
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="wire.unknown_method",
            title="Unknown method produces -32601",
            category=ScenarioCategory.WIRE_INTEGRITY,
            nature=ScenarioNature.NORMATIVE,
            purpose="Method dispatch must reject unknown methods with the standard code.",
            applicability=_applicability_any(),
            severity=Severity.MEDIUM,
            expected_outcome="An error response with code -32601.",
            normative_source=(
                f"{JSONRPC_ERRORS}: -32601 Method not found. Restated in "
                f"{SCHEMA_2026} as MethodNotFoundError. Cited to JSON-RPC because this "
                "scenario also applies to mcp-2025-11-25, whose schema does not "
                "enumerate the standard error codes."
            ),
        ),
        steps=[
            Step(
                step_id="unknown",
                kind=StepKind.REQUEST,
                method="tools/definitely_not_a_method",
                optional=False,
                assertions=[
                    AssertionSpec(
                        assertion_id="is_error",
                        kind=AssertionKind.RESPONSE_IS_ERROR,
                        description="unknown method produces an error",
                    ),
                    AssertionSpec(
                        assertion_id="code",
                        kind=AssertionKind.ERROR_CODE_EQUALS,
                        expected=JSONRPC_METHOD_NOT_FOUND,
                        description="error code is -32601",
                        normative_source=(
                            f"{JSONRPC_ERRORS}: -32601 Method not found "
                            f"(restated in {SCHEMA_2026} as MethodNotFoundError)"
                        ),
                    ),
                ],
            )
        ],
    )


def wire_response_id_correlation() -> Scenario:
    """The response to a request must carry that request's id."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="wire.response_id_correlation",
            title="Response id equals request id",
            category=ScenarioCategory.WIRE_INTEGRITY,
            nature=ScenarioNature.NORMATIVE,
            purpose=(
                "A client correlates concurrent requests by id. A wrong id silently "
                "mismatches results to calls."
            ),
            applicability=_applicability_any(tools=["echo"]),
            severity=Severity.HIGH,
            expected_outcome="A result whose id is identical to the request id.",
            normative_source="JSON-RPC 2.0 §5: 'It MUST be the same as the value of the id "
            "member in the Request Object.'",
        ),
        steps=[
            Step(
                step_id="call",
                kind=StepKind.REQUEST,
                method="tools/call",
                params={"name": "echo", "arguments": {"text": "correlate"}},
                optional=False,
                # A wrong id means no correlated response arrives at all, so the
                # request times out. That is the observable symptom.
                timeout_s=4.0,
                assertions=[
                    AssertionSpec(
                        assertion_id="id_matches",
                        kind=AssertionKind.RESPONSE_ID_MATCHES,
                        description="response id equals request id",
                        normative_source="JSON-RPC 2.0 §5",
                    )
                ],
            )
        ],
    )


# ------------------------------------------------------ version / header rules


def version_unsupported_rejected() -> Scenario:
    """An unknown protocol version must be rejected with -32022."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="version.unsupported_rejected",
            title="Unsupported protocol version is rejected with -32022",
            category=ScenarioCategory.VERSION_HEADER,
            nature=ScenarioNature.NORMATIVE,
            purpose=(
                "Version negotiation is per-request in 2026-07-28. A server that silently "
                "accepts an unknown version gives the client no way to downgrade."
            ),
            applicability=_applicability_2026(),
            severity=Severity.HIGH,
            expected_outcome=(
                "An error response with code -32022 whose data lists the supported versions."
            ),
            normative_source=(
                f"{SCHEMA_2026}: UnsupportedProtocolVersionError code = -32022; "
                "'If the server does not support the requested version, it MUST return an "
                "UnsupportedProtocolVersionError.'"
            ),
        ),
        steps=[
            Step(
                step_id="bad_version",
                kind=StepKind.REQUEST,
                method="tools/list",
                params={"_meta": {"io.modelcontextprotocol/protocolVersion": "1999-01-01"}},
                optional=False,
                assertions=[
                    AssertionSpec(
                        assertion_id="is_error",
                        kind=AssertionKind.RESPONSE_IS_ERROR,
                        description="unsupported version produces an error",
                    ),
                    AssertionSpec(
                        assertion_id="code_32022",
                        kind=AssertionKind.ERROR_CODE_EQUALS,
                        expected=-32022,
                        description="error code is -32022",
                        normative_source=f"{SCHEMA_2026}: UnsupportedProtocolVersionError",
                    ),
                ],
            )
        ],
    )


def version_missing_meta_rejected() -> Scenario:
    """`params._meta` is a required member in 2026-07-28."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="version.missing_request_meta",
            title="Request without params._meta is rejected",
            category=ScenarioCategory.VERSION_HEADER,
            nature=ScenarioNature.NORMATIVE,
            purpose="The stateless model depends on every request being self-describing.",
            applicability=_applicability_2026(),
            severity=Severity.MEDIUM,
            expected_outcome="An error response; -32602 is the expected code for invalid params.",
            normative_source=f"{SCHEMA_2026}: RequestParams.required = ['_meta']",
        ),
        steps=[
            Step(
                step_id="strip_meta",
                kind=StepKind.REQUEST,
                method="tools/list",
                optional=False,
                assertions=[
                    AssertionSpec(
                        assertion_id="is_error",
                        kind=AssertionKind.RESPONSE_IS_ERROR,
                        description="missing _meta produces an error",
                        normative_source=f"{SCHEMA_2026}: RequestParams.required",
                    ),
                    AssertionSpec(
                        assertion_id="code",
                        kind=AssertionKind.ERROR_CODE_IN,
                        expected_codes=[JSONRPC_INVALID_PARAMS, -32020, -32022],
                        description="error code is one of the params/header/version codes",
                    ),
                ],
            )
        ],
        faults=[
            Fault(
                fault_id="remove_request_meta",
                boundary=Boundary.OUTBOUND_MESSAGE,
                kind=FaultKind.REMOVE_FIELD,
                field_path="params._meta",
                trigger=FaultTrigger(occurrence=0),
            )
        ],
    )


# ------------------------------------------------------------- timing / hangs


def timing_response_deadline() -> Scenario:
    """A server that does not answer within the deadline must be reported as hung."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="timing.response_deadline",
            title="Slow tool exceeds the client deadline",
            category=ScenarioCategory.TIMING_CANCELLATION,
            nature=ScenarioNature.RESILIENCE,
            purpose=(
                "Establish that the harness distinguishes a silent-but-alive target "
                "(HANG) from a dead one (TIMEOUT), and attributes the deadline correctly."
            ),
            applicability=_applicability_any(tools=["slow_echo"]),
            severity=Severity.MEDIUM,
            expected_outcome=(
                "Against a fast target, a normal result. Against a target configured to "
                "delay beyond the deadline, no response within the deadline."
            ),
            normative_source=None,
        ),
        steps=[
            Step(
                step_id="slow_call",
                kind=StepKind.REQUEST,
                method="tools/call",
                params={"name": "slow_echo", "arguments": {"text": "slow"}},
                optional=False,
                timeout_s=2.0,
                assertions=[
                    AssertionSpec(
                        assertion_id="answers_in_time",
                        kind=AssertionKind.RESPONSE_IS_RESULT,
                        description="tool answers within the 2s deadline",
                        on_fail="TIMEOUT",
                    )
                ],
            )
        ],
    )


def timing_dropped_request() -> Scenario:
    """Transport loss must be classified as a timeout, not a target defect."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="timing.dropped_request",
            title="Dropped request produces a harness-attributed timeout",
            category=ScenarioCategory.TIMING_CANCELLATION,
            nature=ScenarioNature.RESILIENCE,
            purpose=(
                "Negative control: when the harness itself drops the request, the target "
                "is blameless. Proves timeout attribution is not blindly blaming the target."
            ),
            applicability=_applicability_any(),
            severity=Severity.INFO,
            expected_outcome="No response, attributed to the injected drop rather than the target.",
        ),
        steps=[
            Step(
                step_id="dropped",
                kind=StepKind.REQUEST,
                method="tools/list",
                optional=False,
                timeout_s=1.5,
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
                fault_id="drop_first_request",
                boundary=Boundary.OUTBOUND_MESSAGE,
                kind=FaultKind.DROP,
                trigger=FaultTrigger(occurrence=0),
            )
        ],
    )


# ---------------------------------------------------------- process recovery


def recovery_after_crash() -> Scenario:
    """After the process is killed, the harness must observe it and clean up."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="recovery.process_terminated",
            title="Terminated process is detected and cleaned up",
            category=ScenarioCategory.PROCESS_RECOVERY,
            nature=ScenarioNature.RESILIENCE,
            purpose="Prove the harness detects target death rather than hanging forever.",
            applicability=_applicability_any(),
            severity=Severity.MEDIUM,
            expected_outcome="No response after termination; process tree fully reaped.",
        ),
        steps=[
            Step(
                step_id="warmup",
                kind=StepKind.REQUEST,
                method="tools/list",
                optional=False,
                assertions=[
                    AssertionSpec(
                        assertion_id="warmup_ok",
                        kind=AssertionKind.RESPONSE_IS_RESULT,
                        description="server healthy before termination",
                    )
                ],
            ),
            Step(
                step_id="after_kill",
                kind=StepKind.REQUEST,
                method="tools/list",
                optional=False,
                timeout_s=2.0,
                timeout_is_expected=True,
                assertions=[
                    AssertionSpec(
                        assertion_id="no_response_after_kill",
                        kind=AssertionKind.NO_RESPONSE,
                        description="a terminated process cannot answer",
                    )
                ],
            ),
        ],
        faults=[
            Fault(
                fault_id="kill_before_second_request",
                boundary=Boundary.OUTBOUND_MESSAGE,
                kind=FaultKind.TERMINATE_PROCESS,
                trigger=FaultTrigger(occurrence=1),
            )
        ],
    )


# ------------------------------------------------------ safe tool invocation


def baseline_safe_tool_call() -> Scenario:
    """The tools/call path, exercised with the call an operator approved here.

    ``baseline.tool_call`` requires a tool literally named ``echo`` taking
    ``{"text": ...}``. That is this repository's fixture surface and matches none
    of the external matrix — `mcp-server-time` has time tools, and the two
    everything-servers spell the argument ``message`` — so the scenario covering
    the primary tool-invocation path was NOT_APPLICABLE against every third-party
    target the project measures.

    This one takes its call from the target's approved ``safe_tool_invocation``,
    which the executor confirms against the observed surface before sending
    anything. The scenario is therefore the same scenario everywhere, which is
    what makes the three implementations comparable on it.
    """
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="baseline.safe_tool_call",
            title="An approved safe tool call returns a conforming result",
            category=ScenarioCategory.BASELINE,
            nature=ScenarioNature.NORMATIVE,
            purpose=(
                "Exercise the primary tool-invocation path end to end against every "
                "target in the matrix, over whichever transport it speaks, using a "
                "call reviewed for that target rather than a tool name borrowed from "
                "a local fixture."
            ),
            applicability=_applicability_cross_transport(safe_invocation=True),
            severity=Severity.HIGH,
            expected_outcome=(
                "A result response correlated by id, whose `content` member is an "
                "array. Both dated schemas require `content` on CallToolResult, which "
                "is what lets one scenario cover both eras. The 2026-only `resultType` "
                "requirement is asserted separately by version.result_type_required, "
                "because asserting it here would be a false positive against a "
                "2025-11-25 target."
            ),
            normative_source=(
                f"{SCHEMA_BOTH}: CallToolResult.required includes `content` in both revisions"
            ),
            rationale=(
                "Official conformance coverage: the MCP conformance suite exercises "
                "tools/call against a server it controls. This adds what it does not — "
                "the same call against three independent third-party implementations "
                "under containment, with the tool and its arguments bound to an "
                "operator-approved contract whose input-schema hash is confirmed "
                "against the observed surface first."
            ),
        ),
        steps=[
            Step(
                step_id="call",
                kind=StepKind.REQUEST,
                method="tools/call",
                # Params are filled from the target's approved contract by the
                # executor; a literal here would be a fixture's tool name again.
                use_safe_tool_invocation=True,
                optional=False,
                assertions=[
                    AssertionSpec(
                        assertion_id="is_result",
                        kind=AssertionKind.RESPONSE_IS_RESULT,
                        description="the approved call returns a result, not a protocol error",
                        normative_source=f"{SCHEMA_BOTH}: CallToolResult",
                    ),
                    AssertionSpec(
                        assertion_id="id_matches",
                        kind=AssertionKind.RESPONSE_ID_MATCHES,
                        description="the response is correlated to the request",
                        normative_source="JSON-RPC 2.0 §5",
                    ),
                    AssertionSpec(
                        assertion_id="content_is_array",
                        kind=AssertionKind.RESULT_FIELD_TYPE,
                        path="content",
                        expected_type="array",
                        description="result.content is an array",
                        normative_source=f"{SCHEMA_BOTH}: CallToolResult.content",
                    ),
                ],
            )
        ],
    )


# ------------------------------------ cross-transport JSON-RPC request integrity
#
# Every scenario in this group sends syntactically valid JSON that is not a valid
# JSON-RPC Request object. That distinction is the whole point: JSON-RPC 2.0
# separates -32700 Parse error ("invalid JSON was received") from -32600 Invalid
# Request ("the JSON sent is not a valid Request object"), and a server that
# cannot tell them apart tells a client the wrong thing about its own bug.
#
# They run after the profile's required handshake, so "not initialized" cannot
# mask request validation — a server answering an uninitialized-session error
# would be answering a different question than the one asked.


def wire_invalid_jsonrpc_version() -> Scenario:
    """`jsonrpc` present, valid JSON, and not `"2.0"`."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="wire.invalid_jsonrpc_version",
            title="A jsonrpc member that is not 2.0 is an Invalid Request",
            category=ScenarioCategory.WIRE_INTEGRITY,
            nature=ScenarioNature.NORMATIVE,
            purpose=(
                "A client that receives -32700 for a well-formed message concludes its "
                "serialiser is broken and looks for a bug that is not there. The two "
                "codes send it to two different places."
            ),
            applicability=_applicability_cross_transport(),
            severity=Severity.MEDIUM,
            expected_outcome=(
                "An error response with code -32600. The payload is syntactically "
                "valid JSON — it parses — so -32700 Parse error does not describe it."
            ),
            normative_source=(
                f"{JSONRPC_ERRORS}: -32600 Invalid Request, 'The JSON sent is not a "
                "valid Request object'; -32700 Parse error, 'Invalid JSON was "
                "received by the server'. Cited to JSON-RPC because this scenario "
                "applies to mcp-2025-11-25, whose schema does not enumerate the "
                "standard error codes, as well as to 2026-07-28, which restates them."
            ),
            rationale=(
                "Failure Lab addition: hostile-but-valid input, byte-exact on both "
                "transports, with the two error codes distinguished. Official "
                "conformance does not send a wrong protocol version in the body."
            ),
        ),
        steps=[
            Step(
                step_id="wrong_version",
                kind=StepKind.RAW,
                # Byte-exact. A JSON round trip anywhere in the path would not
                # repair this one, but the same channel carries the truncated
                # payloads where it would, and the two must behave identically.
                raw_payload='{"jsonrpc":"1.0","id":9001,"method":"tools/list","params":{}}',
                optional=False,
                timeout_s=5.0,
                timeout_is_expected=True,
                assertions=[
                    AssertionSpec(
                        assertion_id="is_error",
                        kind=AssertionKind.RESPONSE_IS_ERROR,
                        description="an invalid Request object is rejected",
                        normative_source=f"{JSONRPC_ERRORS}: -32600 Invalid Request",
                        on_fail="BEHAVIORAL_DIVERGENCE",
                    ),
                    AssertionSpec(
                        assertion_id="invalid_request_code",
                        kind=AssertionKind.ERROR_CODE_EQUALS,
                        expected=JSONRPC_INVALID_REQUEST,
                        description="the code is -32600, not -32700",
                        normative_source=(
                            f"{JSONRPC_ERRORS}: -32600 is for a syntactically valid "
                            "payload that is not a valid Request object"
                        ),
                        # A divergence rather than a violation: implementations
                        # differ on which code they choose here, and this
                        # scenario exists to measure that difference honestly
                        # before anyone calls one of them wrong.
                        on_fail="BEHAVIORAL_DIVERGENCE",
                    ),
                ],
            ),
            Step(
                step_id="recover",
                kind=StepKind.REQUEST,
                method="tools/list",
                optional=False,
                assertions=[
                    AssertionSpec(
                        assertion_id="recovers",
                        kind=AssertionKind.RESPONSE_IS_RESULT,
                        description="the server answers normally afterwards",
                        on_fail="RECOVERY_FAILURE",
                    )
                ],
            ),
        ],
    )


def wire_missing_method() -> Scenario:
    """Valid JSON, valid `jsonrpc`, no `method` at all."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="wire.missing_method",
            title="A Request with no method member is an Invalid Request",
            category=ScenarioCategory.WIRE_INTEGRITY,
            nature=ScenarioNature.NORMATIVE,
            purpose=(
                "`method` is required on a Request object. Its absence is a structural "
                "defect in the message, not a failure to parse it — and this is not "
                "malformed JSON by any reading."
            ),
            applicability=_applicability_cross_transport(),
            severity=Severity.MEDIUM,
            expected_outcome="An error response with code -32600.",
            normative_source=(
                f"{JSONRPC_ERRORS}: a Request object requires a `method` member; -32600 "
                "Invalid Request covers a payload that parses but is not one."
            ),
            rationale=(
                "Failure Lab addition. The payload is valid JSON that no parser would "
                "reject, which is what makes the answering code meaningful."
            ),
        ),
        steps=[
            Step(
                step_id="no_method",
                kind=StepKind.RAW,
                raw_payload='{"jsonrpc":"2.0","id":9002,"params":{}}',
                optional=False,
                timeout_s=5.0,
                timeout_is_expected=True,
                assertions=[
                    AssertionSpec(
                        assertion_id="is_error",
                        kind=AssertionKind.RESPONSE_IS_ERROR,
                        description="a Request with no method is rejected",
                        normative_source=f"{JSONRPC_ERRORS}: -32600 Invalid Request",
                        on_fail="BEHAVIORAL_DIVERGENCE",
                    ),
                    AssertionSpec(
                        assertion_id="invalid_request_code",
                        kind=AssertionKind.ERROR_CODE_EQUALS,
                        expected=JSONRPC_INVALID_REQUEST,
                        description="the code is -32600, not -32700",
                        normative_source=f"{JSONRPC_ERRORS}: -32600 Invalid Request",
                        on_fail="BEHAVIORAL_DIVERGENCE",
                    ),
                ],
            ),
            Step(
                step_id="recover",
                kind=StepKind.REQUEST,
                method="tools/list",
                optional=False,
                assertions=[
                    AssertionSpec(
                        assertion_id="recovers",
                        kind=AssertionKind.RESPONSE_IS_RESULT,
                        description="the server answers normally afterwards",
                        on_fail="RECOVERY_FAILURE",
                    )
                ],
            ),
        ],
    )


def wire_method_not_string() -> Scenario:
    """`method` present with the wrong JSON type."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="wire.method_not_string",
            title="A method member that is not a string is an Invalid Request",
            category=ScenarioCategory.WIRE_INTEGRITY,
            nature=ScenarioNature.NORMATIVE,
            purpose=(
                "The third shape of the same question: present-but-wrong rather than "
                "absent. A dispatcher that indexes a method table by an unchecked "
                "value can fail in ways an absent member never reaches."
            ),
            applicability=_applicability_cross_transport(),
            severity=Severity.MEDIUM,
            expected_outcome=(
                "An error response with code -32600. Not -32601: a non-string is not "
                "a method name that happens to be unknown."
            ),
            normative_source=(
                f"{JSONRPC_ERRORS}: `method` is 'A String containing the name of the "
                "method to be invoked'; a non-string payload is an Invalid Request."
            ),
            rationale="Failure Lab addition: type confusion on a required member.",
        ),
        steps=[
            Step(
                step_id="numeric_method",
                kind=StepKind.RAW,
                raw_payload='{"jsonrpc":"2.0","id":9003,"method":42,"params":{}}',
                optional=False,
                timeout_s=5.0,
                timeout_is_expected=True,
                assertions=[
                    AssertionSpec(
                        assertion_id="is_error",
                        kind=AssertionKind.RESPONSE_IS_ERROR,
                        description="a non-string method is rejected",
                        normative_source=f"{JSONRPC_ERRORS}: -32600 Invalid Request",
                        on_fail="BEHAVIORAL_DIVERGENCE",
                    ),
                    AssertionSpec(
                        assertion_id="invalid_request_code",
                        kind=AssertionKind.ERROR_CODE_EQUALS,
                        expected=JSONRPC_INVALID_REQUEST,
                        description="the code is -32600",
                        normative_source=f"{JSONRPC_ERRORS}: -32600 Invalid Request",
                        on_fail="BEHAVIORAL_DIVERGENCE",
                    ),
                ],
            ),
            Step(
                step_id="recover",
                kind=StepKind.REQUEST,
                method="tools/list",
                optional=False,
                assertions=[
                    AssertionSpec(
                        assertion_id="recovers",
                        kind=AssertionKind.RESPONSE_IS_RESULT,
                        description="the server answers normally afterwards",
                        on_fail="RECOVERY_FAILURE",
                    )
                ],
            ),
        ],
    )


def wire_invalid_params_shape() -> Scenario:
    """A known method called with `params` of the wrong structural type."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="wire.invalid_params_shape",
            title="params of an invalid structural type is rejected",
            category=ScenarioCategory.WIRE_INTEGRITY,
            nature=ScenarioNature.NORMATIVE,
            purpose=(
                "JSON-RPC allows params to be an object or an array and nothing else. "
                "A server that coerces a string into either has stopped validating the "
                "envelope, which is where a client's malformed request should stop."
            ),
            applicability=_applicability_cross_transport(),
            severity=Severity.MEDIUM,
            expected_outcome=(
                "An error response. -32602 Invalid params is the expected code and "
                "-32600 Invalid Request is a defensible alternative, since a params "
                "member of a forbidden type makes the object structurally invalid — "
                "so both are accepted and anything else is reported as divergence."
            ),
            normative_source=(
                f"{JSONRPC_ERRORS}: params 'MUST be an Array or Object'; -32602 Invalid "
                "params, -32600 Invalid Request."
            ),
            rationale=(
                "Failure Lab addition: a structurally invalid envelope on a method the "
                "server certainly implements, so a method-dispatch error cannot be "
                "mistaken for parameter validation."
            ),
        ),
        steps=[
            Step(
                step_id="string_params",
                kind=StepKind.RAW,
                raw_payload='{"jsonrpc":"2.0","id":9004,"method":"tools/list","params":"nope"}',
                optional=False,
                timeout_s=5.0,
                timeout_is_expected=True,
                assertions=[
                    AssertionSpec(
                        assertion_id="is_error",
                        kind=AssertionKind.RESPONSE_IS_ERROR,
                        description="params of a forbidden type is rejected",
                        normative_source=f"{JSONRPC_ERRORS}: params MUST be an Array or Object",
                        on_fail="BEHAVIORAL_DIVERGENCE",
                    ),
                    AssertionSpec(
                        assertion_id="params_or_request_code",
                        kind=AssertionKind.ERROR_CODE_IN,
                        expected_codes=[JSONRPC_INVALID_PARAMS, JSONRPC_INVALID_REQUEST],
                        description="the code is -32602 or -32600",
                        normative_source=f"{JSONRPC_ERRORS}: -32602 / -32600",
                        on_fail="BEHAVIORAL_DIVERGENCE",
                    ),
                ],
            ),
            Step(
                step_id="recover",
                kind=StepKind.REQUEST,
                method="tools/list",
                optional=False,
                assertions=[
                    AssertionSpec(
                        assertion_id="recovers",
                        kind=AssertionKind.RESPONSE_IS_RESULT,
                        description="the server answers normally afterwards",
                        on_fail="RECOVERY_FAILURE",
                    )
                ],
            ),
        ],
    )


def wire_notification_no_response() -> Scenario:
    """A valid notification must not be answered with a Response object."""
    return Scenario(
        definition=ScenarioDefinition(
            scenario_id="wire.notification_no_response",
            title="A notification receives no JSON-RPC response",
            category=ScenarioCategory.WIRE_INTEGRITY,
            nature=ScenarioNature.NORMATIVE,
            purpose=(
                "A client correlates by id. A response to a message that carried no id "
                "cannot be correlated to anything, and a client that receives one "
                "either discards it or mismatches it against a pending request."
            ),
            applicability=_applicability_cross_transport(),
            severity=Severity.MEDIUM,
            expected_outcome=(
                "No JSON-RPC response object correlated to the notification. Over "
                "stdio that means nothing comes back at all. Over Streamable HTTP the "
                "POST is still answered at the transport level — an HTTP 202 with an "
                "empty body is the conforming outcome — and that is not a JSON-RPC "
                "response, which is why the assertion is about the response object "
                "rather than about silence."
            ),
            normative_source=(
                f"{JSONRPC_ERRORS} / JSON-RPC 2.0 §4.1: 'A Notification is a Request "
                'object without an "id" member... The Server MUST NOT reply to a '
                "Notification.'"
            ),
            rationale=(
                "Failure Lab addition: the same semantic requirement asserted on both "
                "transports, with the transport-level acknowledgement explicitly "
                "excluded from what counts as a reply."
            ),
        ),
        steps=[
            Step(
                step_id="notify",
                kind=StepKind.NOTIFICATION,
                # `notifications/cancelled` exists in both dated revisions and
                # names a request id no server issued, so a conforming server has
                # nothing to cancel and nothing to say.
                method="notifications/cancelled",
                params={"requestId": 999_999},
                optional=False,
                timeout_s=2.0,
                timeout_is_expected=True,
                assertions=[
                    AssertionSpec(
                        assertion_id="no_response_object",
                        kind=AssertionKind.NO_JSONRPC_RESPONSE,
                        description="no JSON-RPC response object is correlated",
                        normative_source="JSON-RPC 2.0 §4.1: the Server MUST NOT reply",
                    )
                ],
            ),
            Step(
                step_id="still_alive",
                kind=StepKind.REQUEST,
                method="tools/list",
                optional=False,
                assertions=[
                    AssertionSpec(
                        assertion_id="recovers",
                        kind=AssertionKind.RESPONSE_IS_RESULT,
                        description="the server answers normally after the notification",
                        on_fail="RECOVERY_FAILURE",
                    )
                ],
            ),
        ],
    )


from interop_lab.scenarios.http_library import HTTP_SCENARIOS, HTTP_SUITES  # noqa: E402

STDIO_SCENARIOS: dict[str, Scenario] = {
    scenario.scenario_id: scenario
    for scenario in (
        baseline_tools_list(),
        baseline_tool_call(),
        baseline_safe_tool_call(),
        result_type_required(),
        wire_malformed_json(),
        wire_unknown_method(),
        wire_response_id_correlation(),
        wire_invalid_jsonrpc_version(),
        wire_missing_method(),
        wire_method_not_string(),
        wire_invalid_params_shape(),
        wire_notification_no_response(),
        version_unsupported_rejected(),
        version_missing_meta_rejected(),
        timing_response_deadline(),
        timing_dropped_request(),
        recovery_after_crash(),
    )
}

ALL_SCENARIOS: dict[str, Scenario] = {**STDIO_SCENARIOS, **HTTP_SCENARIOS}

SUITES: dict[str, list[str]] = {
    "smoke": ["baseline.tools_list", "baseline.tool_call"],
    # Every scenario that means the same thing on stdio and Streamable HTTP.
    # Named as a suite because it is the comparison the matrix exists to make.
    "cross-transport": [
        "baseline.safe_tool_call",
        "wire.invalid_jsonrpc_version",
        "wire.missing_method",
        "wire.method_not_string",
        "wire.invalid_params_shape",
        "wire.notification_no_response",
    ],
    # Syntactically valid JSON that is not a valid Request object. Grouped so the
    # parse-error/invalid-request distinction can be measured in one pass.
    "request-integrity": [
        "wire.invalid_jsonrpc_version",
        "wire.missing_method",
        "wire.method_not_string",
        "wire.invalid_params_shape",
    ],
    "core": [
        "baseline.tools_list",
        "baseline.tool_call",
        "wire.malformed_json",
        "wire.unknown_method",
        "wire.response_id_correlation",
    ],
    "hostile": [
        "wire.malformed_json",
        "wire.unknown_method",
        "wire.response_id_correlation",
        "version.unsupported_rejected",
        "version.missing_request_meta",
        "version.result_type_required",
    ],
    "recovery": [
        "timing.response_deadline",
        "timing.dropped_request",
        "recovery.process_terminated",
    ],
    "all": list(ALL_SCENARIOS),
    **HTTP_SUITES,
}


def get_scenario(scenario_id: str) -> Scenario:
    try:
        return ALL_SCENARIOS[scenario_id]
    except KeyError:
        raise KeyError(
            f"unknown scenario {scenario_id!r}; known: {sorted(ALL_SCENARIOS)}"
        ) from None


def get_suite(name: str) -> list[Scenario]:
    try:
        return [ALL_SCENARIOS[sid] for sid in SUITES[name]]
    except KeyError:
        raise KeyError(f"unknown suite {name!r}; known: {sorted(SUITES)}") from None
