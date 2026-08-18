/**
 * The dataset the tests run against: the one this build would actually ship.
 *
 * Read from `public/data`, not from a hand-written literal. A fixture written
 * beside the tests drifts from the exporter and then the suite certifies a
 * shape nothing produces — which is the same failure the generated schema
 * exists to prevent one level down.
 */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import type { ViewerDataset } from '@/data/types';

// Resolved from the project root rather than from `import.meta.url`: under the
// jsdom environment that URL is an http one, and `fileURLToPath` rejects it.
export const PUBLIC_DIR = resolve(process.cwd(), 'public');
const PUBLIC_DATASET = resolve(PUBLIC_DIR, 'data/dataset.json');

export function shippedDataset(): ViewerDataset {
  return JSON.parse(readFileSync(PUBLIC_DATASET, 'utf-8')) as ViewerDataset;
}

/** A deep copy, so a test that mutates cannot leak into the next one. */
export function mutableDataset(): ViewerDataset {
  return JSON.parse(JSON.stringify(shippedDataset())) as ViewerDataset;
}
