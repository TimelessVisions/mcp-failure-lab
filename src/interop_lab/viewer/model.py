"""The typed shape of everything the static viewer is allowed to display.

One rule decides this module's design: **the frontend is not a second source of
truth**. Every value on a screen has to exist here first, having been read from a
benchmark artifact whose hash the manifest names, so that no number can be
computed in a component from two other numbers and quietly acquire a meaning
nobody checked.

That is why several fields look redundant. A matrix cell carries its own
applicable denominator, its own repetition count and its own limitations rather
than leaving a view to look them up: a denominator assembled in the browser is a
denominator no test in this repository can hold to the rules the benchmark
states.

The states are kept apart for the same reason they are kept apart in the
benchmark. ``PASS``, ``NOT_APPLICABLE``, ``NOT_MEASURED`` and ``INCONCLUSIVE``
answer four different questions, and any view that collapses two of them is
telling a reader something the evidence does not say.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from interop_lab.benchmark.coverage import MeasurementCoverage

#: Version of the viewer dataset contract. The frontend refuses a dataset whose
#: version it does not know rather than rendering fields it may be guessing at.
VIEWER_SCHEMA_VERSION = "1"


class VisibilityMode(StrEnum):
    """Which of the two datasets this is.

    Not a display preference. The two modes are produced by different code paths
    with different admissible content, and the mode travels inside the document
    so a build cannot mislabel what it bundled.
    """

    PRIVATE = "private"
    PUBLIC_SAFE = "public-safe"


class CellState(StrEnum):
    """What a matrix cell holds.

    A superset of the classifications a run can produce, plus the two states a
    cell can be in for reasons that are not results: nothing applied here, and
    nobody measured here. Both are displayed; neither is a failure.
    """

    PASS = "PASS"  # noqa: S105 - a result state, not a credential
    SPEC_VIOLATION = "SPEC_VIOLATION"
    INVALID_WIRE_MESSAGE = "INVALID_WIRE_MESSAGE"
    BEHAVIORAL_DIVERGENCE = "BEHAVIORAL_DIVERGENCE"
    EXPECTED_DIFFERENCE = "EXPECTED_DIFFERENCE"
    CRASH = "CRASH"
    HANG = "HANG"
    TIMEOUT = "TIMEOUT"
    RECOVERY_FAILURE = "RECOVERY_FAILURE"
    CLEANUP_FAILURE = "CLEANUP_FAILURE"
    SECURITY_RELEVANT = "SECURITY_RELEVANT"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_MEASURED = "NOT_MEASURED"
    HARNESS_ERROR = "HARNESS_ERROR"


class ProjectSummary(BaseModel):
    """What the project is, in the words the repository already uses."""

    model_config = ConfigDict(extra="forbid")

    name: str
    tagline: str
    problem: str
    approach: list[str] = Field(default_factory=list)
    #: Stated as prominently as the capabilities. A tool's boundaries are part of
    #: what it is, and a summary that lists only what something does is an advert.
    not_claimed: list[str] = Field(default_factory=list)


class BenchmarkIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The private benchmark's own name. Absent in public-safe mode: the name of
    #: a private execution is an identity, and a public build that prints it is
    #: claiming to be that execution's report whatever else it says.
    benchmark_name: str | None = None
    benchmark_schema_version: str
    harness_version: str
    #: The commit the definition was pinned at.
    definition_harness_commit: str | None = None
    #: The commit the execution ran at. Legitimately later than the pin, and
    #: recorded separately so neither stands in for the other.
    execution_harness_commit: str | None = None
    generated_at: str
    visibility: str
    correctness_repetitions: int
    timing_warmup_runs: int
    timing_measured_runs: int
    profiles: list[str] = Field(default_factory=list)
    transports: list[str] = Field(default_factory=list)


class EvidenceKind(StrEnum):
    """Where the numbers in this document came from.

    The distinction the viewer's labelling turns on. A real measurement and an
    invented one can be rendered by the same component, and only this field
    stops a reader from taking the second for the first.
    """

    #: Measured against real third-party implementations.
    REAL_MEASUREMENT = "real-measurement"
    #: Produced by the synthetic fixture. No third party is involved at any point.
    SYNTHETIC = "synthetic"


class DatasetProvenance(BaseModel):
    """Three separate provenance questions, kept separate.

    A single "generated from" line runs together three things a reader needs
    apart: what defined the *shape* of this document, what produced its
    *numbers*, and what has been deliberately left out. A public-safe build
    assembled from synthetic evidence shares its schema with the private
    benchmark and shares none of its measurements, and a provenance block that
    could not say both at once was how the synthetic matrix came to be labelled
    with the private benchmark's name.
    """

    model_config = ConfigDict(extra="forbid")

    # 1. Schema and exporter. True of both modes, and the only lineage a
    #    public-safe build genuinely shares with the private one.
    schema_lineage: str
    viewer_schema_version: str
    benchmark_schema_version: str
    harness_version: str

    # 2. The data itself.
    evidence_kind: EvidenceKind
    evidence_note: str

    # 3. What is not here. `benchmark_identity_withheld` is false for a
    #    synthetic build — nothing was withheld, because no private execution
    #    produced it — and the note says which of the two situations this is.
    benchmark_identity_withheld: bool
    benchmark_identity_note: str

    #: The label above the title on every screen. Never the private benchmark's
    #: name unless this document actually is that benchmark's report.
    eyebrow: str
    #: Shown as a banner in every non-private build. `None` in private mode,
    #: where the mode badge and the private title already say what this is.
    disclaimer: str | None = None


class ViewerTarget(BaseModel):
    """One implementation under measurement, as the viewer may show it."""

    model_config = ConfigDict(extra="forbid")

    target_key: str
    display_name: str
    #: Absent in public-safe mode. A pinned identity is exactly the field that
    #: re-identifies a third party, so it is dropped rather than obscured.
    target_id: str | None = None
    target_file: str | None = None
    source_version: str | None = None
    source_commit: str | None = None
    build_context: str | None = None

    language: str
    transport: str
    declared_profile: str
    negotiated_profile: str | None = None
    relationship: str

    target_fingerprint: str | None = None
    portable_identity_fingerprint: str | None = None
    dockerfile_sha256: str | None = None
    lock_sha256: str | None = None
    source_archive_sha256: str | None = None

    capability_fingerprint: str | None = None
    official_conformance: str
    official_conformance_detail: str | None = None
    limitations: list[str] = Field(default_factory=list)


class ViewerScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    version: str
    title: str
    category: str
    scenario_category: str
    nature: str
    purpose: str
    expected_outcome: str
    #: A normative assertion without a citation is an opinion, so the citation is
    #: carried through and the rationale is carried separately — a scenario that
    #: cites nothing must be visibly non-normative rather than merely quiet.
    normative_source: str | None = None
    rationale: str | None = None
    severity: str
    security_category: str | None = None
    scenario_fingerprint: str
    requires_observed_surface: bool = False
    applicability_profiles: list[str] = Field(default_factory=list)
    applicability_transports: list[str] = Field(default_factory=list)
    applicability_min_spec_version: str | None = None
    applicability_max_spec_version: str | None = None
    requires_tools: list[str] = Field(default_factory=list)
    requires_transport_features: dict[str, str] = Field(default_factory=dict)
    requires_safe_tool_invocation: bool = False
    assertion_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class RepetitionResult(BaseModel):
    """One repetition of one scenario against one target."""

    model_config = ConfigDict(extra="forbid")

    repetition: int
    classification: str | None = None
    valid_sample: bool = True
    rejections: list[str] = Field(default_factory=list)
    result_signature_digest: str | None = None


class MatrixCell(BaseModel):
    """One target and one scenario, carrying everything a reader needs beside it.

    The denominator, the repetition count and the limitations travel *in* the
    cell. A view that looked them up elsewhere would be free to look up the wrong
    ones, and the rule this project turns on — that a rate is quoted against the
    applicable set and never against the whole battery — would then hold only by
    convention.
    """

    model_config = ConfigDict(extra="forbid")

    target_key: str
    scenario_id: str
    category: str
    state: CellState

    #: Scenarios in this category, for this target, that could apply. The
    #: denominator for any rate a reader computes, and never the battery size.
    applicable_denominator: int
    applicable: bool
    repetitions: int
    valid_repetitions: int
    #: Complete result-signature equality across repetitions, not a repeated
    #: label. ``None`` when there were too few valid repetitions to compare.
    signature_stable: bool | None = None
    signature_digests: list[str] = Field(default_factory=list)
    repetition_results: list[RepetitionResult] = Field(default_factory=list)

    profile: str
    transport: str
    #: Recorded beside the classification, never instead of it. A result can be a
    #: normative failure and security-relevant at once.
    security_relevant: bool = False
    security_category: str | None = None
    vulnerability_status: str = "not_assessed"
    normative: bool = False
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _a_cell_outside_the_denominator_is_not_counted_inside_it(self) -> MatrixCell:
        if self.state is CellState.NOT_APPLICABLE and self.applicable:
            raise ValueError(
                f"{self.target_key}/{self.scenario_id} is NOT_APPLICABLE and marked "
                "applicable. A scenario that could not apply is not a scenario the "
                "target failed, and counting it inside the denominator turns a "
                "transport choice into a lower score."
            )
        if self.state is CellState.NOT_MEASURED and self.applicable:
            raise ValueError(
                f"{self.target_key}/{self.scenario_id} is NOT_MEASURED and marked "
                "applicable. Nobody looked; there is no result to place in a "
                "denominator."
            )
        return self


class CategorySummary(BaseModel):
    """One category, for one target, with its own denominator and limitations."""

    model_config = ConfigDict(extra="forbid")

    category: str
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
    security_relevant_observations: int = 0
    confirmed_private_vulnerabilities: int = 0
    security_dimension: str = ""
    limitations: list[str] = Field(default_factory=list)


class OverviewTotals(BaseModel):
    """The figures the first screen shows. Deliberately not a score.

    There is no field here that combines the others, and there is no ordering of
    targets. Thirty-two scenarios against three implementations cannot support
    one, and a single number would be read as though it could.
    """

    model_config = ConfigDict(extra="forbid")

    target_count: int = 0
    languages: list[str] = Field(default_factory=list)
    transports: list[str] = Field(default_factory=list)
    profiles: list[str] = Field(default_factory=list)
    scenario_count: int = 0
    correctness_repetitions: int = 0
    applicable_results: int = 0
    stable_pass: int = 0
    stable_non_pass: int = 0
    inconclusive: int = 0
    not_applicable: int = 0
    not_measured: int = 0
    harness_error: int = 0
    security_relevant_observations: int = 0
    confirmed_private_vulnerabilities: int = 0


class AssertionSignatureView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assertion_id: str
    status: str
    classification: str | None = None
    security_relevant: bool = False


class SignatureRecord(BaseModel):
    """One repetition's complete result signature, as the stability contract."""

    model_config = ConfigDict(extra="forbid")

    target_key: str
    scenario_id: str
    repetition: int
    digest: str
    signature_version: int
    classification: str
    failed_assertion_ids: list[str] = Field(default_factory=list)
    assertions: list[AssertionSignatureView] = Field(default_factory=list)
    timeout_cause: str | None = None
    exit_code: int | None = None
    signal: int | None = None
    killed_by_harness: bool = False
    cleanup_ok: bool = True
    containment_outcome: str | None = None
    containment_usable: bool = True
    target_fingerprint: str | None = None
    scenario_fingerprint: str | None = None
    capability_fingerprint: str | None = None
    security_relevant: bool = False
    security_category: str | None = None
    vulnerability_status: str = "not_assessed"
    normative: bool = False
    first_meaningful_difference: str | None = None


class ToolSurface(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    input_schema_fingerprint: str | None = None
    output_schema_fingerprint: str | None = None


class CapabilitySurface(BaseModel):
    """What a target advertised, and how far that was observed rather than read.

    ``declared_tools`` and ``tool_names`` are carried separately on purpose. One
    is a claim in a target file written by a person; the other is what the server
    answered. Presenting either in place of the other is the substitution the
    applicability rules exist to prevent.
    """

    model_config = ConfigDict(extra="forbid")

    target_key: str
    status: str
    status_detail: str | None = None
    negotiated_profile: str | None = None
    offered_profile: str | None = None
    capability_fingerprint: str | None = None
    server_capabilities: dict[str, str] = Field(default_factory=dict)
    tools: list[ToolSurface] = Field(default_factory=list)
    declared_tools: list[str] = Field(default_factory=list)
    advertises_resources: bool = False
    advertises_prompts: bool = False
    advertises_logging: bool = False
    advertises_completions: bool = False
    observed_transport_features: dict[str, str] = Field(default_factory=dict)
    #: Where the target file's declaration and the observed surface disagree.
    declared_versus_observed: list[str] = Field(default_factory=list)
    fingerprint_stable_across_repetitions: bool | None = None
    limitations: list[str] = Field(default_factory=list)


class ContainmentFact(BaseModel):
    """One containment property, requested and observed side by side.

    Never merged. A restriction this harness asked for and the runtime did not
    apply is the single most important thing this screen can show, and a merged
    value would be exactly the one that hides it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    requested: str | None = None
    observed: str | None = None
    agrees: bool | None = None


class ContainmentEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_key: str
    mode: str
    provenance: str
    outcome: str
    usable: bool
    image_source_kind: str | None = None
    portable_build_identity: str | None = None
    builder_local_image_identity: str | None = None
    facts: list[ContainmentFact] = Field(default_factory=list)
    cleanup_ok: bool | None = None
    weaknesses: list[str] = Field(default_factory=list)
    unavailable_reason: str | None = None
    limitations: list[str] = Field(default_factory=list)


class TimingCard(BaseModel):
    """One timing, with everything that decides whether it means anything.

    ``state`` is never omitted and NOT_MEASURED is never dropped. A timings
    screen that showed only the timings it had would let a reader conclude that
    was the whole set, which is the failure this whole checkpoint is about.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    metric_id: str | None = None
    unit: str = "ms"
    state: str
    sample_count: int = 0
    warmup_count: int = 0
    samples_ms: list[float] = Field(default_factory=list)
    warmup_samples_ms: list[float] = Field(default_factory=list)
    median_ms: float | None = None
    min_ms: float | None = None
    max_ms: float | None = None
    #: A single observation that is not part of a sampled distribution, kept
    #: distinct from ``median_ms`` so it can never be displayed as one.
    single_value_ms: float | None = None
    rejected_samples: int = 0
    environment: str = ""
    insufficient_reason: str | None = None
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _a_headline_figure_has_samples_behind_it(self) -> TimingCard:
        if self.median_ms is not None and not self.samples_ms:
            raise ValueError(
                f"{self.name} quotes a median with no samples behind it. A summary "
                "whose individual values are absent cannot be checked."
            )
        return self


class ContainmentOverheadPair(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transport: str
    state: str
    scenario: str | None = None
    host: TimingCard | None = None
    contained: TimingCard | None = None
    overhead_ratio: float | None = None
    overhead_absolute_ms: float | None = None
    reason: str | None = None
    limitations: list[str] = Field(default_factory=list)


class ReproductionSummary(BaseModel):
    """One reproduction package, described without exposing what it contains."""

    model_config = ConfigDict(extra="forbid")

    reference: str
    status: str
    stability_runs: int = 0
    minimization_status: str = "not_measured"
    original_event_count: int | None = None
    minimized_event_count: int | None = None
    original_body_bytes: int | None = None
    minimized_body_bytes: int | None = None
    #: Minimisation preserves the failure *signature* — classification and the
    #: identities of the failing assertions — not merely the label.
    preserved_signature: bool | None = None
    replay_requirements: list[str] = Field(default_factory=list)
    #: True only for first-party fixtures whose defect this repository authored.
    synthetic: bool = False
    limitations: list[str] = Field(default_factory=list)


class MethodologySection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: str
    title: str
    body: list[str] = Field(default_factory=list)


class LimitationEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str
    text: str


class DisclosureControls(BaseModel):
    """What this document is allowed to be, recorded inside the document.

    A build that bundles a dataset cannot then claim a different provenance for
    it, and a public-safe dataset that was never approved for anonymised labels
    says so here rather than in a commit message nobody reads at deploy time.
    """

    model_config = ConfigDict(extra="forbid")

    visibility_mode: VisibilityMode
    publishable: bool = False
    #: Whether the owner explicitly approved anonymised labels standing in for
    #: real third-party identities. Never set by an export on its own.
    anonymization_approved: bool = False
    approval_reference: str | None = None
    redactions_applied: list[str] = Field(default_factory=list)
    #: Deliberately not the denylist itself. Shipping the terms a scan looks for
    #: would put every one of them in the public bundle.
    denylist_term_count: int = 0
    notes: list[str] = Field(default_factory=list)


class ViewerDataset(BaseModel):
    """Everything the viewer may display, and nothing it may compute."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = VIEWER_SCHEMA_VERSION
    visibility_mode: VisibilityMode
    generated_at: str
    #: The harness commit the benchmark execution ran at, carried so a screen can
    #: name the code that produced what it shows.
    generated_from_harness_commit: str | None = None
    #: A digest over the benchmark manifest's artifact map. Two datasets built
    #: from the same execution agree here; one built from a different execution
    #: cannot pretend otherwise.
    generated_from_benchmark_manifest_hash: str

    benchmark: BenchmarkIdentity
    provenance: DatasetProvenance
    project: ProjectSummary
    overview: OverviewTotals

    targets: list[ViewerTarget] = Field(default_factory=list)
    scenarios: list[ViewerScenario] = Field(default_factory=list)
    matrix: list[MatrixCell] = Field(default_factory=list)
    categories: list[CategorySummary] = Field(default_factory=list)
    capabilities: list[CapabilitySurface] = Field(default_factory=list)
    result_signatures: list[SignatureRecord] = Field(default_factory=list)
    containment: list[ContainmentEvidence] = Field(default_factory=list)
    timings: list[TimingCard] = Field(default_factory=list)
    containment_overhead: list[ContainmentOverheadPair] = Field(default_factory=list)
    measurement_coverage: MeasurementCoverage
    reproduction: list[ReproductionSummary] = Field(default_factory=list)
    methodology: list[MethodologySection] = Field(default_factory=list)
    limitations: list[LimitationEntry] = Field(default_factory=list)
    disclosure_controls: DisclosureControls

    @model_validator(mode="after")
    def _the_document_agrees_with_itself_about_what_it_is(self) -> ViewerDataset:
        if self.disclosure_controls.visibility_mode is not self.visibility_mode:
            raise ValueError(
                f"the dataset is {self.visibility_mode.value} and its disclosure "
                f"controls say {self.disclosure_controls.visibility_mode.value}. A "
                "document that disagrees with itself about what it may be cannot be "
                "trusted with either answer."
            )
        if self.visibility_mode is VisibilityMode.PRIVATE and self.disclosure_controls.publishable:
            raise ValueError(
                "a private dataset is marked publishable. These results concern "
                "software this project does not own."
            )
        if self.visibility_mode is VisibilityMode.PUBLIC_SAFE:
            # The name of a private execution is an identity. A public-safe
            # document that still carries it is presenting itself as that
            # execution's report, which is the one thing this mode exists to
            # prevent — and it had been doing exactly that.
            if self.benchmark.benchmark_name is not None:
                raise ValueError(
                    "a public-safe dataset still carries the private benchmark's "
                    f"name ({self.benchmark.benchmark_name!r}). The name identifies "
                    "the private execution; it is dropped, not re-used as a title."
                )
            if self.provenance.disclaimer is None:
                raise ValueError(
                    "a public-safe dataset carries no disclaimer. Every non-private "
                    "build states what its numbers are before it shows any of them."
                )
            if self.benchmark.visibility != self.visibility_mode.value:
                raise ValueError(
                    f"a public-safe dataset reports benchmark visibility "
                    f"{self.benchmark.visibility!r}. The field describes this "
                    "document, and this document is public-safe."
                )
        # A synthetic document may never describe itself as measurement, in
        # either mode. This is the assertion that stops an invented matrix from
        # being read as a third party's results.
        synthetic = self.provenance.evidence_kind is EvidenceKind.SYNTHETIC
        if synthetic and self.provenance.benchmark_identity_withheld:
            raise ValueError(
                "a synthetic dataset claims a private benchmark identity is "
                "withheld. Nothing is being withheld: no private execution "
                "produced this document, and implying one did suggests these "
                "invented rows are an anonymised version of real results."
            )
        known = {target.target_key for target in self.targets}
        unknown = sorted({cell.target_key for cell in self.matrix} - known)
        if unknown:
            raise ValueError(
                f"the matrix holds cells for {unknown}, which are not in the target "
                "list. A cell whose target the document does not describe would "
                "render with no identity, no profile and no limitations."
            )
        return self
