/**
 * The rules the evidence turns on, asserted against the shipped dataset.
 *
 * These are semantic assertions, not snapshots. A snapshot of a large data blob
 * passes whenever the blob is unchanged and says nothing about whether the
 * denominators are right — which is the only property here worth defending.
 */

import { describe, expect, it } from 'vitest';

import { describeDenominator, stateFamily, unmeasuredTimings } from '@/data/selectors';
import { shippedDataset } from './fixture';

const dataset = shippedDataset();

describe('denominators', () => {
  it('never places a NOT_APPLICABLE cell inside the applicable set', () => {
    for (const cell of dataset.matrix) {
      if (cell.state === 'NOT_APPLICABLE' || cell.state === 'NOT_MEASURED') {
        expect(cell.applicable).toBe(false);
      }
    }
  });

  it('gives every applicable cell a denominator greater than zero', () => {
    for (const cell of dataset.matrix.filter((entry) => entry.applicable)) {
      expect(cell.applicable_denominator).toBeGreaterThan(0);
    }
  });

  it('carries the category denominator into the cell rather than re-deriving it', () => {
    const byKey = new Map(
      dataset.categories.map((summary) => [
        `${summary.target_key ?? ''}::${summary.category}`,
        summary.applicable_scenarios,
      ]),
    );
    for (const cell of dataset.matrix) {
      const expected = byKey.get(`${cell.target_key}::${cell.category}`);
      if (expected !== undefined) {
        expect(cell.applicable_denominator).toBe(expected);
      }
    }
  });

  it('states a denominator as a fraction and never as a percentage', () => {
    expect(describeDenominator(4, 11)).toBe('4 of 11 applicable');
    expect(describeDenominator(0, 0)).toContain('no applicable scenarios');
    expect(describeDenominator(4, 11)).not.toContain('%');
  });

  it('agrees with the overview totals', () => {
    const applicable = dataset.categories.reduce(
      (total, summary) => total + summary.applicable_scenarios,
      0,
    );
    expect(dataset.overview.applicable_results).toBe(applicable);
  });
});

describe('result states', () => {
  it('keeps the absent states in their own visual family', () => {
    expect(stateFamily('NOT_APPLICABLE')).toBe('absent');
    expect(stateFamily('NOT_MEASURED')).toBe('absent');
    expect(stateFamily('INCONCLUSIVE')).toBe('uncertain');
    expect(stateFamily('BEHAVIORAL_DIVERGENCE')).toBe('divergence');
    expect(stateFamily('SPEC_VIOLATION')).toBe('normative');
    expect(stateFamily('PASS')).toBe('pass');
  });

  it('does not sort a divergence into the same family as a violation', () => {
    expect(stateFamily('BEHAVIORAL_DIVERGENCE')).not.toBe(stateFamily('SPEC_VIOLATION'));
  });

  it('keeps security relevance beside the primary outcome, never instead of it', () => {
    const relevant = dataset.matrix.filter((cell) => cell.security_relevant);
    expect(relevant.length).toBeGreaterThan(0);
    for (const cell of relevant) {
      expect(cell.state).not.toBe('SECURITY_RELEVANT');
    }
  });

  it('reports no confirmed vulnerability, which no execution path may write', () => {
    expect(dataset.overview.confirmed_private_vulnerabilities).toBe(0);
  });
});

describe('signature stability', () => {
  it('calls a cell stable only when one distinct signature was recorded', () => {
    for (const cell of dataset.matrix) {
      if (cell.signature_stable === true) {
        expect(cell.signature_digests).toHaveLength(1);
      }
      if (cell.signature_stable === false) {
        expect(cell.signature_digests.length).toBeGreaterThan(1);
      }
    }
  });

  it('exercises at least one genuinely unstable result', () => {
    const unstable = dataset.matrix.filter((cell) => cell.signature_stable === false);
    expect(unstable.length).toBeGreaterThan(0);
  });
});

describe('measurement coverage', () => {
  it('gives every promised metric an explicit state', () => {
    expect(dataset.measurement_coverage.entries.length).toBeGreaterThanOrEqual(16);
    for (const entry of dataset.measurement_coverage.entries) {
      expect(['measured', 'partially_measured', 'not_measured', 'not_applicable']).toContain(
        entry.state,
      );
      expect(entry.limitation.length).toBeGreaterThan(0);
      if (entry.state !== 'measured') {
        expect(entry.reason_not_measured).toBeTruthy();
      }
    }
  });

  it('never reports an un-measured metric with samples behind it', () => {
    for (const entry of dataset.measurement_coverage.entries) {
      if (entry.state === 'not_measured') {
        expect(entry.sample_count).toBe(0);
        expect(entry.artifact_reference).toBeNull();
      }
    }
  });

  it('gives every un-measured metric a timing card carrying its reason', () => {
    const carded = new Set(unmeasuredTimings(dataset).map((card) => card.metric_id));
    for (const entry of dataset.measurement_coverage.entries) {
      if (entry.state === 'not_measured') {
        expect(carded.has(entry.metric_id)).toBe(true);
      }
    }
    for (const card of unmeasuredTimings(dataset)) {
      expect(card.insufficient_reason).toBeTruthy();
    }
  });

  it('never quotes a median without the samples behind it', () => {
    for (const card of dataset.timings) {
      if (card.median_ms !== null) {
        expect(card.samples_ms.length).toBeGreaterThan(0);
        expect(card.min_ms).not.toBeNull();
        expect(card.max_ms).not.toBeNull();
      }
    }
  });
});

describe('no ranking', () => {
  it('has no field on the overview that combines the others', () => {
    for (const key of Object.keys(dataset.overview)) {
      expect(key).not.toMatch(/score|rank|rating|grade|overall|best|winner/i);
    }
  });

  it('carries a methodology section stating the no-ranking rule', () => {
    const section = dataset.methodology.find((entry) => entry.section_id === 'no-ranking');
    expect(section).toBeDefined();
    expect(section?.body.join(' ')).toMatch(
      /no best implementation|not a ranking|cannot support/i,
    );
  });
});
