"""The versioned probe-plan contract.

A plan is the entire instruction set handed to the contained probe. It is
deliberately a *transport* description: methods, paths, headers, bytes, timing
and generic response-driven state. No member of it names an MCP concept, and
the probe therefore cannot form an opinion about MCP — which is the property
that keeps the assertion engine single-implementation.

Two rules make the contract enforceable rather than decorative:

* Every model forbids unknown fields. A plan written by a newer host and read by
  an older probe fails loudly instead of running with the parts it recognised.
* ``PLAN_SCHEMA_VERSION`` is checked by the probe before anything else. A probe
  that silently ignored an unsupported step would report a run that never
  happened the way the plan asked.

The probe itself cannot import this module — it runs inside a minimal container
with no third-party packages and no access to this source tree. It re-implements
the same validation over the standard library, and
``tests/unit/test_probe_plan.py`` holds the two implementations to the same
contract so they cannot drift apart silently.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Bumped whenever the plan grows or changes a member. The probe refuses a plan
#: whose version it does not implement.
PLAN_SCHEMA_VERSION = "1"

#: Bumped when the probe's *behaviour* changes without the schema changing.
#: Recorded in the run artifact alongside the script hash, because "which probe
#: produced this evidence" is part of the evidence.
PROBE_VERSION = "2"


class ProbePhase(StrEnum):
    """Which part of the run a step belongs to.

    The distinction is load-bearing for fault attribution: occurrence indices
    count ``SCENARIO`` traffic only, so the same fault definition means the same
    thing against a profile that needs a handshake and one that does not.
    """

    #: Traffic required to reach the state the scenario is about. Never asserted
    #: on, never counted for fault occurrence.
    SETUP = "setup"
    #: The traffic under test.
    SCENARIO = "scenario"
    #: Traffic that returns the target to a clean state afterwards.
    CLEANUP = "cleanup"


class ProbeMethod(StrEnum):
    POST = "POST"
    GET = "GET"
    DELETE = "DELETE"


class ProbeBodyMode(StrEnum):
    """How a step's request body is produced.

    Separate modes rather than one "body" field, because the difference between
    "serialise this object" and "send exactly these bytes" is the difference
    between a conformance test and a wire-integrity test.
    """

    #: No body at all. ``Content-Length: 0``.
    NONE = "none"
    #: ``json_body`` serialised with a canonical separator set.
    JSON = "json"
    #: ``text_body`` encoded as UTF-8, byte for byte. Used for malformed and
    #: truncated payloads, where a JSON round-trip would repair the defect.
    TEXT = "text"
    #: ``base64_body`` decoded to raw bytes. The only way to send a payload that
    #: is not valid UTF-8.
    BASE64 = "base64"


class ProbeAbort(StrEnum):
    """How the probe abandons an exchange, to test what the target does then."""

    NONE = "none"
    #: Close immediately after writing the request, reading nothing.
    AFTER_REQUEST = "after_request"
    #: Read the status line and headers, then close without reading the body.
    AFTER_HEADERS = "after_headers"
    #: Read part of the body, then close. Best effort: a body that arrives in a
    #: single segment leaves nothing to abort part-way through, and the event
    #: records which of the two happened.
    AFTER_PARTIAL_BODY = "after_partial_body"


class ProbeCaptureSource(StrEnum):
    """Where a captured value comes from. All three are transport-level."""

    RESPONSE_HEADER = "response_header"
    JSON_POINTER = "json_pointer"
    STATUS = "status"


class ProbeSubstitutionTarget(StrEnum):
    """Where a previously captured value is injected on a later step."""

    HEADER = "header"
    PATH = "path"
    BODY = "body"


class ProbeVariable(BaseModel):
    """A declared slot in the probe's variable store.

    Declared up front so a capture or substitution naming an undeclared variable
    is a plan error rather than a silent no-op at run time.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    #: When true, the probe stops the plan if the variable is never captured.
    #: Not every profile issues a session id, so this is opt-in per plan.
    required: bool = False


class ProbeCapture(BaseModel):
    """Take one value out of a response and put it in the variable store."""

    model_config = ConfigDict(extra="forbid")

    variable: str
    source: ProbeCaptureSource
    #: Header name, for ``response_header``. Matched case-insensitively, since
    #: HTTP field names are case-insensitive and servers differ.
    header: str | None = None
    #: RFC 6901 JSON Pointer, for ``json_pointer``.
    pointer: str | None = None
    #: When true, a missing value fails the plan instead of leaving the variable
    #: unset.
    required: bool = False

    @model_validator(mode="after")
    def _source_carries_its_selector(self) -> ProbeCapture:
        if self.source is ProbeCaptureSource.RESPONSE_HEADER and not self.header:
            raise ValueError("capture from response_header requires `header`")
        if self.source is ProbeCaptureSource.JSON_POINTER and self.pointer is None:
            raise ValueError("capture from json_pointer requires `pointer`")
        if self.source is ProbeCaptureSource.STATUS and (self.header or self.pointer):
            raise ValueError("capture from status takes neither `header` nor `pointer`")
        return self


class ProbeSubstitution(BaseModel):
    """Inject a stored variable into a later request."""

    model_config = ConfigDict(extra="forbid")

    variable: str
    target: ProbeSubstitutionTarget
    #: Header name to set, for ``header``.
    header: str | None = None
    #: JSON Pointer to write, for ``body``. Parent objects must already exist;
    #: the probe does not invent structure, because guessing the shape of a
    #: message body is a protocol judgement.
    pointer: str | None = None
    #: Literal token replaced in the path, for ``path``.
    placeholder: str | None = None
    #: When true, the step fails if the variable was never captured. When false
    #: the substitution is skipped and the event records that it was.
    required: bool = False

    @model_validator(mode="after")
    def _target_carries_its_selector(self) -> ProbeSubstitution:
        if self.target is ProbeSubstitutionTarget.HEADER and not self.header:
            raise ValueError("substitution into a header requires `header`")
        if self.target is ProbeSubstitutionTarget.BODY and self.pointer is None:
            raise ValueError("substitution into a body requires `pointer`")
        if self.target is ProbeSubstitutionTarget.PATH and not self.placeholder:
            raise ValueError("substitution into a path requires `placeholder`")
        return self


class ProbeStep(BaseModel):
    """One HTTP exchange, described exactly."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    phase: ProbePhase
    method: ProbeMethod = ProbeMethod.POST
    #: Absolute request path, including any query string. Never a URL: the probe
    #: reaches the target through a fixed network alias it is given separately,
    #: so a plan can never carry a host-specific address.
    path: str = "/"
    headers: dict[str, str] = Field(default_factory=dict)

    body_mode: ProbeBodyMode = ProbeBodyMode.NONE
    json_body: dict[str, Any] | None = None
    text_body: str | None = None
    base64_body: str | None = None

    timeout_s: float = 10.0
    delay_before_ms: float = 0.0
    #: Split the request body in two and pause between the halves. Exercises a
    #: target's tolerance of a slow client without needing a second fixture.
    body_chunk_delay_ms: float = 0.0
    abort: ProbeAbort = ProbeAbort.NONE
    #: Bytes of response body read before the probe stops and marks the event
    #: truncated. A hostile target must not be able to exhaust the sidecar.
    response_body_limit_bytes: int = 256 * 1024

    #: Reuse the connection left open by the previous step, when there is one.
    reuse_connection: bool = False
    #: Leave the connection open for a later step to reuse.
    keep_connection_open: bool = False

    captures: list[ProbeCapture] = Field(default_factory=list)
    substitutions: list[ProbeSubstitution] = Field(default_factory=list)

    @model_validator(mode="after")
    def _body_mode_matches_the_body(self) -> ProbeStep:
        present = {
            ProbeBodyMode.JSON: self.json_body is not None,
            ProbeBodyMode.TEXT: self.text_body is not None,
            ProbeBodyMode.BASE64: self.base64_body is not None,
        }
        for mode, is_set in present.items():
            if self.body_mode is mode and not is_set:
                raise ValueError(
                    f"step {self.step_id!r} declares body_mode={mode.value} but the "
                    f"matching body member is unset"
                )
            if self.body_mode is not mode and is_set:
                raise ValueError(
                    f"step {self.step_id!r} sets a {mode.value} body but declares "
                    f"body_mode={self.body_mode.value}; two bodies would make the bytes "
                    "on the wire depend on field order"
                )
        return self

    @model_validator(mode="after")
    def _path_is_a_path(self) -> ProbeStep:
        if "://" in self.path:
            raise ValueError(
                f"step {self.step_id!r} path {self.path!r} looks like a URL. The probe is "
                "given its target separately; a plan carrying an address would not "
                "survive being replayed on another machine"
            )
        if not self.path.startswith("/"):
            raise ValueError(f"step {self.step_id!r} path {self.path!r} must be absolute")
        return self


class ProbeCleanup(BaseModel):
    """What the probe must do after the scenario traffic, and what it must leave."""

    model_config = ConfigDict(extra="forbid")

    #: Close every connection the probe still holds open.
    close_connections: bool = True
    #: Traffic that releases target-side state, e.g. a session teardown.
    steps: list[ProbeStep] = Field(default_factory=list)

    @model_validator(mode="after")
    def _cleanup_steps_are_cleanup_phase(self) -> ProbeCleanup:
        wrong = [s.step_id for s in self.steps if s.phase is not ProbePhase.CLEANUP]
        if wrong:
            raise ValueError(f"cleanup steps must carry phase=cleanup: {wrong}")
        return self


class ProbePlan(BaseModel):
    """Everything the contained probe is told, and nothing else."""

    model_config = ConfigDict(extra="forbid")

    plan_schema_version: str = PLAN_SCHEMA_VERSION
    #: The probe version the host compiled this plan for. The probe compares it
    #: against its own and records both.
    probe_version: str = PROBE_VERSION
    run_id: str
    #: Recorded so the artifact says which dated revision the plan was built
    #: for. The probe never reads it; it exists purely as provenance.
    protocol_profile: str

    #: Filled in by the sidecar at launch, from the network alias. A plan
    #: compiled on the host carries no address at all.
    base_url: str = ""

    timeout_s: float = 10.0
    ready_timeout_s: float = 30.0
    #: Hard caps on the output channel, enforced by the probe rather than
    #: trusted to the target's good behaviour.
    max_events: int = 512
    max_total_output_bytes: int = 4 * 1024 * 1024

    #: Send captured cookies back on later requests. Off unless a plan asks,
    #: because implicit cookie state would make a stateless run stateful without
    #: anything in the artifact saying so.
    retain_cookies: bool = False

    variables: list[ProbeVariable] = Field(default_factory=list)
    setup_steps: list[ProbeStep] = Field(default_factory=list)
    scenario_steps: list[ProbeStep] = Field(default_factory=list)
    cleanup: ProbeCleanup = Field(default_factory=ProbeCleanup)

    @model_validator(mode="after")
    def _phases_match_their_lists(self) -> ProbePlan:
        for step in self.setup_steps:
            if step.phase is not ProbePhase.SETUP:
                raise ValueError(f"setup step {step.step_id!r} carries phase={step.phase.value}")
        for step in self.scenario_steps:
            if step.phase is not ProbePhase.SCENARIO:
                raise ValueError(f"scenario step {step.step_id!r} carries phase={step.phase.value}")
        return self

    @model_validator(mode="after")
    def _every_variable_reference_is_declared(self) -> ProbePlan:
        """A capture or substitution naming an undeclared variable is a plan bug.

        Left unchecked it degrades into a silent no-op: the substitution simply
        never happens, the target answers something reasonable, and the artifact
        records a scenario that did not run as written.
        """
        declared = {variable.name for variable in self.variables}
        for step in self.all_steps():
            for capture in step.captures:
                if capture.variable not in declared:
                    raise ValueError(
                        f"step {step.step_id!r} captures into undeclared variable "
                        f"{capture.variable!r}; declare it in `variables`"
                    )
            for substitution in step.substitutions:
                if substitution.variable not in declared:
                    raise ValueError(
                        f"step {step.step_id!r} substitutes undeclared variable "
                        f"{substitution.variable!r}; declare it in `variables`"
                    )
        return self

    @model_validator(mode="after")
    def _step_ids_are_unique(self) -> ProbePlan:
        seen: set[str] = set()
        for step in self.all_steps():
            if step.step_id in seen:
                raise ValueError(f"duplicate step id {step.step_id!r}; events are keyed by it")
            seen.add(step.step_id)
        return self

    def all_steps(self) -> list[ProbeStep]:
        return [*self.setup_steps, *self.scenario_steps, *self.cleanup.steps]

    def to_json_dict(self) -> dict[str, Any]:
        """The exact document handed to the probe, and persisted in the artifact."""
        return self.model_dump(mode="json")
