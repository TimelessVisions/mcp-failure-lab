import { expect, test, type Locator, type Page } from '@playwright/test';

import {
  gotoView,
  matrixPresentation,
  openFirstCellEvidence,
  VIEWS,
  watchConsole,
} from './helpers';

/**
 * Layout assertions about things that were actually wrong.
 *
 * Every check here corresponds to a defect a reader hit: a transport name cut
 * off mid-word, a matrix whose implementation columns began off-screen on a
 * phone, and a page that could be scrolled sideways by accident. They are
 * measured against the rendered box rather than eyeballed, because "looks fine"
 * is exactly the standard that let a clipped value ship.
 */

/** True when an element's content is wider or taller than the box drawn for it. */
async function isClipped(locator: Locator): Promise<boolean> {
  return locator.evaluate((element) => {
    // 1px of tolerance: sub-pixel rounding on a scaled viewport is not a clip.
    return (
      element.scrollWidth > element.clientWidth + 1 ||
      element.scrollHeight > element.clientHeight + 1
    );
  });
}

async function pageOverflowsHorizontally(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const doc = document.documentElement;
    return doc.scrollWidth > doc.clientWidth + 1;
  });
}

test('the transport names are rendered in full, not clipped', async ({ page }) => {
  await gotoView(page, '#/');

  // The overview stat that carries them. `streamable_http` was being cut to
  // "streamable_h" inside a card with `overflow: hidden`.
  const card = page.locator('.stat', { hasText: 'Transports' }).first();
  await expect(card).toBeVisible();

  const value = card.locator('.stat__value');
  await expect(value).toBeVisible();
  expect(await isClipped(value), 'the transports value is clipped').toBe(false);
  expect(await isClipped(card), 'the transports card clips its content').toBe(false);

  // Rendered for a reader, and complete.
  await expect(card).toContainText('Streamable HTTP');
  await expect(card).toContainText('stdio');
  await expect(card).not.toContainText('streamable_http');
});

test('no view scrolls the page sideways', async ({ page }) => {
  for (const view of VIEWS) {
    await gotoView(page, view.hash);
    expect(
      await pageOverflowsHorizontally(page),
      `${view.name} overflows the viewport horizontally`,
    ).toBe(false);
  }
});

test('the matrix presents every implementation without a hidden table', async ({ page }) => {
  const problems = watchConsole(page);
  await gotoView(page, '#/matrix');

  const presentation = await matrixPresentation(page);

  if (presentation === 'stacked') {
    // The phone arrangement. Every implementation's result for the selected
    // scenario is on screen, stacked, with nothing behind a sideways gesture.
    const cards = page.locator('.stacked-cell');
    const count = await cards.count();
    expect(count).toBeGreaterThanOrEqual(3);

    for (let index = 0; index < count; index += 1) {
      const card = cards.nth(index);
      await expect(card).toBeVisible();
      // Each carries its own evidence, not just a colour.
      await expect(card.getByText('Applicable denominator')).toBeVisible();
      await expect(card.getByText('Repetitions')).toBeVisible();
      await expect(card.getByText('Signature stability')).toBeVisible();
      await expect(card.getByText('Transport')).toBeVisible();
    }

    // The scenario identity stays reachable, and the selector reaches them all.
    const selector = page.locator('#stacked-matrix-scenario');
    await expect(selector).toBeVisible();
    const options = await selector.locator('option').count();
    expect(options).toBeGreaterThan(10);

    // The wide table must not be what a phone is being asked to read.
    await expect(page.locator('.only-wide')).toBeHidden();
    expect(problems).toEqual([]);
    return;
  }

  // The desktop arrangement: a real table, with a header per implementation.
  const table = page.locator('.only-wide table');
  await expect(table).toBeVisible();
  const headers = table.locator('thead th');
  expect(await headers.count()).toBeGreaterThanOrEqual(5);
  await expect(page.locator('.stacked-matrix')).toBeHidden();
  expect(problems).toEqual([]);
});

test('a region that still scrolls sideways says so and keeps its row identity', async ({
  page,
}) => {
  await gotoView(page, '#/matrix');
  if ((await matrixPresentation(page)) === 'stacked') {
    test.skip(true, 'the stacked presentation has no horizontally scrolling region');
    return;
  }

  const region = page.locator('.matrix-wrap').first();
  await expect(region).toBeVisible();

  if ((await region.getAttribute('data-overflowing')) === 'true') {
    // A visible affordance, in words rather than a fading edge.
    await expect(page.getByText(/wider than the screen/i).first()).toBeVisible();
    // And the scenario identity stays put while the columns move.
    const rowHeader = region.locator('tbody th[scope="row"]').first();
    await expect(rowHeader).toHaveCSS('position', 'sticky');
  }

  // The header row is sticky whether or not it currently overflows.
  await expect(region.locator('thead th').first()).toHaveCSS('position', 'sticky');
});

test('not-measured labels name which domain they belong to', async ({ page }) => {
  await gotoView(page, '#/');
  // Scenario results and timing metrics are different things wearing the same
  // words. Neither may appear as a bare "Not measured".
  await expect(page.getByText('Scenario results not measured')).toBeVisible();
  await expect(page.getByText('Timing metrics not measured')).toBeVisible();

  await gotoView(page, '#/timings');
  await expect(
    page.getByRole('heading', { name: 'Timing metrics not measured', exact: true }),
  ).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Not measured', exact: true })).toHaveCount(0);
});

test('a security-oriented scenario is distinguishable from a security finding', async ({
  page,
}) => {
  await gotoView(page, '#/matrix');

  const evidence = await openFirstCellEvidence(page);
  const dimension = evidence.locator('.security-dimension').first();
  await expect(dimension).toBeVisible();

  // Three separate records, never collapsed into one verdict.
  await expect(dimension.getByText('Scenario purpose')).toBeVisible();
  await expect(dimension.getByText('Observation in this run')).toBeVisible();
  await expect(dimension.getByText('Vulnerability')).toBeVisible();

  // Whatever this particular cell is, the vulnerability line must never assert
  // one on the strength of the scenario's purpose alone.
  const text = (await dimension.textContent()) ?? '';
  if (/security-oriented/.test(text) && /no security-relevant observation/.test(text)) {
    expect(text).toMatch(/not assessed/);
  }
});
