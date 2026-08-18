/**
 * Loading the dataset, and refusing to render one that does not validate.
 *
 * The viewer's claim is that everything on a screen came from a benchmark
 * artifact. A loader that shrugged at a malformed document would break that
 * claim quietly — the page would render, the missing fields would come out
 * blank, and blank reads as zero. So the document is validated against the
 * schema generated from the exporter's own models, and a failure produces an
 * error screen naming the paths rather than a partial render.
 */

import type { ErrorObject } from 'ajv';

import validate from './validate-dataset.js';
import type { ViewerDataset } from './types';

/** The contract version this build understands. */
export const SUPPORTED_SCHEMA_VERSION = '1';

export class DatasetError extends Error {
  readonly problems: string[];

  constructor(message: string, problems: string[]) {
    super(message);
    this.name = 'DatasetError';
    this.problems = problems;
  }
}

function describe(error: ErrorObject): string {
  const path = error.instancePath === '' ? '<root>' : error.instancePath;
  return `${path}: ${error.message ?? 'does not validate'}`;
}

/**
 * Validate an unknown value as a dataset.
 *
 * The validator is precompiled at build time. Ajv's usual path builds one with
 * `new Function`, which this page's Content-Security-Policy forbids — a viewer
 * that renders evidence has no business evaluating strings as code.
 */
export function validateDataset(payload: unknown): ViewerDataset {
  if (!validate(payload)) {
    throw new DatasetError(
      'the dataset does not match the schema this viewer was built against',
      (validate.errors ?? []).map(describe).slice(0, 40),
    );
  }

  const dataset = payload as ViewerDataset;
  if (dataset.schema_version !== SUPPORTED_SCHEMA_VERSION) {
    throw new DatasetError('the dataset was written against a different contract version', [
      `dataset schema_version is ${dataset.schema_version}; this build understands ` +
        `${SUPPORTED_SCHEMA_VERSION}. Rendering it would put values on a screen ` +
        `that nothing checked.`,
    ]);
  }
  return dataset;
}

/**
 * Fetch the dataset from the static file server this page is served by.
 *
 * A relative URL, always: the bundle is built with a relative base so it can be
 * served from any directory, and an absolute path would break the moment it was
 * not the site root. No other request is made by this application at any point.
 */
export async function loadDataset(url = './data/dataset.json'): Promise<ViewerDataset> {
  let response: Response;
  try {
    response = await fetch(url);
  } catch (cause) {
    throw new DatasetError('the dataset could not be read', [
      `${url} could not be fetched: ${cause instanceof Error ? cause.message : String(cause)}`,
      'This viewer reads one JSON file from the directory it is served from and ' +
        'makes no other request. Generate it with `mcp-failure-lab viewer export`.',
    ]);
  }
  if (!response.ok) {
    throw new DatasetError('the dataset could not be read', [
      `${url} returned HTTP ${response.status}`,
      'Generate it with `mcp-failure-lab viewer export --benchmark <dir> --mode <mode>`.',
    ]);
  }
  return validateDataset(await response.json());
}
