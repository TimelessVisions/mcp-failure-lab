/**
 * The walkthrough's contract: every required kind of evidence is accounted for.
 *
 * "Accounted for" is deliberately two outcomes. A stage either shows a real
 * example drawn from this execution, or it says the execution contains none.
 * What it may never do is quietly disappear, or substitute a different example
 * and let the title imply otherwise — a walkthrough that skipped its
 * divergence stage on a dataset with no divergence would read as though every
 * dataset had one.
 *
 * These run against whichever dataset the build ships. The same assertions run
 * against the real private benchmark in the private viewer job, where the
 * interesting question is not whether the code works but whether the actual
 * measurements contain each kind of evidence.
 */

import { describe, expect, it } from 'vitest';

import {
  GUIDED_REQUIREMENTS,
  auditStages,
  buildPublicStages,
  buildStages,
  stagesFor,
} from '@/guided/GuidedMode';
import { mutableDataset, shippedDataset } from './fixture';

const dataset = shippedDataset();

describe('the private walkthrough', () => {
  // Built explicitly rather than through `stagesFor`, because the shipped
  // dataset is the public-safe one and this is the other script.
  const stages = buildStages(dataset);

  it('covers every required kind of evidence', () => {
    const audit = auditStages(stages);
    const uncovered = audit.filter((entry) => !entry.covered).map((entry) => entry.requirement);
    expect(uncovered, `no stage claims: ${uncovered.join(', ')}`).toEqual([]);
    expect(audit.length).toBe(GUIDED_REQUIREMENTS.length);
  });

  it('resolves each requirement to either real evidence or a stated absence', () => {
    for (const entry of auditStages(stages)) {
      expect(
        entry.evidence,
        `${entry.requirement} resolved to neither evidence nor a stated absence`,
      ).toMatch(/^(present|absent)$/);
    }
  });

  it('gives every stage a distinct id and a title', () => {
    const ids = stages.map((stage) => stage.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const stage of stages) expect(stage.title.length).toBeGreaterThan(0);
  });

  it('ends on limitations rather than on a result', () => {
    expect(stages[stages.length - 1]?.id).toBe('methodology');
  });
});

describe('the public-safe walkthrough', () => {
  const stages = buildPublicStages(dataset);

  it('is a different script from the private one', () => {
    const privateIds = new Set(buildStages(dataset).map((stage) => stage.id));
    for (const stage of stages) expect(privateIds.has(stage.id)).toBe(false);
  });

  it('is the script a public-safe dataset actually gets', () => {
    expect(stagesFor(dataset).map((stage) => stage.id)).toEqual(
      stages.map((stage) => stage.id),
    );
  });

  it('claims no required private evidence', () => {
    // The public-safe script demonstrates the interface and the methodology. It
    // must not present itself as exhibiting findings, so it claims none of the
    // evidence requirements the private walkthrough is measured against.
    for (const entry of auditStages(stages)) {
      expect(entry.covered).toBe(false);
    }
  });

  it('opens by saying what the evidence is', () => {
    expect(stages[0]?.id).toBe('public-what-this-is');
  });
});

describe('script selection', () => {
  it('follows the dataset rather than a build flag', () => {
    const asPrivate = mutableDataset();
    asPrivate.visibility_mode = 'private';
    expect(stagesFor(asPrivate).map((stage) => stage.id)).toEqual(
      buildStages(asPrivate).map((stage) => stage.id),
    );
  });
});
