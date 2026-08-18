import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { expect, test, type Page } from '@playwright/test';

import { blockExternalRequests, watchConsole } from './helpers';

/**
 * Guided mode, exercised against the private dataset.
 *
 * The private document is served by intercepting the one fetch this application
 * makes, rather than by building a second bundle. That keeps private data out of
 * every built artifact — including the ones a CI job uploads — while still
 * exercising the mode that only exists for it.
 *
 * `PRIVATE_DATASET` points at the synthetic fixture's private export by default,
 * so "private mode" here means the private code path rather than private subject
 * matter. The private viewer job in the benchmark workflow overrides it to the
 * dataset exported from the real Benchmark 0.3 artifact, and the assertions
 * below are written to hold for both: they check that each required stage
 * resolves to real evidence *or* states its absence, never that a particular
 * example exists.
 */
const PRIVATE_DATASET =
  process.env.PRIVATE_DATASET_PATH ??
  resolve(process.cwd(), '..', 'artifacts', 'viewer-data-private', 'dataset.json');

/**
 * The evidence a complete private walkthrough must account for.
 *
 * Mirrors `GUIDED_REQUIREMENTS` in the application. Duplicated deliberately: a
 * test that imported the list from the code under test would pass whatever that
 * list became, including empty.
 */
const REQUIREMENTS = [
  'stable-pass',
  'stable-spec-violation',
  'behavioural-divergence',
  'security-relevant-dimension',
  'not-applicable-with-reason',
  'not-measured-metric',
  'complete-result-signature',
  'containment-record',
  'minimised-reproduction',
  'timing-card-all-samples',
] as const;

const HOW_TO_GENERATE =
  `${PRIVATE_DATASET} is not present. Generate it with:\n` +
  '  uv run mcp-failure-lab viewer fixture --out artifacts/viewer-fixture\n' +
  '  uv run mcp-failure-lab viewer export --benchmark artifacts/viewer-fixture ' +
  '--mode private --out artifacts/viewer-data-private';

function privateDataset(): string | null {
  try {
    return readFileSync(PRIVATE_DATASET, 'utf-8');
  } catch {
    return null;
  }
}

/**
 * Skip, with the reason and the fix, when the dataset has not been generated.
 *
 * This used to throw. In the development repository that is fine — the dataset
 * is always there — but the whole of `e2e/` ships in the public export, where a
 * generated input is by definition absent until someone makes it. `npx
 * playwright test` therefore failed on a clean checkout for a reason that was
 * not a defect, which teaches a new reader that a red suite is normal here.
 *
 * A skip that names its input and prints the command that produces it is
 * honest; the assertions below are unchanged and still run the moment the
 * dataset exists.
 */
test.beforeEach(async ({ page }) => {
  const body = privateDataset();
  test.skip(body === null, HOW_TO_GENERATE);
  await page.route('**/data/dataset.json', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: body ?? '' });
  });
});

async function openWalkthrough(page: Page) {
  await page.goto('/#/');
  await expect(page.locator('main h1')).toBeVisible();
  await page.getByRole('button', { name: 'Guided walkthrough', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Guided walkthrough' });
  await expect(dialog).toBeVisible();
  return dialog;
}

test('the private build offers the walkthrough and states its mode', async ({ page }) => {
  const problems = watchConsole(page);
  await page.goto('/#/');
  await expect(page.locator('main h1')).toBeVisible();

  await expect(page.getByText('private', { exact: true }).first()).toBeVisible();
  await expect(
    page.getByRole('button', { name: 'Guided walkthrough', exact: true }),
  ).toBeVisible();
  expect(problems).toEqual([]);
});

test('the walkthrough runs end to end by keyboard, offline', async ({ page }) => {
  const problems = watchConsole(page);
  const blocked = await blockExternalRequests(page);

  const dialog = await openWalkthrough(page);
  const stages = await dialog.locator('.guided__tick').count();
  expect(stages).toBeGreaterThanOrEqual(10);
  await expect(dialog.getByText(new RegExp(`stage 1 of ${stages}`))).toBeVisible();

  for (let stage = 1; stage < stages; stage += 1) {
    await page.keyboard.press('ArrowRight');
    await expect(dialog.getByText(new RegExp(`stage ${stage + 1} of ${stages}`))).toBeVisible();
  }

  // The walkthrough ends on limitations, where a presentation would have stopped.
  await expect(dialog.getByText(/Methodology and limitations/i).first()).toBeVisible();

  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);

  expect(blocked).toEqual([]);
  expect(problems).toEqual([]);
});

/**
 * The assertion this whole phase exists for.
 *
 * Every required kind of evidence must be reached by walking the stages, and
 * each must land in one of exactly two states: a real example drawn from this
 * execution, or an explicit statement that this execution contains none. A
 * stage that renders nothing, or that quietly drops out of the script, fails
 * here — and it fails identically whether the dataset is the synthetic fixture
 * or the real Benchmark 0.3 export.
 */
test('every required stage resolves to real evidence or states its absence', async ({
  page,
}) => {
  const problems = watchConsole(page);
  const dialog = await openWalkthrough(page);
  const stages = await dialog.locator('.guided__tick').count();

  const resolved = new Map<string, string>();
  for (let index = 0; index < stages; index += 1) {
    const stage = dialog.locator('.guided__stage');
    await expect(stage).toBeVisible();

    const requirement = await stage.getAttribute('data-requirement');
    const evidence = await stage.getAttribute('data-evidence');
    if (requirement !== null) {
      expect(
        evidence,
        `stage "${requirement}" declared neither evidence nor an absence`,
      ).toMatch(/^(present|absent)$/);

      // An absent stage must actually say so on screen. A data attribute that
      // nothing renders would satisfy a test and tell a viewer nothing.
      if (evidence === 'absent') {
        await expect(stage.locator('[data-evidence="absent"]')).toBeVisible();
        await expect(stage.getByText(/No such evidence in this execution/i)).toBeVisible();
      } else {
        // A present stage must render something beyond its heading.
        const text = (await stage.textContent()) ?? '';
        expect(text.length).toBeGreaterThan(80);
      }
      resolved.set(requirement, evidence ?? '');
    }
    if (index < stages - 1) await page.keyboard.press('ArrowRight');
  }

  const missing = REQUIREMENTS.filter((requirement) => !resolved.has(requirement));
  expect(missing, `the walkthrough never reached: ${missing.join(', ')}`).toEqual([]);

  // Reported rather than asserted: which examples a given execution contains is
  // a fact about the measurements, not about the code.
  //
  // Printed as well as annotated. A category that this execution had no example
  // of is exactly the thing a reader of the job needs told — an annotation the
  // log does not carry leaves "every stage resolved" and "four stages found
  // nothing and said so" looking identical from the outside.
  const absent = [...resolved.entries()].filter(([, state]) => state === 'absent');
  const summary =
    absent.length === 0
      ? 'every required stage resolved to real evidence'
      : `stated absent in this execution: ${absent.map(([name]) => name).join(', ')}`;

  // `process.stdout.write` rather than `console.log`: this is a report written
  // deliberately to the job's output, not a debug message, and the lint rule
  // that forbids the latter is worth keeping everywhere rather than disabling
  // here.
  const lines = [...resolved.entries()]
    .sort()
    .map(
      ([requirement, state]) =>
        `  ${state === 'present' ? 'evidence' : 'absent  '}  ${requirement}`,
    );
  process.stdout.write(`guided evidence — ${summary}\n${lines.join('\n')}\n`);

  test.info().annotations.push({ type: 'guided-evidence', description: summary });

  expect(problems).toEqual([]);
});

test.describe('reduced motion', () => {
  test.use({ contextOptions: { reducedMotion: 'reduce' } });

  test('the walkthrough still advances with animation suppressed', async ({ page }) => {
    const dialog = await openWalkthrough(page);
    const stages = await dialog.locator('.guided__tick').count();

    await page.keyboard.press('End');
    await expect(dialog.getByText(new RegExp(`stage ${stages} of ${stages}`))).toBeVisible();
    await page.keyboard.press('Home');
    await expect(dialog.getByText(new RegExp(`stage 1 of ${stages}`))).toBeVisible();
  });
});

test('the private walkthrough names an actual example or says there is none', async ({
  page,
}) => {
  const dialog = await openWalkthrough(page);

  // Walk to the stable normative stage by name rather than by index, so adding
  // a stage ahead of it does not silently retarget the assertion.
  for (let index = 0; index < 20; index += 1) {
    const stage = dialog.locator('.guided__stage');
    if ((await stage.getAttribute('data-requirement')) === 'stable-spec-violation') break;
    await page.keyboard.press('ArrowRight');
  }

  const stage = dialog.locator('.guided__stage');
  expect(await stage.getAttribute('data-requirement')).toBe('stable-spec-violation');
  const text = (await stage.textContent()) ?? '';
  expect(text).toMatch(/SPEC_VIOLATION|No stable SPEC_VIOLATION exists/);
});
