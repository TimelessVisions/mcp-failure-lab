"""Core typed domain model.

Everything the harness persists is versioned and validated. Unknown fields are
rejected so a stale configuration fails loudly rather than being half-applied.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from interop_lab.artifacts.runtime import RuntimeRecord
from interop_lab.capabilities.invocation import SafeToolInvocation
from interop_lab.capabilities.model import ResolvedTargetCapabilities
from interop_lab.containment.model import (
    ContainerSpec,
    ContainmentRecord,
    ExecutionMode,
    NetworkMode,
    Provenance,
)
from interop_lab.models.dimensions import (
    DEFAULT_SECURITY_LIMITATIONS,
    ResultDimensions,
    SecurityCategory,
    VulnerabilityStatus,
)
from interop_lab.models.severity import Severity
from interop_lab.naming import ARTIFACT_SCHEMA_VERSION, HARNESS_VERSION
from interop_lab.profiles.registry import Role, Transport

__all__ = [
    "DEFAULT_SECURITY_LIMITATIONS",
    "ResultDimensions",
    "SecurityCategory",
    "Severity",
    "VulnerabilityStatus",
]


class Classification(StrEnum):
    """What the target *did*. One dimension, and deliberately only one.

    This enum answers a single question: how did the execution come out? Whether
    the outcome is security-relevant, whether anyone has established
    exploitability, and how severe it is are separate questions answered by
    :class:`~interop_lab.models.dimensions.ResultDimensions`, which travels
    beside this value rather than competing with it for the same field.

    A divergence is not automatically a defect; a security-relevant behaviour is
    not automatically a vulnerability.
    """

    PASS = "PASS"  # noqa: S105 - a classification name, not a credential
    SPEC_VIOLATION = "SPEC_VIOLATION"
    INVALID_WIRE_MESSAGE = "INVALID_WIRE_MESSAGE"
    CRASH = "CRASH"
    HANG = "HANG"
    TIMEOUT = "TIMEOUT"
    RECOVERY_FAILURE = "RECOVERY_FAILURE"
    CLEANUP_FAILURE = "CLEANUP_FAILURE"
    #: Retained so artifacts and baselines written before the dimension split
    #: still load and still read as what they were.
    #:
    #: Not available to a new normative assertion, and the validator on
    #: :class:`~interop_lab.scenarios.model.AssertionSpec` refuses one that tries.
    #: Using it there was the original error: an assertion quoting a MUST that
    #: maps its failure to this value throws away the fact that a dated
    #: requirement was violated, in exchange for a label that says nothing about
    #: conformance. Security relevance is now recorded alongside the outcome
    #: instead of instead of it.
    SECURITY_RELEVANT = "SECURITY_RELEVANT"
    BEHAVIORAL_DIVERGENCE = "BEHAVIORAL_DIVERGENCE"
    EXPECTED_DIFFERENCE = "EXPECTED_DIFFERENCE"
    HARNESS_ERROR = "HARNESS_ERROR"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


#: Classifications that mean "the target did something it should not have".
FAILING = frozenset(
    {
        Classification.SPEC_VIOLATION,
        Classification.INVALID_WIRE_MESSAGE,
        Classification.CRASH,
        Classification.HANG,
        Classification.TIMEOUT,
        Classification.RECOVERY_FAILURE,
        Classification.CLEANUP_FAILURE,
    }
)


class TimeoutCause(StrEnum):
    """A timeout is meaningless without saying whose deadline expired."""

    HARNESS_DEADLINE = "harness_deadline"
    TRANSPORT_DEADLINE = "transport_deadline"
    TARGET_NO_RESPONSE = "target_no_response"


class ScenarioCategory(StrEnum):
    BASELINE = "baseline"
    WIRE_INTEGRITY = "wire_integrity"
    VERSION_HEADER = "version_header"
    STATEFUL_LIFECYCLE = "stateful_lifecycle"
    STATELESS_LIFECYCLE = "stateless_lifecycle"
    TIMING_CANCELLATION = "timing_cancellation"
    PROCESS_RECOVERY = "process_recovery"
    SCHEMA_DRIFT = "schema_drift"
    HTTP_AUTHZ = "http_authz"
    HOSTILE_OUTPUT = "hostile_output"


class RuntimeKind(StrEnum):
    """What actually executes the server inside the image.

    `STATIC_BINARY` exists because a `scratch` image has no language runtime to
    observe. Recording the Go compiler version as a "runtime version" there
    would state, as a fact about the running container, something that is only
    true of the machine that built it.
    """

    PYTHON = "python"
    NODE = "node"
    STATIC_BINARY = "static_binary"
    OTHER = "other"


class BuildToolchain(BaseModel):
    """What produced the artifact. Not what runs it.

    Conflating the two is how `go1.25.5` ends up recorded as the runtime of an
    image that contains no Go runtime at all. Both facts matter and neither
    substitutes for the other, so they are recorded in separate places.
    """

    model_config = ConfigDict(extra="forbid")

    python: str | None = None
    node: str | None = None
    npm: str | None = None
    go: str | None = None
    #: The build stage's base image, pinned by digest.
    base_image: str | None = None


class RuntimeIdentity(BaseModel):
    """What executes the server, measured from the image that will run."""

    model_config = ConfigDict(extra="forbid")

    kind: RuntimeKind
    #: The version string as the runtime itself reports it, or a description of
    #: why there is no version to report.
    detail: str | None = None
    #: The runtime stage's base image, pinned by digest. `scratch` is a legitimate
    #: value and means exactly what it says.
    base_image: str | None = None


class ProjectRelationship(StrEnum):
    """Whether a target comes from the organisation that publishes the spec.

    Declared, never inferred. An earlier version of the evidence manifest
    guessed this by searching the target's prose for the word "independent" —
    and promptly labelled the official TypeScript server independent, because
    its notes say "it is not the independent slot in the matrix". A matrix whose
    central claim is "one official and one independent implementation" cannot
    rest on a substring search.
    """

    #: From the modelcontextprotocol organisation.
    OFFICIAL = "official"
    #: A separate organisation, author and codebase.
    INDEPENDENT = "independent"
    #: Written in this repository.
    FIRST_PARTY = "first_party"


class SupplyChain(BaseModel):
    """Pinned identity of what the image was built from.

    Every field is a hash someone can recompute. Recorded on the target rather
    than derived from the build context, because the artifacts differ by
    ecosystem: an npm target has a lockfile in its context, a Go target's
    closure lives inside a module zip that never lands there, and a Python
    target pins wheels by hash in a requirements file.
    """

    model_config = ConfigDict(extra="forbid")

    #: Upstream source archive — npm tarball, module zip, sdist.
    source_archive_sha256: str | None = None
    #: The dependency closure: package-lock.json, go.sum, requirements.lock.txt.
    lock_sha256: str | None = None
    #: Ecosystem-native closure identity, where one exists (Go module sums).
    module_sum: str | None = None
    go_mod_sum: str | None = None


class ToolDeclarationPolicy(StrEnum):
    """What a disagreement between declared and advertised tools should do.

    The disagreement is always *recorded*; this only decides whether it is worth
    interrupting anyone. Neither side is ever overwritten by the other: the
    declaration is what a human expected, the observation is what the target
    said, and silently reconciling them would destroy the only signal that an
    upstream release changed its tool surface.
    """

    #: Record it and move on. For a target deliberately declaring nothing.
    IGNORE = "ignore"
    #: Record it and surface it. The default: a drifted declaration is usually a
    #: stale target file, which is worth knowing and is not worth a failed run.
    WARN = "warn"
    #: Treat it as a configuration error. For a target whose tool surface is part
    #: of what is being pinned.
    FAIL = "fail"


class ScenarioNature(StrEnum):
    NORMATIVE = "normative"
    DIFFERENTIAL = "differential"
    RESILIENCE = "resilience"
    SECURITY_RELEVANT = "security_relevant"
    #: Machinery, not a test. Carries no assertions, can produce no verdict about
    #: a target, and never appears in the battery. Capability discovery is the
    #: only user: it needs a scenario's shape so it can be executed by the same
    #: strategy the battery uses, but labelling it `normative` would put a
    #: requirement-checking claim on an exchange that checks nothing.
    INFORMATIONAL = "informational"


# --------------------------------------------------------------------- targets


class TargetDefinition(BaseModel):
    """A pinned, launchable implementation under test."""

    model_config = ConfigDict(extra="forbid")

    target_id: str
    role: Role
    implementation: str
    language: str
    version: str = Field(description="Package release or fixture version. Must be exact.")
    commit_sha: str | None = None
    runtime_version: str | None = None

    launch_command: list[str] = Field(
        description=(
            "Argv for the target. May contain the documented ${PYTHON} token, which "
            "resolves to the interpreter running the harness. A literal interpreter "
            "path is rejected: it is platform-specific and breaks portable artifacts."
        )
    )
    #: Distribution name used to observe the actually-installed version.
    package_name: str | None = None
    working_dir: str | None = None
    #: Only these environment names are forwarded; everything else is scrubbed.
    env_allowlist: list[str] = Field(default_factory=list)
    #: Explicit values injected after scrubbing. Never secrets.
    env: dict[str, str] = Field(default_factory=dict)

    transport: Transport
    protocol_profile: str
    timeout_s: float = 10.0

    #: Who wrote the code. Decides whether host execution is defensible at all.
    provenance: Provenance = Provenance.LOCAL_FIXTURE
    #: Where it runs. There is no `auto`: a mode that degrades to host execution
    #: when Docker is missing would make the same artifact mean two things.
    execution_mode: ExecutionMode = ExecutionMode.HOST
    container: ContainerSpec | None = None
    #: What the target is *expected* to advertise. An expectation, not an
    #: inventory, and never the basis of an applicability decision — that comes
    #: from `interop_lab.capabilities`, observed from the target itself. Leaving
    #: this empty is legitimate and means "no expectation recorded"; it does not
    #: mean the target has no tools.
    expected_tools: list[str] = Field(default_factory=list)
    #: What to do when the expectation above and the observed surface disagree.
    tool_declaration_policy: ToolDeclarationPolicy = ToolDeclarationPolicy.WARN
    #: Official, independent, or written here. Declared because the matrix's
    #: central claim depends on it and a guess would be worthless.
    relationship: ProjectRelationship = ProjectRelationship.FIRST_PARTY
    #: Hashes of what the image was built from.
    supply_chain: SupplyChain | None = None
    #: Whether this target may fall back to `expected_tools` when capability
    #: discovery cannot complete. False everywhere by default, and refused for
    #: external targets by the validator below.
    #:
    #: The only legitimate use is a deliberately broken local fixture whose
    #: defect prevents a handshake at all — one that resets every connection, so
    #: no discovery could ever succeed against it. Those fixtures exist to prove
    #: the harness detects failures, and blocking them on an impossible
    #: precondition would delete the coverage that proves detection works.
    allow_declared_tool_fallback: bool = False
    #: One reviewed, safe call this target may be asked to perform.
    #:
    #: An operator-approved fixture, never an observation. The tool-call baseline
    #: was tied to a literal `echo` taking `{"text": ...}`, which is a fixture's
    #: surface and matches none of the external matrix — so the scenario that
    #: exercises the primary tool-invocation path was NOT_APPLICABLE against every
    #: third-party target this project measures.
    #:
    #: Synthesising the call from whatever the target advertises would fix that
    #: and is refused: a tool surface is untrusted input, and generating a call
    #: from it means deciding at run time, from a document the target wrote, to
    #: invoke a named operation with invented arguments.
    #:
    #: Declaring one here does not make the tool exist. Observed capabilities
    #: remain the source of applicability truth: the tool must be advertised and
    #: its input schema must still hash to the value below.
    safe_tool_invocation: SafeToolInvocation | None = None
    #: What built the image, kept apart from what runs inside it.
    build_toolchain: BuildToolchain | None = None
    #: What executes the server inside the image.
    runtime: RuntimeIdentity | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _reject_absolute_launch_paths(self) -> TargetDefinition:
        """A launch command must survive being read on another machine."""
        from interop_lab.artifacts.portability import EXECUTABLE_TOKENS, violations_in_text

        for part in self.launch_command:
            if any(token in part for token in EXECUTABLE_TOKENS):
                continue
            problems = violations_in_text(part, context="launch_command")
            if problems:
                raise ValueError(
                    f"target {self.target_id!r} has a non-portable launch command "
                    f"({problems[0]}); use the ${{PYTHON}} token or a relative path"
                )
        return self

    @model_validator(mode="after")
    def _only_local_fixtures_may_fall_back_to_a_declaration(self) -> TargetDefinition:
        """Third-party tool surfaces are observed, never taken on trust.

        The fallback exists for fixtures this repository wrote, whose defects are
        deliberate and known. Allowing it for external code would restore exactly
        the defect observed capability resolution was built to remove: a
        handwritten list deciding which requirements apply to someone else's
        server.
        """
        if self.allow_declared_tool_fallback and self.provenance is Provenance.EXTERNAL:
            raise ValueError(
                f"target {self.target_id!r} is external and sets "
                "allow_declared_tool_fallback. A third-party target's tool surface is "
                "observed or the scenario is INCONCLUSIVE; it is never read off a "
                "list someone typed."
            )
        return self

    @model_validator(mode="after")
    def _external_code_must_be_contained(self) -> TargetDefinition:
        """Third-party code does not run on this host.

        The rule is deliberately in the model rather than in the runner: a
        target file that would run someone else's server as a bare local
        process must fail to load, not fail later when someone remembers to
        check.
        """
        if (
            self.provenance is Provenance.EXTERNAL
            and self.execution_mode is not ExecutionMode.CONTAINER
        ):
            raise ValueError(
                f"target {self.target_id!r} declares provenance=external but "
                f"execution_mode={self.execution_mode.value}. Code this project did "
                "not write is run in a container or not at all."
            )
        if self.execution_mode is ExecutionMode.CONTAINER and self.container is None:
            raise ValueError(
                f"target {self.target_id!r} requests container execution without a "
                "container spec; there is nothing to enforce"
            )
        if self.execution_mode is ExecutionMode.HOST and self.container is not None:
            raise ValueError(
                f"target {self.target_id!r} carries a container spec but runs on the "
                "host. A spec that is not enforced reads as containment that exists."
            )
        return self

    @model_validator(mode="after")
    def _contained_http_publishes_a_port(self) -> TargetDefinition:
        spec = self.container
        if spec is None:
            return self
        needs_port = self.transport is Transport.STREAMABLE_HTTP
        http_modes = {NetworkMode.ISOLATED_SIDECAR, NetworkMode.PUBLISHED_LOOPBACK}
        if needs_port and spec.network not in http_modes:
            raise ValueError(
                f"target {self.target_id!r} uses Streamable HTTP but requests "
                f"network={spec.network.value}; nothing could open a socket to it. "
                "Use isolated_sidecar (preferred) or published_loopback."
            )
        if not needs_port and spec.network is not NetworkMode.NONE:
            raise ValueError(
                f"target {self.target_id!r} uses {self.transport.value} but requests "
                f"network={spec.network.value}; stdio needs no network at all"
            )
        return self

    @model_validator(mode="after")
    def _reject_moving_version(self) -> TargetDefinition:
        moving = {"main", "master", "latest", "HEAD", "*"}
        if self.version.strip() in moving:
            raise ValueError(
                f"target {self.target_id!r} pins a moving version {self.version!r}; "
                "reproducibility requires an exact release or commit SHA"
            )
        return self


# ------------------------------------------------------------------- scenarios


class Applicability(BaseModel):
    """Gate deciding whether a scenario may run against a target at all."""

    model_config = ConfigDict(extra="forbid")

    min_spec_version: str | None = None
    max_spec_version: str | None = None
    profiles: list[str] = Field(default_factory=list)
    transports: list[Transport] = Field(default_factory=list)
    roles: list[Role] = Field(default_factory=list)
    requires_tools: list[str] = Field(default_factory=list)
    #: Observed transport behaviour this scenario needs, as
    #: ``ObservedTransportFeatures`` field name → required value.
    #:
    #: The whole point is that these are *observed*. A target file declaring
    #: "this server issues session identifiers" would be the same architectural
    #: error as `expected_tools` deciding applicability: a human's claim standing
    #: in for the server's behaviour. The session lifecycle scenarios ask for
    #: ``session_identifier_issued: true`` and are skipped, with the observed
    #: reason, against a server that assigns none — which 2025-11-25 permits.
    requires_transport_features: dict[str, Any] = Field(default_factory=dict)
    #: Whether the scenario needs the target's approved safe invocation.
    #:
    #: A separate gate from ``requires_tools`` because it asks a different
    #: question. ``requires_tools`` names a tool the scenario knows about; this
    #: says the scenario needs *whichever* tool an operator approved for this
    #: target, and is answered by the executor, which can see both the contract
    #: and the observed surface.
    requires_safe_tool_invocation: bool = False

    def reason_not_applicable(
        self,
        *,
        spec_version: str,
        profile_id: str,
        transport: Transport,
        role: Role,
        available_tools: list[str],
    ) -> str | None:
        """Return why this scenario cannot run, or ``None`` if it can.

        ``available_tools`` must be what the target *advertised*, not what its
        file declared. Passing a declaration here is how a scenario gets skipped
        against a server that could run it, or run against a tool that does not
        exist — see ``interop_lab.capabilities``.
        """
        declarative = self.reason_not_applicable_declaratively(
            spec_version=spec_version, profile_id=profile_id, transport=transport, role=role
        )
        if declarative is not None:
            return declarative
        missing = [t for t in self.requires_tools if t not in available_tools]
        if missing:
            return (
                f"target does not advertise required tools {missing} "
                f"(advertised: {sorted(available_tools)})"
            )
        return None

    def reason_not_applicable_declaratively(
        self,
        *,
        spec_version: str,
        profile_id: str,
        transport: Transport,
        role: Role,
    ) -> str | None:
        """The half of the gate that needs no traffic with the target.

        Split out so the executor can decide these before starting a container:
        a scenario restricted to another transport cannot become applicable no
        matter what the target advertises, and paying for a discovery run to
        learn that would be wasted work on every target in the matrix.
        """
        if self.profiles and profile_id not in self.profiles:
            return f"scenario restricted to profiles {self.profiles}, target is {profile_id}"
        if self.min_spec_version and spec_version < self.min_spec_version:
            return f"requires spec >= {self.min_spec_version}, target is {spec_version}"
        if self.max_spec_version and spec_version > self.max_spec_version:
            return f"requires spec <= {self.max_spec_version}, target is {spec_version}"
        if self.transports and transport not in self.transports:
            return f"scenario restricted to transports {self.transports}, target uses {transport}"
        if self.roles and role not in self.roles:
            return f"scenario restricted to roles {self.roles}, target is {role}"
        return None


class ScenarioDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    version: str = "1"
    title: str
    category: ScenarioCategory
    nature: ScenarioNature
    purpose: str
    applicability: Applicability = Field(default_factory=Applicability)
    severity: Severity = Severity.MEDIUM
    #: What a conforming target is expected to do, in prose, for the report.
    expected_outcome: str = ""
    #: Normative citation: which dated requirement backs the assertion.
    normative_source: str | None = None
    rationale: str | None = None
    #: Which security property this scenario is about, when it is about one.
    #:
    #: Declared here rather than inferred from ``nature``, because ``nature`` says
    #: what kind of scenario this is and this says what a non-pass would *mean*.
    #: A scenario carrying one produces security-relevant results when it fails,
    #: on a dimension separate from its primary classification — so a failed
    #: normative assertion stays a SPEC_VIOLATION and is also counted as a
    #: security-relevant observation, which the single-enum design could not do.
    security_category: SecurityCategory | None = None
    #: What a security reading of this scenario's results does not establish.
    #: Falls back to :data:`DEFAULT_SECURITY_LIMITATIONS` when left empty.
    security_limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _a_security_nature_names_the_property(self) -> ScenarioDefinition:
        """A scenario calling itself security-relevant has to say about what.

        Without this, ``nature=SECURITY_RELEVANT`` is a label with no consequence:
        the results it produces could not be grouped, counted, or compared against
        another target's results for the same property.
        """
        if self.nature is ScenarioNature.SECURITY_RELEVANT and self.security_category is None:
            raise ValueError(
                f"scenario {self.scenario_id!r} declares nature=security_relevant and "
                "no security_category, so nothing it produces could be counted "
                "alongside another result about the same property"
            )
        return self


# ------------------------------------------------------------------ assertions


class AssertionStatus(StrEnum):
    PASS = "pass"  # noqa: S105 - an assertion status, not a credential
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    NOT_APPLICABLE = "not_applicable"


class AssertionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assertion_id: str
    status: AssertionStatus
    expected: str
    actual: str
    #: Sequence numbers of the wire events that justify this result.
    evidence_seq: list[int] = Field(default_factory=list)
    normative_source: str | None = None
    confidence: Literal["high", "medium", "low"] = "high"
    classification: Classification | None = None
    detail: str | None = None
    #: Whether failing *this* assertion is security-relevant, carried from the
    #: spec so the run's dimensions can be derived from what actually failed
    #: rather than from the scenario as a whole. A scenario may hold one
    #: assertion about a security property and several about message shape.
    security_relevant: bool = False
    security_category: SecurityCategory | None = None


# ------------------------------------------------------------------------ runs


class RunStatus(StrEnum):
    COMPLETED = "completed"
    HARNESS_ERROR = "harness_error"
    NOT_APPLICABLE = "not_applicable"


class ProcessOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exit_code: int | None = None
    signal: int | None = None
    killed_by_harness: bool = False
    stderr_bytes: int = 0
    cleanup_ok: bool = True
    cleanup_detail: str | None = None


class RunResult(BaseModel):
    """One scenario executed once against one target."""

    model_config = ConfigDict(extra="forbid")

    artifact_schema_version: str = ARTIFACT_SCHEMA_VERSION
    harness_version: str = HARNESS_VERSION

    run_id: str
    scenario_id: str
    scenario_version: str
    target_id: str
    protocol_profile: str
    spec_version: str
    transport: Transport
    seed: int

    started_at: str
    finished_at: str
    duration_ms: float

    status: RunStatus = RunStatus.COMPLETED
    classification: Classification = Classification.INCONCLUSIVE
    #: The secondary dimensions, recorded beside the classification rather than
    #: competing with it for the same field. Filled by
    #: :func:`interop_lab.results.derive.derive_dimensions` on every executed run.
    dimensions: ResultDimensions = Field(default_factory=ResultDimensions)
    timeout_cause: TimeoutCause | None = None
    assertions: list[AssertionResult] = Field(default_factory=list)
    process: ProcessOutcome = Field(default_factory=ProcessOutcome)

    not_applicable_reason: str | None = None
    harness_error: str | None = None

    #: Which execution strategy produced this evidence — a live interactive
    #: session, or a compiled plan run inside a probe sidecar. Recorded because
    #: two runs of the same scenario against the same target can be gathered
    #: through genuinely different paths, and a reader must not have to infer
    #: which one from the transport and the containment record.
    execution_strategy: str | None = None
    #: Fully resolved target, embedded so the artifact does not depend on the YAML.
    resolved_target: TargetDefinition | None = None
    faults_applied: list[str] = Field(default_factory=list)
    #: Artifact URI (``artifact://runs/<run-id>/wire.jsonl``), never an absolute
    #: filesystem path — a committed artifact must resolve on another machine.
    trace_path: str | None = None
    #: Declared versus observed runtime, with any disagreement recorded.
    runtime: RuntimeRecord | None = None
    #: What isolation this run actually had. Requested and observed, side by side.
    containment: ContainmentRecord | None = None
    #: What the target advertised, observed from real traffic under the same
    #: containment. This is what decided any tool prerequisite, so it is carried
    #: with the result: an applicability decision whose justification is not in
    #: the artifact cannot be re-checked later.
    resolved_capabilities: ResolvedTargetCapabilities | None = None
    environment_warnings: list[str] = Field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.classification in FAILING

    @property
    def is_usable_evidence(self) -> bool:
        """Whether this run may be cited as evidence about the target.

        A run whose containment was requested but not obtained says nothing
        about the target: the harness did not execute what it claims to have
        executed.
        """
        return self.containment is None or self.containment.is_usable_evidence


# -------------------------------------------------------------------- findings


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_schema_version: str = ARTIFACT_SCHEMA_VERSION
    finding_id: str
    classification: Classification
    title: str
    target_id: str
    target_version: str
    scenario_id: str
    protocol_profile: str
    severity: Severity
    #: The secondary dimensions this finding records.
    #:
    #: A finding is where ``vulnerability_status`` may legitimately exceed
    #: ``unconfirmed``: a run records what was observed, and only a human working
    #: a result up — establishing that a real deployment can be driven this way —
    #: can move it to ``confirmed_private``. Nothing in the execution path writes
    #: that value.
    dimensions: ResultDimensions = Field(default_factory=ResultDimensions)

    fact: str
    normative_basis: str | None
    interpretation: str
    limitation: str
    reproduction: str

    first_divergent_seq: int | None = None
    evidence_paths: list[str] = Field(default_factory=list)
    reproduction_status: Literal["unverified", "stable", "unstable"] = "unverified"
    minimization_status: Literal["none", "minimized", "failed"] = "none"
    #: Third-party findings stay private until explicitly approved for disclosure.
    public_safety: Literal["private", "approved_for_disclosure"] = "private"


class DifferentialVerdict(StrEnum):
    EQUIVALENT = "equivalent"
    EXPECTED_DIFFERENCE = "expected_difference"
    MEANINGFUL_DIVERGENCE = "meaningful_divergence"
    NORMATIVE_DIVERGENCE = "normative_divergence"
    INCOMPARABLE = "incomparable"


class TraceDifference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int
    field_path: str
    left: Any = None
    right: Any = None
    meaningful: bool
    explanation: str


class DifferentialResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_schema_version: str = ARTIFACT_SCHEMA_VERSION
    scenario_id: str
    left_target: str
    right_target: str
    left_classification: Classification
    right_classification: Classification

    verdict: DifferentialVerdict
    first_raw_difference_seq: int | None = None
    first_normalized_difference_seq: int | None = None
    differences: list[TraceDifference] = Field(default_factory=list)
    normative_interpretation: str = ""
    limitations: str = ""

    @property
    def meaningful_differences(self) -> list[TraceDifference]:
        return [d for d in self.differences if d.meaningful]

    @property
    def expected_differences(self) -> list[TraceDifference]:
        return [d for d in self.differences if not d.meaningful]
