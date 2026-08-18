"""Declarative scenario steps.

Scenarios are data, not code paths. That is what makes replay exact and makes
minimisation possible: the minimiser removes steps and shrinks payloads on the
same structure the runner executes.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from interop_lab.injection.faults import Fault
from interop_lab.models.core import Classification, ScenarioDefinition, SecurityCategory


class StepKind(StrEnum):
    #: Build a profile-correct JSON-RPC request and await its response.
    REQUEST = "request"
    #: Send literal bytes; used for malformed-wire scenarios.
    RAW = "raw"
    #: Send a notification and assert that no response comes back.
    NOTIFICATION = "notification"
    #: Await one inbound message without sending anything first.
    EXPECT_MESSAGE = "expect_message"
    #: One HTTP exchange described at the transport level: a method, a path,
    #: headers and an optional literal body.
    #:
    #: Distinct from ``RAW`` because ``RAW`` is still a POST of a JSON-RPC body
    #: — it asks what a server does with a bad message. This asks what a server
    #: does with a request that is not a message-carrying POST at all: a GET, a
    #: DELETE, a body under an unacceptable content type. Those questions belong
    #: to the Streamable HTTP binding rather than to JSON-RPC, and a step kind
    #: that could not express them would have forced every one of them to be
    #: written as a special case somewhere else.
    HTTP_REQUEST = "http_request"


class AssertionKind(StrEnum):
    RESPONSE_IS_RESULT = "response_is_result"
    RESPONSE_IS_ERROR = "response_is_error"
    ERROR_CODE_EQUALS = "error_code_equals"
    ERROR_CODE_IN = "error_code_in"
    RESPONSE_ID_MATCHES = "response_id_matches"
    RESULT_HAS_FIELD = "result_has_field"
    RESULT_FIELD_TYPE = "result_field_type"
    RESULT_FIELD_EQUALS = "result_field_equals"
    VALID_JSON = "valid_json"
    NO_RESPONSE = "no_response"
    #: The HTTP status is one of a permitted set.
    #:
    #: A set rather than a value because the Streamable HTTP binding genuinely
    #: permits alternatives in several places — a GET may be answered with an
    #: event stream or with Method Not Allowed, and both are conforming. An
    #: assertion that could only name one would have to pick a winner the
    #: specification does not.
    HTTP_STATUS_IN = "http_status_in"
    #: No JSON-RPC *response object* was correlated to this step.
    #:
    #: Deliberately not ``NO_RESPONSE``, which passes only when nothing arrived
    #: at all. Over HTTP something always arrives: a notification is answered at
    #: the transport level, and an HTTP 202 with an empty body is exactly the
    #: conforming outcome. Grading that with ``NO_RESPONSE`` would report a
    #: correct server as having answered a notification it did not answer.
    NO_JSONRPC_RESPONSE = "no_jsonrpc_response"


class AssertionSpec(BaseModel):
    """One checkable claim about the target's answer to a step."""

    model_config = ConfigDict(extra="forbid")

    assertion_id: str
    kind: AssertionKind
    #: Dotted path within `result`, for field assertions.
    path: str | None = None
    expected: Any = None
    expected_codes: list[int] = Field(default_factory=list)
    #: Permitted HTTP statuses, for ``HTTP_STATUS_IN``.
    expected_statuses: list[int] = Field(default_factory=list)
    expected_type: str | None = None
    #: The dated requirement this assertion enforces. Required for normative claims.
    normative_source: str | None = None
    #: A failing assertion maps to this classification.
    #:
    #: The *outcome* only. Whether the failure is security-relevant is declared
    #: below and travels separately, so an assertion can be both — which is the
    #: case this field alone could not express.
    on_fail: str = "SPEC_VIOLATION"
    description: str = ""
    #: Whether failing this assertion is security-relevant.
    #:
    #: Independent of ``on_fail``. An assertion quoting a MUST about Origin
    #: validation fails as a ``SPEC_VIOLATION`` *and* is security-relevant; those
    #: are two facts about one event and recording only one of them loses the
    #: other.
    security_relevant: bool = False
    #: Which property, when the assertion is security-relevant. Falls back to the
    #: scenario's declaration.
    security_category: SecurityCategory | None = None
    #: What a security reading of this assertion's failure does not establish.
    #: Falls back to the scenario's, then to the harness default.
    security_limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _a_normative_assertion_does_not_collapse_into_a_security_label(self) -> AssertionSpec:
        """Refuse the mapping that lost a normative failure to say it was scary.

        ``SECURITY_RELEVANT`` is retained in the classification vocabulary so
        artifacts written before the dimensions existed still read as what they
        were. It is not available to an assertion that cites a dated requirement:
        such an assertion's failure *is* a specification violation, and mapping it
        to a security label discards the conformance fact in exchange for a word
        that makes no claim about conformance at all.

        The security reading is not lost by this rule — it moves to
        ``security_relevant`` below, where it no longer has to displace anything.
        """
        if self.on_fail != Classification.SECURITY_RELEVANT.value:
            return self
        if self.normative_source:
            raise ValueError(
                f"assertion {self.assertion_id!r} cites the normative source "
                f"{self.normative_source!r} and maps its failure to "
                "SECURITY_RELEVANT. A failed normative assertion is a "
                "SPEC_VIOLATION; set on_fail accordingly and declare "
                "security_relevant=True so the result carries both facts."
            )
        return self

    @model_validator(mode="after")
    def _a_security_category_belongs_to_a_security_assertion(self) -> AssertionSpec:
        if self.security_category is not None and not self.security_relevant:
            raise ValueError(
                f"assertion {self.assertion_id!r} names a security category without "
                "declaring security_relevant. One of the two is wrong."
            )
        return self


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    kind: StepKind
    method: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    raw_payload: str | None = None
    timeout_s: float | None = None
    #: Whether the minimiser is permitted to delete this step.
    optional: bool = True
    assertions: list[AssertionSpec] = Field(default_factory=list)
    #: A step may tolerate a timeout when the scenario is about timing.
    timeout_is_expected: bool = False
    #: Fill this step's params from the target's approved safe invocation.
    #:
    #: The alternative was a scenario per target, each hard-coding one server's
    #: tool name and argument shape. That multiplies the battery by the matrix and
    #: makes "the tool-call path works" a different scenario for every
    #: implementation, which is precisely the comparison the project exists to
    #: make. The executor substitutes the contract before execution, so the trace,
    #: the compiled plan and the reproduction all carry concrete arguments.
    use_safe_tool_invocation: bool = False
    #: HTTP transports only. Headers are normally derived from the body so they
    #: always agree with it; a disagreement must be requested explicitly here so
    #: a mismatch can never be introduced by accident.
    http_header_overrides: dict[str, str] = Field(default_factory=dict)
    #: Headers to *remove* from the derived set, for ``HTTP_REQUEST`` steps.
    #:
    #: Separate from an override to the empty string, because those are different
    #: requests: a header absent from the wire and a header present with an empty
    #: value are distinguishable, and a scenario about what a server does when a
    #: header is missing has to be able to say which one it means.
    http_header_removals: list[str] = Field(default_factory=list)
    #: For ``HTTP_REQUEST``: the method, the path, and an optional literal body.
    #: Defaults keep an ``HTTP_REQUEST`` step that sets none of them a plain
    #: bodyless GET of the endpoint.
    http_method: str = "GET"
    http_path: str | None = None
    http_body_text: str | None = None
    #: Stop after the status line and headers rather than reading the body.
    #:
    #: The 2025 binding lets a server answer a GET with an open event stream that
    #: never completes. A probe that read to completion would hang on a
    #: conforming server, and the header check is the whole assertion anyway —
    #: nothing here claims to consume a stream.
    http_abort_after_headers: bool = False
    #: Send the session identifier the setup handshake captured, when one exists.
    #: Off by default so a scenario about a *missing* or *stale* identifier is
    #: written by asking for it rather than by remembering to suppress it.
    http_send_session_id: bool = True
    #: Which of the above the minimiser may delete. Empty by default, because an
    #: override is normally the *condition under test*: removing the header that
    #: creates a mismatch leaves a scenario whose assertions still fail, but for
    #: an entirely different reason. Signature preservation alone does not catch
    #: that, so constitutive elements are protected by construction.
    reducible_headers: list[str] = Field(default_factory=list)


class Scenario(BaseModel):
    """A definition plus the executable step list and faults."""

    model_config = ConfigDict(extra="forbid")

    definition: ScenarioDefinition
    steps: list[Step]
    faults: list[Fault] = Field(default_factory=list)
    #: Default per-step deadline; individual steps may tighten it.
    timeout_s: float = 10.0
    #: Skip the profile's required handshake before the scenario traffic.
    #:
    #: For exactly one class of scenario: the ones asking what a server does
    #: *before* its lifecycle is established. There is no way to ask that
    #: question while the harness is performing the handshake first, and a
    #: scenario that tried would measure the state after initialization while
    #: claiming to measure the state before it.
    #:
    #: Never a convenience. A scenario setting this is asserting about an
    #: uninitialized server and has to say so in its expected outcome.
    suppress_profile_handshake: bool = False

    @property
    def scenario_id(self) -> str:
        return self.definition.scenario_id

    def with_steps(self, steps: list[Step]) -> Scenario:
        return self.model_copy(update={"steps": steps})

    def with_faults(self, faults: list[Fault]) -> Scenario:
        return self.model_copy(update={"faults": faults})
