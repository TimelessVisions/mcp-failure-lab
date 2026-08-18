"""The pinned benchmark definition, and the typed shape of every output.

Everything a result depends on is named in the definition rather than discovered
at run time: the harness commit, the target identities and fingerprints, the
scenario set, the profiles, the transports, the repetition counts, the allowed
exit codes, the timeout bounds and the artifacts each sample must produce. A
benchmark whose inputs are whatever happened to be on the machine is not a
benchmark, and the whole point of pinning them is that a disagreement between the
definition and the run is a *rejected sample* rather than a quiet difference.

Unknown fields are rejected everywhere. A definition carrying a key this code
does not understand is a definition someone edited against a different version,
and running it would measure something other than what it says.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from interop_lab.models.core import Classification, ResultDimensions
from interop_lab.naming import HARNESS_VERSION
from interop_lab.results.signature import ResultSignature

#: Schema version of the benchmark definition and its outputs.
BENCHMARK_SCHEMA_VERSION = "0.3"

BENCHMARK_NAME = "MCP Interoperability and Failure-Recovery Benchmark — Private 0.3"

#: Minimum independent repetitions before a correctness claim may be made.
#:
#: Three, and the number is a floor rather than a target. A result observed once
#: is not a claim; two agreeing observations cannot distinguish "stable" from
#: "coincidence" in a way anyone should rely on.
MIN_CORRECTNESS_REPETITIONS = 3

#: Minimum *measured* timing samples, excluding the warm-up.
#:
#: Five is enough to report a median with a stated range and nothing more. It is
#: emphatically not enough for a significance claim or a confidence interval, and
#: the reporting layer refuses to produce either — see ``reporting``.
MIN_TIMING_SAMPLES = 5


class BenchmarkCategory(StrEnum):
    """The fourteen categories results are reported under.

    Categories rather than one score. Each has its own applicable denominator and
    its own limitations, because "how did this target do on stateful lifecycle"
    and "how did it do on process containment" are different questions with
    different evidence behind them, and averaging them produces a number that
    answers neither.
    """

    ENVIRONMENT_AND_BUILD_IDENTITY = "environment_and_build_identity"
    VALID_BASELINE_BEHAVIOR = "valid_baseline_behavior"
    JSONRPC_REQUEST_INTEGRITY = "jsonrpc_request_integrity"
    STREAMABLE_HTTP_SEMANTICS = "streamable_http_semantics"
    STATEFUL_LIFECYCLE_BEHAVIOR = "stateful_lifecycle_behavior"
    TIMING_AND_RECOVERY = "timing_and_recovery"
    PROCESS_CONTAINMENT_AND_CLEANUP = "process_containment_and_cleanup"
    NORMATIVE_FAILURES = "normative_failures"
    BEHAVIORAL_DIVERGENCES = "behavioral_divergences"
    SECURITY_RELEVANT_OBSERVATIONS = "security_relevant_observations"
    APPLICABILITY_AND_NOT_MEASURED_SURFACE = "applicability_and_not_measured_surface"
    REPRODUCTION_AND_MINIMIZATION = "reproduction_and_minimization"
    CI_GATE_PERFORMANCE = "ci_gate_performance"
    TIME_TO_FIRST_ACTIONABLE_RESULT = "time_to_first_actionable_result"


class MeasurementState(StrEnum):
    """Why a cell in the report holds no result.

    Three separate things, and collapsing any two of them is how a benchmark
    starts lying. ``NOT_APPLICABLE`` means the scenario could not apply and is
    never counted in a passing denominator. ``NOT_MEASURED`` means nobody looked
    and is never counted as a failure. ``INCONCLUSIVE`` means someone looked and
    could not tell, which is a fact about this harness rather than about the
    target.
    """

    MEASURED = "measured"
    NOT_APPLICABLE = "not_applicable"
    NOT_MEASURED = "not_measured"
    INCONCLUSIVE = "inconclusive"
    PARTIALLY_MEASURED = "partially_measured"


class ConformanceBoundary(StrEnum):
    """How far the *official* conformance framework was run against a target.

    Recorded separately and never merged into a Failure Lab classification. The
    official framework's outcome is a statement by that framework; folding it into
    this vocabulary would attribute its verdict to this project.
    """

    MEASURED = "measured"
    NOT_MEASURED = "not_measured"
    PARTIALLY_MEASURED = "partially_measured"


class SampleRejection(StrEnum):
    """Why a sample contributes to no median and no summary.

    A rejected sample is not a slower measurement or a worse one — it is a
    measurement of something other than what the definition describes, and
    averaging it in would silently move every number it touches.
    """

    UNEXPECTED_EXIT_CODE = "unexpected_exit_code"
    MISSING_ARTIFACT = "missing_artifact"
    TARGET_FINGERPRINT_MISMATCH = "target_fingerprint_mismatch"
    SCENARIO_FINGERPRINT_MISMATCH = "scenario_fingerprint_mismatch"
    CAPABILITY_FINGERPRINT_MISSING = "capability_fingerprint_missing"
    CAPABILITY_FINGERPRINT_CHANGED = "capability_fingerprint_changed"
    RESULT_SIGNATURE_MISSING = "result_signature_missing"
    RESULT_SIGNATURE_UNSTABLE = "result_signature_unstable"
    CONTAINMENT_UNUSABLE = "containment_unusable"
    HARNESS_ERROR = "harness_error"
    CLEANUP_FAILED = "cleanup_failed"
    TRACKED_TREE_CHANGED = "tracked_tree_changed"
    TIMEOUT_BOUND_EXCEEDED = "timeout_bound_exceeded"


# ------------------------------------------------------------------ definition


class PinnedTarget(BaseModel):
    """One implementation under measurement, pinned by everything that decides it."""

    model_config = ConfigDict(extra="forbid")

    target_key: str
    target_id: str
    target_file: str
    language: str
    transport: str
    declared_profile: str
    relationship: str
    source_version: str
    source_commit: str | None = None
    #: Computed from the target definition. A run whose target fingerprints
    #: differently is measuring a different thing and its samples are rejected.
    target_fingerprint: str
    #: The portable half of image identity: what the image is built *from*.
    #: Reproducible on any machine, unlike the builder-local image ID.
    portable_identity_fingerprint: str | None = None
    build_context: str | None = None
    dockerfile_sha256: str | None = None
    lock_sha256: str | None = None
    source_archive_sha256: str | None = None
    #: How far the official conformance framework was run here.
    official_conformance: ConformanceBoundary = ConformanceBoundary.NOT_MEASURED
    official_conformance_detail: str | None = None


class PinnedScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    version: str
    category: BenchmarkCategory
    scenario_fingerprint: str
    #: Whether applicability rests on observing the target. Pinned so the report
    #: can say why a scenario was skipped without re-deriving it.
    requires_observed_surface: bool = False


class TimingBounds(BaseModel):
    """What a sample is allowed to take before it stops being a valid sample."""

    model_config = ConfigDict(extra="forbid")

    #: Beyond this, the measurement is of an environment problem rather than of
    #: the operation, and it is rejected instead of widening the range.
    per_sample_timeout_s: float = 900.0
    #: The whole benchmark. A bound so a wedged run fails rather than hangs.
    total_timeout_s: float = 7200.0


class EnvironmentRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid")

    docker_required: bool = True
    #: Every external target is built here; a run without a resolution measures
    #: nothing, because the identity of the image to run exists nowhere else.
    build_resolution_required: bool = True
    #: The tracked tree must be byte-identical to the commit under test, before
    #: and after. A benchmark that mutated its own inputs measured something the
    #: commit does not describe.
    tracked_tree_must_be_clean: bool = True
    minimum_python: str = "3.12"


class BenchmarkDefinition(BaseModel):
    """The pinned definition. Everything a result depends on is named here."""

    model_config = ConfigDict(extra="forbid")

    benchmark_schema_version: str = BENCHMARK_SCHEMA_VERSION
    benchmark_name: str = BENCHMARK_NAME
    harness_version: str = HARNESS_VERSION
    #: The commit the definition was written against. Recorded, and compared at
    #: run time: results from a different tree are results about a different
    #: harness.
    harness_commit: str | None = None

    #: Never published. Stated in the definition so it travels with every output.
    visibility: str = "private"

    targets: list[PinnedTarget] = Field(default_factory=list)
    scenarios: list[PinnedScenario] = Field(default_factory=list)
    profiles: list[str] = Field(default_factory=list)
    transports: list[str] = Field(default_factory=list)

    correctness_repetitions: int = MIN_CORRECTNESS_REPETITIONS
    #: Excluded from timing. One warm-up, always, because the first execution
    #: measures a cold page cache and a cold image layer rather than the operation.
    timing_warmup_runs: int = 1
    timing_measured_runs: int = MIN_TIMING_SAMPLES

    allowed_exit_codes: list[int] = Field(default_factory=lambda: [0, 1])
    expected_artifacts: list[str] = Field(default_factory=list)
    timing_bounds: TimingBounds = Field(default_factory=TimingBounds)
    environment: EnvironmentRequirements = Field(default_factory=EnvironmentRequirements)

    #: Complete result-signature equality, not classification equality. Settable
    #: only to true; the field exists so the requirement is visible in the
    #: definition rather than implied by the code.
    require_complete_result_signature: bool = True

    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _the_repetition_counts_support_the_claims_made_from_them(self) -> BenchmarkDefinition:
        if self.correctness_repetitions < MIN_CORRECTNESS_REPETITIONS:
            raise ValueError(
                f"correctness_repetitions is {self.correctness_repetitions}; a stability "
                f"claim rests on at least {MIN_CORRECTNESS_REPETITIONS} independent "
                "repetitions with equal result signatures"
            )
        if self.timing_measured_runs < MIN_TIMING_SAMPLES:
            raise ValueError(
                f"timing_measured_runs is {self.timing_measured_runs}; a median with a "
                f"stated range needs at least {MIN_TIMING_SAMPLES} measured samples"
            )
        if self.timing_warmup_runs < 1:
            raise ValueError(
                "timing_warmup_runs is 0. The first execution measures a cold cache "
                "rather than the operation, and including it moves every median."
            )
        if not self.require_complete_result_signature:
            raise ValueError(
                "require_complete_result_signature is false. Classification-only "
                "stability is what this benchmark version exists to replace: two runs "
                "can share a label while failing different assertions."
            )
        if self.visibility != "private":
            raise ValueError(
                f"visibility is {self.visibility!r}. These results compare third-party "
                "implementations and are not published."
            )
        return self


# --------------------------------------------------------------------- samples


class BenchmarkSample(BaseModel):
    """One execution, with everything needed to decide whether it counts."""

    model_config = ConfigDict(extra="forbid")

    sample_id: str
    category: BenchmarkCategory
    target_key: str | None = None
    scenario_id: str | None = None
    repetition: int
    #: Warm-up samples are executed and recorded, and excluded from every timing
    #: statistic. Recorded rather than discarded so the exclusion is visible.
    is_warmup: bool = False

    #: Monotonic clock, always. Wall-clock time is subject to adjustment and two
    #: samples measured across one would differ for a reason that is not the
    #: operation.
    duration_ms: float = 0.0
    exit_code: int | None = None

    observed_artifacts: list[str] = Field(default_factory=list)
    target_fingerprint: str | None = None
    scenario_fingerprint: str | None = None
    capability_fingerprint: str | None = None
    result_signature: ResultSignature | None = None
    result_signature_digest: str | None = None
    classification: Classification | None = None
    dimensions: ResultDimensions = Field(default_factory=ResultDimensions)

    containment_usable: bool = True
    cleanup_ok: bool = True
    tracked_tree_clean: bool = True

    #: Empty means valid. Non-empty means this sample contributes to no median and
    #: no classification summary.
    rejections: list[SampleRejection] = Field(default_factory=list)
    rejection_detail: list[str] = Field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.rejections

    @property
    def counts_for_timing(self) -> bool:
        return self.valid and not self.is_warmup


class TimingSummary(BaseModel):
    """A median with its range and every sample behind it.

    No confidence interval, no p-value, no standard error. Five samples on a
    shared runner cannot support any of those, and producing one anyway would
    dress a rough number in a precision it does not have. The individual samples
    are published so a reader can see the spread rather than trust a summary of it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    unit: str = "ms"
    state: MeasurementState = MeasurementState.MEASURED
    samples_ms: list[float] = Field(default_factory=list)
    median_ms: float | None = None
    min_ms: float | None = None
    max_ms: float | None = None
    rejected_samples: int = 0
    #: Set when there were not enough valid samples. The headline number is then
    #: absent rather than computed from what survived.
    insufficient_reason: str | None = None
    limitations: list[str] = Field(default_factory=list)


class CategoryObservation(BaseModel):
    """One category's counts, with the denominator stated rather than implied.

    ``applicable`` is the denominator for every rate a reader might compute, and
    ``not_applicable`` is deliberately outside it. A scenario that could not apply
    is not a scenario the target failed, and including it in a denominator turns
    "this target uses stdio" into a lower score.
    """

    model_config = ConfigDict(extra="forbid")

    category: BenchmarkCategory
    target_key: str | None = None

    applicable_scenarios: int = 0
    executed_scenarios: int = 0
    stable_pass: int = 0
    stable_non_pass: int = 0
    inconclusive: int = 0
    not_applicable: int = 0
    not_measured: int = 0
    expected_difference: int = 0
    behavioral_divergence: int = 0
    spec_violation: int = 0
    harness_error: int = 0
    #: Counted independently of the classifications above, because a
    #: security-relevant result is usually *also* one of them.
    security_relevant_observations: int = 0
    confirmed_private_vulnerabilities: int = 0
    limitations: list[str] = Field(default_factory=list)


class BenchmarkOutputs(BaseModel):
    """The manifest of everything one benchmark execution produced."""

    model_config = ConfigDict(extra="forbid")

    benchmark_schema_version: str = BENCHMARK_SCHEMA_VERSION
    benchmark_name: str = BENCHMARK_NAME
    generated_at: str
    #: The commit this **execution** ran at, read from the working tree that
    #: produced these outputs.
    #:
    #: Distinct from ``definition_harness_commit`` below, and the distinction is
    #: not pedantry. The definition names the commit it was *pinned* at, and a
    #: later execution legitimately runs at a newer one — that is exactly what
    #: `benchmark plan --check` exists to permit. An earlier revision of this
    #: model copied the definition's value into here, so the manifest said the
    #: results came from a commit that had not run them.
    harness_commit: str | None = None
    #: The commit the definition was pinned at, carried through for comparison.
    definition_harness_commit: str | None = None
    visibility: str = "private"

    #: Relative path → sha256 of every artifact this execution wrote.
    artifacts: dict[str, str] = Field(default_factory=dict)
    problems: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems
