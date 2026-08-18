import { expect, type Locator, type Page } from '@playwright/test';

/**
 * Collect console errors and page exceptions for the life of one page.
 *
 * Asserted at the end of every spec. A console error in a viewer whose whole
 * claim is provenance is not cosmetic — it usually means a value came out
 * `undefined` and rendered as a blank where a number should be.
 */
export function watchConsole(page: Page): string[] {
  const problems: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error') problems.push(`console.error: ${message.text()}`);
  });
  page.on('pageerror', (error) => problems.push(`pageerror: ${error.message}`));
  return problems;
}

/**
 * Fail the test if the page tries to reach any origin other than its own.
 *
 * Returns the blocked URLs so a spec can assert on them. The viewer is served
 * with the network disabled, so an outbound request is a defect that would only
 * surface in the environment where it matters least to discover it.
 */
/**
 * Resource kinds a *page* asks for. Anything else reaching this handler is the
 * browser talking on its own behalf — telemetry, component updates, safe-browsing
 * — which is a fact about the browser build rather than about this page, and
 * counting it would fail the suite on a runner with a real network while passing
 * on one without.
 */
const PAGE_INITIATED = new Set([
  'document',
  'script',
  'stylesheet',
  'image',
  'font',
  'fetch',
  'xhr',
  'media',
  'manifest',
]);

export async function blockExternalRequests(page: Page): Promise<string[]> {
  const blocked: string[] = [];
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    const local = url.hostname === '127.0.0.1' || url.hostname === 'localhost';
    if (local || url.protocol === 'data:' || url.protocol === 'blob:') {
      // `fallback`, not `continue`: routes registered earlier — the private
      // dataset stub, for instance — must still get their turn. `continue`
      // would send the request to the server and silently serve the public
      // document to a test that thought it was exercising the private one.
      await route.fallback();
      return;
    }
    if (PAGE_INITIATED.has(route.request().resourceType())) {
      blocked.push(url.toString());
    }
    await route.abort();
  });
  return blocked;
}

export async function gotoView(page: Page, hash: string): Promise<void> {
  await page.goto(`/${hash}`);
  await expect(page.locator('main h1')).toBeVisible();
}

/**
 * Which dataset the page actually loaded, read from the page itself.
 *
 * The smoke suite runs against the public-safe build in the viewer workflow and
 * against the private build in the benchmark workflow. Asserting a hard-coded
 * mode would have made one of those two runs a test of the wrong thing, so the
 * assertions are about the shell agreeing with the document it loaded.
 */
export async function datasetMode(page: Page): Promise<'private' | 'public-safe'> {
  return page.evaluate(async () => {
    const response = await fetch('data/dataset.json');
    const dataset = (await response.json()) as { visibility_mode: 'private' | 'public-safe' };
    return dataset.visibility_mode;
  });
}

/**
 * Which matrix presentation this viewport actually gets.
 *
 * The table and the stacked cards are both in the DOM; a CSS media query
 * decides which is displayed. A test that reached for `.cell-button` on a phone
 * was reaching into the hidden half, so the presentation is resolved from what
 * is visible rather than from an assumption about the project name.
 */
export async function matrixPresentation(page: Page): Promise<'table' | 'stacked'> {
  return (await page.locator('.stacked-matrix').first().isVisible()) ? 'stacked' : 'table';
}

/**
 * The half of the matrix screen this viewport actually shows.
 *
 * Both presentations are in the DOM, so an unscoped `getByText` resolves to
 * whichever comes first in document order — which is the hidden one on a
 * desktop viewport. Every matrix assertion goes through this.
 */
export function visibleMatrix(page: Page): Locator {
  return page.locator('.only-wide, .only-narrow').locator('visible=true').first();
}

/**
 * Open one cell's evidence, in whichever presentation is on screen, and return
 * the element that now carries it.
 *
 * In the table that means clicking a cell button. In the stacked view every
 * implementation's evidence is already open — that is the point of it — so
 * there is nothing to click.
 */
export async function openFirstCellEvidence(page: Page): Promise<Locator> {
  if ((await matrixPresentation(page)) === 'table') {
    await page.locator('.only-wide .cell-button').first().click();
    const panel = page.locator('.evidence-panel');
    await expect(panel).toBeVisible();
    return panel;
  }
  const card = page.locator('.stacked-cell').first();
  await expect(card).toBeVisible();
  return card;
}

export const VIEWS = [
  { hash: '#/', heading: /MCP Failure Lab|Failure Lab/i, name: 'overview' },
  { hash: '#/matrix', heading: /Every implementation against every scenario/i, name: 'matrix' },
  { hash: '#/categories', heading: /Every benchmark category/i, name: 'categories' },
  { hash: '#/signatures', heading: /same result/i, name: 'result signature' },
  { hash: '#/capabilities', heading: /advertised/i, name: 'capability surface' },
  { hash: '#/containment', heading: /requested/i, name: 'containment' },
  { hash: '#/timings', heading: /Like-for-like/i, name: 'timings' },
  { hash: '#/reproduction', heading: /reduced to something/i, name: 'reproduction' },
  { hash: '#/methodology', heading: /What these figures mean/i, name: 'methodology' },
] as const;
