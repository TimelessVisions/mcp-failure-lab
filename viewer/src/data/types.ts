/**
 * The shape of the viewer dataset, mirroring `interop_lab.viewer.model`.
 *
 * These are types, not values. Nothing here decides what a number means — the
 * exporter does that, and `validateDataset` holds a loaded document to the
 * schema generated from those same Python models, so a drift between this file
 * and the exporter fails a test rather than rendering a field that does not
 * exist.
 */

export type VisibilityMode = 'private' | 'public-safe';

/**
 * Every state a matrix cell can hold.
 *
 * `NOT_APPLICABLE`, `NOT_MEASURED` and `INCONCLUSIVE` are three different
 * answers and none of them is a failure. The union keeps them apart at the type
 * level so a component cannot narrow two of them into one branch by accident.
 */
export type CellState =
  | 'PASS'
  | 'SPEC_VIOLATION'
  | 'INVALID_WIRE_MESSAGE'
  | 'BEHAVIORAL_DIVERGENCE'
  | 'EXPECTED_DIFFERENCE'
  | 'CRASH'
  | 'HANG'
  | 'TIMEOUT'
  | 'RECOVERY_FAILURE'
  | 'CLEANUP_FAILURE'
  | 'SECURITY_RELEVANT'
  | 'INCONCLUSIVE'
  | 'NOT_APPLICABLE'
  | 'NOT_MEASURED'
  | 'HARNESS_ERROR';

export type CoverageState =
  'measured' | 'partially_measured' | 'not_measured' | 'not_applicable';

export interface ProjectSummary {
  name: string;
  tagline: string;
  problem: string;
  approach: string[];
  not_claimed: string[];
}

export interface BenchmarkIdentity {
  /**
   * Null in public-safe mode. The private execution's name is an identity, so
   * it is dropped rather than re-used as a public title — read
   * `provenance.eyebrow` for the label a screen should actually show.
   */
  benchmark_name: string | null;
  benchmark_schema_version: string;
  harness_version: string;
  definition_harness_commit: string | null;
  execution_harness_commit: string | null;
  generated_at: string;
  visibility: string;
  correctness_repetitions: number;
  timing_warmup_runs: number;
  timing_measured_runs: number;
  profiles: string[];
  transports: string[];
}

/** Where the numbers came from. See `interop_lab.viewer.model.EvidenceKind`. */
export type EvidenceKind = 'real-measurement' | 'synthetic';

/**
 * Three provenance questions kept apart: what defined the shape of this
 * document, what produced its numbers, and what is deliberately absent.
 */
export interface DatasetProvenance {
  schema_lineage: string;
  viewer_schema_version: string;
  benchmark_schema_version: string;
  harness_version: string;
  evidence_kind: EvidenceKind;
  evidence_note: string;
  benchmark_identity_withheld: boolean;
  benchmark_identity_note: string;
  eyebrow: string;
  disclaimer: string | null;
}

export interface ViewerTarget {
  target_key: string;
  display_name: string;
  target_id: string | null;
  target_file: string | null;
  source_version: string | null;
  source_commit: string | null;
  build_context: string | null;
  language: string;
  transport: string;
  declared_profile: string;
  negotiated_profile: string | null;
  relationship: string;
  target_fingerprint: string | null;
  portable_identity_fingerprint: string | null;
  dockerfile_sha256: string | null;
  lock_sha256: string | null;
  source_archive_sha256: string | null;
  capability_fingerprint: string | null;
  official_conformance: string;
  official_conformance_detail: string | null;
  limitations: string[];
}

export interface ViewerScenario {
  scenario_id: string;
  version: string;
  title: string;
  category: string;
  scenario_category: string;
  nature: string;
  purpose: string;
  expected_outcome: string;
  normative_source: string | null;
  rationale: string | null;
  severity: string;
  security_category: string | null;
  scenario_fingerprint: string;
  requires_observed_surface: boolean;
  applicability_profiles: string[];
  applicability_transports: string[];
  applicability_min_spec_version: string | null;
  applicability_max_spec_version: string | null;
  requires_tools: string[];
  requires_transport_features: Record<string, string>;
  requires_safe_tool_invocation: boolean;
  assertion_ids: string[];
  limitations: string[];
}

export interface RepetitionResult {
  repetition: number;
  classification: string | null;
  valid_sample: boolean;
  rejections: string[];
  result_signature_digest: string | null;
}

export interface MatrixCell {
  target_key: string;
  scenario_id: string;
  category: string;
  state: CellState;
  applicable_denominator: number;
  applicable: boolean;
  repetitions: number;
  valid_repetitions: number;
  signature_stable: boolean | null;
  signature_digests: string[];
  repetition_results: RepetitionResult[];
  profile: string;
  transport: string;
  security_relevant: boolean;
  security_category: string | null;
  vulnerability_status: string;
  normative: boolean;
  limitations: string[];
}

export interface CategorySummary {
  category: string;
  target_key: string | null;
  applicable_scenarios: number;
  executed_scenarios: number;
  stable_pass: number;
  stable_non_pass: number;
  inconclusive: number;
  not_applicable: number;
  not_measured: number;
  expected_difference: number;
  behavioral_divergence: number;
  spec_violation: number;
  harness_error: number;
  security_relevant_observations: number;
  confirmed_private_vulnerabilities: number;
  security_dimension: string;
  limitations: string[];
}

export interface OverviewTotals {
  target_count: number;
  languages: string[];
  transports: string[];
  profiles: string[];
  scenario_count: number;
  correctness_repetitions: number;
  applicable_results: number;
  stable_pass: number;
  stable_non_pass: number;
  inconclusive: number;
  not_applicable: number;
  not_measured: number;
  harness_error: number;
  security_relevant_observations: number;
  confirmed_private_vulnerabilities: number;
}

export interface AssertionSignatureView {
  assertion_id: string;
  status: string;
  classification: string | null;
  security_relevant: boolean;
}

export interface SignatureRecord {
  target_key: string;
  scenario_id: string;
  repetition: number;
  digest: string;
  signature_version: number;
  classification: string;
  failed_assertion_ids: string[];
  assertions: AssertionSignatureView[];
  timeout_cause: string | null;
  exit_code: number | null;
  signal: number | null;
  killed_by_harness: boolean;
  cleanup_ok: boolean;
  containment_outcome: string | null;
  containment_usable: boolean;
  target_fingerprint: string | null;
  scenario_fingerprint: string | null;
  capability_fingerprint: string | null;
  security_relevant: boolean;
  security_category: string | null;
  vulnerability_status: string;
  normative: boolean;
  first_meaningful_difference: string | null;
}

export interface ToolSurface {
  name: string;
  input_schema_fingerprint: string | null;
  output_schema_fingerprint: string | null;
}

export interface CapabilitySurface {
  target_key: string;
  status: string;
  status_detail: string | null;
  negotiated_profile: string | null;
  offered_profile: string | null;
  capability_fingerprint: string | null;
  server_capabilities: Record<string, string>;
  tools: ToolSurface[];
  declared_tools: string[];
  advertises_resources: boolean;
  advertises_prompts: boolean;
  advertises_logging: boolean;
  advertises_completions: boolean;
  observed_transport_features: Record<string, string>;
  declared_versus_observed: string[];
  fingerprint_stable_across_repetitions: boolean | null;
  limitations: string[];
}

export interface ContainmentFact {
  name: string;
  requested: string | null;
  observed: string | null;
  agrees: boolean | null;
}

export interface ContainmentEvidence {
  target_key: string;
  mode: string;
  provenance: string;
  outcome: string;
  usable: boolean;
  image_source_kind: string | null;
  portable_build_identity: string | null;
  builder_local_image_identity: string | null;
  facts: ContainmentFact[];
  cleanup_ok: boolean | null;
  weaknesses: string[];
  unavailable_reason: string | null;
  limitations: string[];
}

export interface TimingCard {
  name: string;
  metric_id: string | null;
  unit: string;
  state: string;
  sample_count: number;
  warmup_count: number;
  samples_ms: number[];
  warmup_samples_ms: number[];
  median_ms: number | null;
  min_ms: number | null;
  max_ms: number | null;
  single_value_ms: number | null;
  rejected_samples: number;
  environment: string;
  insufficient_reason: string | null;
  limitations: string[];
}

export interface ContainmentOverheadPair {
  transport: string;
  state: string;
  scenario: string | null;
  host: TimingCard | null;
  contained: TimingCard | null;
  overhead_ratio: number | null;
  overhead_absolute_ms: number | null;
  reason: string | null;
  limitations: string[];
}

export interface MeasurementCoverageEntry {
  metric_id: string;
  display_name: string;
  category: string;
  state: CoverageState;
  sample_count: number;
  minimum_required_samples: number;
  warmup_count: number;
  artifact_reference: string | null;
  measurement_method: string;
  environment: string;
  limitation: string;
  reason_not_measured: string | null;
}

export interface MeasurementCoverage {
  coverage_schema_version: string;
  entries: MeasurementCoverageEntry[];
  limitations: string[];
}

export interface ReproductionSummary {
  reference: string;
  status: string;
  stability_runs: number;
  minimization_status: string;
  original_event_count: number | null;
  minimized_event_count: number | null;
  original_body_bytes: number | null;
  minimized_body_bytes: number | null;
  preserved_signature: boolean | null;
  replay_requirements: string[];
  synthetic: boolean;
  limitations: string[];
}

export interface MethodologySection {
  section_id: string;
  title: string;
  body: string[];
}

export interface LimitationEntry {
  scope: string;
  text: string;
}

export interface DisclosureControls {
  visibility_mode: VisibilityMode;
  publishable: boolean;
  anonymization_approved: boolean;
  approval_reference: string | null;
  redactions_applied: string[];
  denylist_term_count: number;
  notes: string[];
}

export interface ViewerDataset {
  schema_version: string;
  visibility_mode: VisibilityMode;
  generated_at: string;
  generated_from_harness_commit: string | null;
  generated_from_benchmark_manifest_hash: string;
  benchmark: BenchmarkIdentity;
  provenance: DatasetProvenance;
  project: ProjectSummary;
  overview: OverviewTotals;
  targets: ViewerTarget[];
  scenarios: ViewerScenario[];
  matrix: MatrixCell[];
  categories: CategorySummary[];
  capabilities: CapabilitySurface[];
  result_signatures: SignatureRecord[];
  containment: ContainmentEvidence[];
  timings: TimingCard[];
  containment_overhead: ContainmentOverheadPair[];
  measurement_coverage: MeasurementCoverage;
  reproduction: ReproductionSummary[];
  methodology: MethodologySection[];
  limitations: LimitationEntry[];
  disclosure_controls: DisclosureControls;
}
