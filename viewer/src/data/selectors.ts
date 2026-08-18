/**
 * Grouping and lookup over the dataset. Deliberately no arithmetic.
 *
 * Every function here indexes, filters or sorts values the exporter already
 * decided. None of them adds, divides or compares two numbers to produce a
 * third: a rate computed in the browser is a rate no Python test can hold to
 * the benchmark's denominator rules, and that is exactly the failure mode this
 * whole design is arranged against.
 *
 * The one exception is `describeDenominator`, which formats a count and its
 * denominator into a string. It does not divide them, on purpose.
 */

import type {
  CapabilitySurface,
  CategorySummary,
  CellState,
  ContainmentEvidence,
  MatrixCell,
  SignatureRecord,
  TimingCard,
  ViewerDataset,
  ViewerScenario,
  ViewerTarget,
} from './types';

export interface CellKey {
  targetKey: string;
  scenarioId: string;
}

/** Cell lookup by target and scenario. */
export function indexMatrix(dataset: ViewerDataset): Map<string, MatrixCell> {
  const index = new Map<string, MatrixCell>();
  for (const cell of dataset.matrix) {
    index.set(`${cell.target_key}::${cell.scenario_id}`, cell);
  }
  return index;
}

export function cellAt(index: Map<string, MatrixCell>, key: CellKey): MatrixCell | undefined {
  return index.get(`${key.targetKey}::${key.scenarioId}`);
}

export function targetByKey(dataset: ViewerDataset, key: string): ViewerTarget | undefined {
  return dataset.targets.find((target) => target.target_key === key);
}

export function scenarioById(dataset: ViewerDataset, id: string): ViewerScenario | undefined {
  return dataset.scenarios.find((scenario) => scenario.scenario_id === id);
}

export function cellsForTarget(dataset: ViewerDataset, key: string): MatrixCell[] {
  return dataset.matrix.filter((cell) => cell.target_key === key);
}

export function cellsForScenario(dataset: ViewerDataset, id: string): MatrixCell[] {
  return dataset.matrix.filter((cell) => cell.scenario_id === id);
}

export function categoriesForTarget(dataset: ViewerDataset, key: string): CategorySummary[] {
  return dataset.categories.filter((summary) => summary.target_key === key);
}

export function summariesForCategory(
  dataset: ViewerDataset,
  category: string,
): CategorySummary[] {
  return dataset.categories.filter((summary) => summary.category === category);
}

export function capabilityFor(
  dataset: ViewerDataset,
  key: string,
): CapabilitySurface | undefined {
  return dataset.capabilities.find((surface) => surface.target_key === key);
}

export function containmentFor(
  dataset: ViewerDataset,
  key: string,
): ContainmentEvidence | undefined {
  return dataset.containment.find((evidence) => evidence.target_key === key);
}

export function signaturesFor(dataset: ViewerDataset, key: CellKey): SignatureRecord[] {
  return dataset.result_signatures
    .filter(
      (record) => record.target_key === key.targetKey && record.scenario_id === key.scenarioId,
    )
    .sort((left, right) => left.repetition - right.repetition);
}

/** Every category present in the dataset, in a stable order. */
export function categoryNames(dataset: ViewerDataset): string[] {
  return [...new Set(dataset.categories.map((summary) => summary.category))].sort();
}

/** Timings whose metric the coverage ledger reports as never measured. */
export function unmeasuredTimings(dataset: ViewerDataset): TimingCard[] {
  return dataset.timings.filter((card) => card.state === 'not_measured');
}

export function measuredTimings(dataset: ViewerDataset): TimingCard[] {
  return dataset.timings.filter((card) => card.state !== 'not_measured');
}

/**
 * Format a count against its stated denominator.
 *
 * Returns `"4 of 11 applicable"`, never `"36%"`. A percentage invites a
 * comparison between two targets whose applicable sets are different sizes for
 * reasons that have nothing to do with quality, and this project's central rule
 * is that such a comparison is not available from this evidence.
 */
export function describeDenominator(count: number, denominator: number): string {
  if (denominator === 0) {
    return `${count} — no applicable scenarios in this category`;
  }
  return `${count} of ${denominator} applicable`;
}

/** How a state reads to someone who has not memorised the vocabulary. */
export const STATE_MEANING: Record<CellState, string> = {
  PASS: 'The target answered the exchanges this scenario sends.',
  SPEC_VIOLATION: 'A dated requirement of the negotiated profile was not met.',
  INVALID_WIRE_MESSAGE: 'A message that could not be parsed as valid JSON-RPC.',
  BEHAVIORAL_DIVERGENCE:
    'Implementations differ here and no dated requirement decides between them. Not a defect.',
  EXPECTED_DIFFERENCE: 'A difference this project decided in advance is legitimate.',
  CRASH: 'The process terminated abnormally.',
  HANG: 'No answer arrived and the process did not terminate.',
  TIMEOUT: 'No answer arrived within the deadline this harness chose.',
  RECOVERY_FAILURE: 'The target did not return to a usable state after a fault.',
  CLEANUP_FAILURE: 'The harness could not reclaim the process tree.',
  SECURITY_RELEVANT:
    'A legacy label retained so older artifacts still read as what they were. Security relevance is now a separate dimension.',
  INCONCLUSIVE: 'The repetitions disagreed. A fact about this harness, not about the target.',
  NOT_APPLICABLE:
    'The scenario could not apply. Outside every denominator, and never a failure.',
  NOT_MEASURED: 'Nobody looked. Never a failure and never a zero.',
  HARNESS_ERROR: 'The harness or the environment failed. Never a verdict about the target.',
};

/** Which visual family a state belongs to. Four families, not a scale. */
export function stateFamily(
  state: CellState,
): 'pass' | 'normative' | 'divergence' | 'absent' | 'uncertain' {
  switch (state) {
    case 'PASS':
      return 'pass';
    case 'SPEC_VIOLATION':
    case 'INVALID_WIRE_MESSAGE':
    case 'CRASH':
    case 'HANG':
    case 'TIMEOUT':
    case 'RECOVERY_FAILURE':
    case 'CLEANUP_FAILURE':
      return 'normative';
    case 'BEHAVIORAL_DIVERGENCE':
    case 'EXPECTED_DIFFERENCE':
    case 'SECURITY_RELEVANT':
      return 'divergence';
    case 'NOT_APPLICABLE':
    case 'NOT_MEASURED':
      return 'absent';
    case 'INCONCLUSIVE':
    case 'HARNESS_ERROR':
      return 'uncertain';
  }
}

/**
 * How a transport identifier is written for a reader.
 *
 * The dataset carries the protocol's own spelling — `streamable_http` — which is
 * the right value to compare and the wrong one to print: it is long, it is not
 * how the specification names the transport in prose, and it was being clipped
 * mid-word in the overview. Unknown identifiers are returned untouched rather
 * than prettified by a rule that might mangle them.
 */
export function transportLabel(transport: string): string {
  switch (transport) {
    case 'stdio':
      return 'stdio';
    case 'streamable_http':
      return 'Streamable HTTP';
    case 'sse':
      return 'SSE';
    default:
      return transport;
  }
}

export function formatMs(value: number | null): string {
  if (value === null) return '—';
  if (value < 1) return `${value.toFixed(3)} ms`;
  if (value < 1000) return `${value.toFixed(1)} ms`;
  return `${(value / 1000).toFixed(2)} s`;
}
