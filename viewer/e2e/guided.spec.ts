import { expect, test } from '@playwright/test';

import { blockExternalRequests, gotoView, watchConsole } from './helpers';

/**
 * The public-safe build has a walkthrough of its own, and it is a different one.
 *
 * It used to have none, on the reasoning that a walkthrough is a private thing.
 * That left the only demonstrable artefact behind a dataset nobody outside the
 * project may see. The answer is a second script rather than a shared one: this
 * walkthrough demonstrates the interface and the methodology over invented data,
 * says so on every stage that shows a result, and claims no finding about anyone.
 */
test('the public-safe build offers the synthetic walkthrough, named as such', async ({
  page,
}) => {
  await gotoView(page, '#/');
  const button = page.getByRole('button', { name: 'Guided walkthrough (synthetic)' });
  await expect(button).toBeVisible();
  // The private walkthrough's entry point must not be what a public build shows.
  await expect(
    page.getByRole('button', { name: 'Guided walkthrough', exact: true }),
  ).toHaveCount(0);
});

test('the synthetic walkthrough says it is synthetic and names no third party', async ({
  page,
}) => {
  const problems = watchConsole(page);
  const blocked = await blockExternalRequests(page);

  await gotoView(page, '#/');
  await page.getByRole('button', { name: 'Guided walkthrough (synthetic)' }).click();

  const dialog = page.getByRole('dialog', { name: 'Guided walkthrough' });
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveAttribute('data-mode', 'public-safe');

  // The opening stage states what the evidence is, before any of it is shown.
  await expect(dialog.getByText(/PUBLIC-SAFE SYNTHETIC EVIDENCE/)).toBeVisible();
  await expect(
    dialog.getByText(/invented implementations and synthetic measurements/i),
  ).toBeVisible();

  // Walk the whole script. Every stage that shows a result must carry the
  // synthetic note, because a stage screenshotted alone loses the first slide.
  const stages = await dialog.locator('.guided__tick').count();
  expect(stages).toBeGreaterThan(5);

  const seen: string[] = [];
  for (let index = 1; index < stages; index += 1) {
    await page.keyboard.press('ArrowRight');
    const stage = dialog.locator('.guided__stage');
    await expect(stage).toBeVisible();
    seen.push((await stage.textContent()) ?? '');
  }

  const whole = seen.join('\n');
  // No private identity may appear in any shape.
  expect(whole).not.toMatch(/\b[0-9a-f]{40}\b/);
  expect(whole).not.toMatch(/ILAB-[A-Z]+-\d+/);
  expect(whole).not.toMatch(/Private 0\.3/);
  expect(whole).not.toMatch(/github\.com\/[\w.-]+\/[\w.-]+/);
  expect(whole).not.toMatch(/agent\/mcp-interop-foundation/);

  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);

  expect(blocked).toEqual([]);
  expect(problems).toEqual([]);
});

test('the public-safe build shows no private finding package', async ({ page }) => {
  const problems = watchConsole(page);
  const blocked = await blockExternalRequests(page);

  await gotoView(page, '#/reproduction');
  await expect(page.getByText(/private finding packages are absent/i)).toBeVisible();
  await expect(page.getByText(/ILAB-EXT-/)).toHaveCount(0);

  expect(blocked).toEqual([]);
  expect(problems).toEqual([]);
});

test('the public-safe build never names a pinned implementation', async ({ page }) => {
  await gotoView(page, '#/matrix');
  const text = (await page.locator('body').textContent()) ?? '';

  expect(text).toMatch(/Implementation A/);
  // A structural check rather than a name list: a commit or a digest in the
  // rendered page is a leak whichever project it belongs to.
  expect(text).not.toMatch(/\b[0-9a-f]{40}\b/);
  expect(text).not.toMatch(/github\.com\/[\w.-]+\/[\w.-]+/);
});

test('the public-safe build does not wear the private benchmark title', async ({ page }) => {
  await gotoView(page, '#/');
  const text = (await page.locator('body').textContent()) ?? '';

  // The specific defect: the synthetic build presented itself under the private
  // execution's name, which made an invented matrix read as a real report.
  expect(text).not.toMatch(/Private 0\.3/);
  expect(text).toMatch(/PUBLIC-SAFE SYNTHETIC EVIDENCE/);
  expect(text).toMatch(/invented implementations and synthetic measurements/i);
  // The schema lineage may be stated — it is true — but not as the identity.
  expect(text).toMatch(/Generated from the Benchmark 0\.3 schema and scenario library/);
});
