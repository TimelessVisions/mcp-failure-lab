/**
 * The loader must refuse what it cannot verify.
 *
 * A viewer that rendered a malformed dataset would leave its missing fields
 * blank, and blank reads as zero to anyone moving quickly. So a document that
 * does not validate produces an error, never a partial page.
 */

import { describe, expect, it } from 'vitest';

import { DatasetError, SUPPORTED_SCHEMA_VERSION, validateDataset } from '@/data/dataset';
import { mutableDataset, shippedDataset } from './fixture';

describe('dataset validation', () => {
  it('accepts the dataset this build ships', () => {
    const dataset = validateDataset(shippedDataset());
    expect(dataset.schema_version).toBe(SUPPORTED_SCHEMA_VERSION);
    expect(dataset.targets.length).toBeGreaterThan(0);
    expect(dataset.matrix.length).toBeGreaterThan(0);
  });

  it('refuses a document that is not an object', () => {
    expect(() => validateDataset('not a dataset')).toThrow(DatasetError);
  });

  it('refuses a dataset missing a required section', () => {
    const dataset = mutableDataset() as unknown as Record<string, unknown>;
    delete dataset.measurement_coverage;
    expect(() => validateDataset(dataset)).toThrow(DatasetError);
  });

  it('refuses an unknown classification rather than rendering it', () => {
    const dataset = mutableDataset();
    const cell = dataset.matrix[0];
    expect(cell).toBeDefined();
    // A state nothing in this vocabulary defines. Rendering it would give it a
    // colour and a position in a table, which is a meaning nobody assigned.
    (cell as unknown as Record<string, unknown>).state = 'MOSTLY_FINE';
    expect(() => validateDataset(dataset)).toThrow(DatasetError);
  });

  it('refuses a dataset carrying a field the exporter never writes', () => {
    const dataset = mutableDataset() as unknown as Record<string, unknown>;
    dataset.overall_score = 91;
    expect(() => validateDataset(dataset)).toThrow(DatasetError);
  });

  it('refuses a dataset written against a different contract version', () => {
    const dataset = mutableDataset();
    dataset.schema_version = '99';
    expect(() => validateDataset(dataset)).toThrow(/contract version/);
  });

  it('names the failing paths rather than reporting a bare failure', () => {
    const dataset = mutableDataset() as unknown as Record<string, unknown>;
    delete dataset.overview;
    try {
      validateDataset(dataset);
      expect.unreachable('a dataset without an overview must not validate');
    } catch (error) {
      expect(error).toBeInstanceOf(DatasetError);
      expect((error as DatasetError).problems.length).toBeGreaterThan(0);
    }
  });
});
