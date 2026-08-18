/**
 * The bundled dataset must not carry anything that names a third party.
 *
 * The authoritative leak scan lives in Python, where the denylist is generated
 * from the private inventories. This is the second gate: it runs over the file
 * this build actually bundles, so a dataset copied into `public/data` by hand,
 * or left behind from a private export, fails here before it can be built.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { PUBLIC_DIR, shippedDataset } from './fixture';

const dataset = shippedDataset();

/**
 * Shapes that identify a third party regardless of which name they carry.
 *
 * Deliberately structural rather than a list of names: a name list only catches
 * the targets somebody remembered to add, and the point of a second gate is to
 * catch what the first one missed.
 */
const IDENTIFYING_SHAPES: Array<{ name: string; pattern: RegExp }> = [
  { name: 'a git commit sha', pattern: /\b[0-9a-f]{40}\b/ },
  { name: 'a sha256 digest', pattern: /\bsha256:[0-9a-f]{64}\b/ },
  { name: 'a GitHub repository path', pattern: /github\.com\/[\w.-]+\/[\w.-]+/ },
  { name: 'an npm scoped package', pattern: /@[\w-]+\/[\w-]+/ },
  { name: 'a GitHub Actions artifact URL', pattern: /actions\/runs\/\d+/ },
  { name: 'a private finding identifier', pattern: /ILAB-[A-Z]+-\d+/ },
  { name: 'the private development branch', pattern: /agent\/mcp-interop-foundation/ },
  { name: 'the private results directory', pattern: /private-(results|findings)/ },
];

function walk(directory: string): string[] {
  return readdirSync(directory).flatMap((entry) => {
    const path = join(directory, entry);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}

describe('the bundled dataset', () => {
  it('is the public-safe one', () => {
    expect(dataset.visibility_mode).toBe('public-safe');
    expect(dataset.disclosure_controls.visibility_mode).toBe('public-safe');
  });

  it('is not marked publishable', () => {
    expect(dataset.disclosure_controls.publishable).toBe(false);
  });

  it('carries no pinned third-party identity', () => {
    for (const target of dataset.targets) {
      expect(target.target_id).toBeNull();
      expect(target.target_file).toBeNull();
      expect(target.source_version).toBeNull();
      expect(target.source_commit).toBeNull();
      expect(target.build_context).toBeNull();
      expect(target.display_name).toMatch(/^Implementation [A-Z]$/);
    }
  });

  it('carries no fingerprint a guess could be confirmed against', () => {
    for (const target of dataset.targets) {
      expect(target.target_fingerprint).toBeNull();
      expect(target.capability_fingerprint).toBeNull();
      expect(target.portable_identity_fingerprint).toBeNull();
      expect(target.dockerfile_sha256).toBeNull();
      expect(target.lock_sha256).toBeNull();
      expect(target.source_archive_sha256).toBeNull();
    }
    for (const record of dataset.result_signatures) {
      expect(record.target_fingerprint).toBeNull();
      expect(record.capability_fingerprint).toBeNull();
    }
  });

  it('carries no private finding package', () => {
    for (const summary of dataset.reproduction) {
      expect(summary.synthetic).toBe(true);
    }
  });

  it('carries no environment or harness identity', () => {
    expect(dataset.generated_from_harness_commit).toBeNull();
    for (const card of dataset.timings) {
      expect(card.environment).toBe('environment withheld');
    }
  });

  it('contains none of the identifying shapes anywhere in the document', () => {
    const serialised = JSON.stringify(dataset);
    for (const { name, pattern } of IDENTIFYING_SHAPES) {
      const match = pattern.exec(serialised);
      expect(match, `the dataset contains ${name}: ${match?.[0] ?? ''}`).toBeNull();
    }
  });
});

describe('what the public-safe build says it is', () => {
  it('does not present the private benchmark as its identity', () => {
    // The exact defect this closes: the public-safe build displayed the
    // private benchmark's title, so an invented matrix appeared under the
    // masthead of a real private execution.
    expect(dataset.benchmark.benchmark_name).toBeNull();
    expect(JSON.stringify(dataset)).not.toMatch(/Private 0\.3/);
  });

  it('states that the evidence is synthetic, in those words', () => {
    expect(dataset.provenance.evidence_kind).toBe('synthetic');
    expect(dataset.provenance.eyebrow).toBe('PUBLIC-SAFE SYNTHETIC EVIDENCE');
    expect(dataset.provenance.disclaimer).toBe(
      'This demonstration uses invented implementations and synthetic measurements. ' +
        'No third-party benchmark result is displayed.',
    );
  });

  it('does not imply the synthetic matrix is an anonymised real one', () => {
    // Nothing is withheld, because no private execution produced this
    // document. Claiming otherwise would suggest these rows are a masked
    // version of results that exist.
    expect(dataset.provenance.benchmark_identity_withheld).toBe(false);
    expect(dataset.provenance.benchmark_identity_note).toMatch(/not an anonymised version/i);
  });

  it('keeps schema provenance separate from data provenance', () => {
    // The schema really is shared with the private benchmark. The measurements
    // are not. Both facts, stated apart.
    expect(dataset.provenance.schema_lineage).toMatch(/Benchmark 0\.3 schema/);
    expect(dataset.provenance.benchmark_schema_version).toBe('0.3');
    expect(dataset.provenance.evidence_note).toMatch(/invented/i);
  });

  it('reports its own visibility rather than the source benchmark', () => {
    expect(dataset.benchmark.visibility).toBe('public-safe');
  });
});

describe('everything staged for the bundle', () => {
  it('contains none of the identifying shapes in any bundled file', () => {
    for (const path of walk(PUBLIC_DIR)) {
      const text = readFileSync(path, 'utf-8');
      for (const { name, pattern } of IDENTIFYING_SHAPES) {
        const match = pattern.exec(text);
        expect(match, `${path} contains ${name}: ${match?.[0] ?? ''}`).toBeNull();
      }
    }
  });
});
