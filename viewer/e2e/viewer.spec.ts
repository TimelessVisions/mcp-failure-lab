import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';

import {
  blockExternalRequests,
  datasetMode,
  gotoView,
  matrixPresentation,
  openFirstCellEvidence,
  VIEWS,
  watchConsole,
} from './helpers';

test.describe('every view', () => {
  for (const view of VIEWS) {
    test(`${view.name} renders offline with no console error`, async ({ page }) => {
      const problems = watchConsole(page);
      const blocked = await blockExternalRequests(page);

      await gotoView(page, view.hash);
      await expect(page.locator('main h1')).toHaveText(view.heading);

      expect(blocked, `the page reached outside its own origin: ${blocked.join(', ')}`).toEqual(
        [],
      );
      expect(problems).toEqual([]);
    });
  }
});

test('the shell states which dataset it is showing', async ({ page }) => {
  await gotoView(page, '#/');
  // The mode badge must name the document that was loaded, whichever it is.
  // This spec covers the public-safe build and the private one, so the
  // expectation comes from the dataset rather than from a constant.
  const mode = await datasetMode(page);
  await expect(page.getByText(mode, { exact: true }).first()).toBeVisible();
});

test('the overview shows the absent states rather than omitting them', async ({ page }) => {
  const problems = watchConsole(page);
  await gotoView(page, '#/');

  await expect(page.getByText('Applicable results')).toBeVisible();
  await expect(page.getByText('outside every denominator')).toBeVisible();
  await expect(page.getByText('Scenario results not measured')).toBeVisible();
  await expect(page.getByText(/no overall score on this page/i)).toBeVisible();
  expect(problems).toEqual([]);
});

test('a matrix cell opens its evidence, denominator and limitations', async ({ page }) => {
  const problems = watchConsole(page);
  await gotoView(page, '#/matrix');

  const evidence = await openFirstCellEvidence(page);

  await expect(evidence.getByText('Applicable denominator')).toBeVisible();
  await expect(evidence.getByText('Signature stability')).toBeVisible();
  await expect(evidence.getByText(/what this does not establish/i).first()).toBeVisible();
  expect(problems).toEqual([]);
});

test('the timings screen shows the not-measured metrics', async ({ page }) => {
  const problems = watchConsole(page);
  await gotoView(page, '#/timings');

  await expect(
    page.getByRole('heading', { name: 'Timing metrics not measured', exact: true }),
  ).toBeVisible();
  await expect(page.getByText('Target startup').first()).toBeVisible();
  await expect(page.getByText('Time to first valid request').first()).toBeVisible();
  await expect(page.getByText(/no boundary exists/i).first()).toBeVisible();
  expect(problems).toEqual([]);
});

test('an unknown route resolves to the overview rather than to nothing', async ({ page }) => {
  const problems = watchConsole(page);
  await page.goto('/#/no-such-view');
  await expect(page.locator('main h1')).toBeVisible();
  expect(problems).toEqual([]);
});

test.describe('accessibility', () => {
  for (const view of VIEWS) {
    test(`${view.name} has no detectable WCAG A or AA violation`, async ({ page }) => {
      await gotoView(page, view.hash);
      const results = await new AxeBuilder({ page })
        .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
        .analyze();

      const summary = results.violations.map(
        (violation) => `${violation.id}: ${violation.nodes.length} node(s) — ${violation.help}`,
      );
      expect(summary, summary.join('\n')).toEqual([]);
    });
  }
});

test.describe('keyboard', () => {
  test('the skip link is the first stop and reaches the evidence', async ({ page }) => {
    await gotoView(page, '#/');
    await page.keyboard.press('Tab');
    await expect(page.locator('.skip-link')).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(page.locator('#main')).toBeVisible();
  });

  test('every navigation entry is reachable and operable by keyboard', async ({ page }) => {
    await gotoView(page, '#/');
    const links = page.locator('.nav-link');
    const count = await links.count();
    expect(count).toBeGreaterThanOrEqual(9);

    for (let index = 0; index < count; index += 1) {
      const link = links.nth(index);
      await link.focus();
      await expect(link).toBeFocused();
    }
  });

  test('a matrix cell can be operated without a pointer', async ({ page }) => {
    await gotoView(page, '#/matrix');
    if ((await matrixPresentation(page)) === 'stacked') {
      // The stacked presentation has no cell to operate: the scenario selector
      // is the control, and every implementation's evidence is already open.
      const selector = page.locator('#stacked-matrix-scenario');
      await selector.focus();
      await expect(selector).toBeFocused();
      await expect(
        page.locator('.stacked-cell').first().getByText('Applicable denominator'),
      ).toBeVisible();
      return;
    }
    const cell = page.locator('.only-wide .cell-button').first();
    await cell.focus();
    await expect(cell).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(
      page.locator('.evidence-panel').getByText('Applicable denominator'),
    ).toBeVisible();
  });

  test('the route change moves focus to the new heading', async ({ page }) => {
    await gotoView(page, '#/');
    await page.locator('.nav-link', { hasText: 'Timings' }).click();
    await expect(page.locator('main h1')).toBeFocused();
  });
});

test.describe('reduced motion', () => {
  test.use({ contextOptions: { reducedMotion: 'reduce' } });

  test('every view still renders its evidence with animation suppressed', async ({ page }) => {
    const problems = watchConsole(page);
    for (const view of VIEWS) {
      await gotoView(page, view.hash);
      await expect(page.locator('main h1')).toBeVisible();
    }
    expect(problems).toEqual([]);
  });

  test('the sample strip still reports its values to assistive technology', async ({
    page,
  }) => {
    await gotoView(page, '#/timings');
    const strip = page.locator('.samples').first();
    await expect(strip).toBeVisible();
    // Motion may be suppressed; the data behind it must not be.
    await expect(strip).toHaveAttribute('aria-label', /measured samples/);
  });
});
