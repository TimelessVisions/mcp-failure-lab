import { defineConfig, devices } from '@playwright/test';

/**
 * The viewer is exercised as it would actually be served: a built bundle behind
 * a plain static file server, at a desktop viewport and a phone one.
 *
 * `python3 -m http.server` rather than `vite preview`, deliberately. This is the
 * environment the viewer is built for — a directory of files and a server that
 * knows nothing about it — and any build tooling left in the loop can paper over
 * a failure that only appears once the bundle is handed to someone. It also
 * removes a dependency from the one test that is supposed to prove there are
 * none.
 */
/**
 * Use a Chromium that is already on the machine, when one is named.
 *
 * CI installs the browser this Playwright version expects and sets nothing, so
 * the default path is taken there. Some development environments ship a
 * pre-installed Chromium under a different build number, and downloading a
 * second copy of a browser to run a static-page test is not a good trade —
 * `PLAYWRIGHT_CHROMIUM_EXECUTABLE` points at the one already present.
 */
const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE;
const browser = executablePath === undefined ? {} : { launchOptions: { executablePath } };

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : '50%',
  // `list` in CI as well as the annotations. The GitHub reporter buffers until
  // the end, so a systemic failure shows up as twenty silent minutes followed by
  // a wall of text; the list reporter names each test as it resolves.
  reporter: process.env.CI ? [['list'], ['github'], ['html', { open: 'never' }]] : [['list']],
  // A systemic failure — the dataset not loading, the server not serving — fails
  // every test the same way. Stopping after a handful reports it in a minute
  // instead of burning the job's whole timeout to say it thirty-six times.
  maxFailures: process.env.CI ? 5 : 0,
  timeout: 45_000,
  expect: { timeout: 10_000 },

  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'retain-on-failure',
    // The page must never reach a network it does not have. Anything that tried
    // would be a defect, so the trace is kept rather than the request allowed.
    bypassCSP: false,
  },

  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], ...browser } },
    { name: 'mobile', use: { ...devices['Pixel 7'], ...browser } },
  ],

  webServer: {
    command: 'npm run build && python3 -m http.server 4173 --bind 127.0.0.1 --directory dist',
    url: 'http://127.0.0.1:4173/index.html',
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
    stdout: 'pipe',
    stderr: 'pipe',
  },
});
