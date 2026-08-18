/**
 * Deterministic capture of a viewer walkthrough, and the captions that match it.
 *
 * Not a screen recording somebody made once. The route, the focus order and the
 * dwell on every stage come from a committed timeline, so two captures of the
 * same build differ only in the encoder's output — and the caption file is
 * generated from that same timeline, which is the only way captions and picture
 * cannot drift apart.
 *
 * There is nothing here that reaches a network. The page is served from a local
 * directory, every request that leaves its own origin is aborted, and the video
 * carries no font or asset the bundle did not already contain. A demo that
 * needed the internet would be demonstrating something other than the offline
 * viewer this is a demo of.
 *
 * Usage:
 *   node demos/capture.mjs --mode public-safe --dist viewer/dist --out demos/public-safe
 *   node demos/capture.mjs --mode private     --dist viewer/dist --out demos/private
 *
 * The private mode expects a viewer built from a real Benchmark 0.3 artifact.
 * See demos/private/runbook.md — it is not built on this machine by default,
 * and its recording is never committed.
 */

import { spawn } from 'node:child_process';
import { mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

/**
 * Playwright comes from the viewer's own locked dependencies.
 *
 * `demos/` deliberately has no `package.json` of its own: a second lockfile
 * would be a second version of the browser automation this repository already
 * pins, and the demo is supposed to be a recording of the viewer as built, not
 * of a slightly different one.
 */
const require = createRequire(pathToFileURL(join(process.cwd(), 'viewer', 'package.json')));
// `require`, not a dynamic `import`: the package is CommonJS, and importing it
// puts the named exports one level down under `default` on some Node versions.
const { chromium } = require('@playwright/test');

const PORT = 4319;
const VIEWPORT = { width: 1920, height: 1080 };

function arg(name, fallback) {
  const index = process.argv.indexOf(`--${name}`);
  if (index === -1 || index === process.argv.length - 1) return fallback;
  return process.argv[index + 1];
}

/** Serve a directory over plain HTTP. No framework, no build step, no network. */
async function serve(directory) {
  const types = {
    '.html': 'text/html',
    '.js': 'text/javascript',
    '.css': 'text/css',
    '.json': 'application/json',
    '.svg': 'image/svg+xml',
  };
  const server = createServer(async (request, response) => {
    const url = new URL(request.url ?? '/', `http://127.0.0.1:${PORT}`);
    const path = url.pathname === '/' ? '/index.html' : url.pathname;
    try {
      const body = await readFile(join(directory, path));
      const extension = path.slice(path.lastIndexOf('.'));
      response.writeHead(200, { 'content-type': types[extension] ?? 'application/octet-stream' });
      response.end(body);
    } catch {
      response.writeHead(404).end('not found');
    }
  });
  await new Promise((done) => server.listen(PORT, '127.0.0.1', done));
  return server;
}

function timecode(ms) {
  const total = Math.round(ms / 1000);
  const hours = String(Math.floor(total / 3600)).padStart(2, '0');
  const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, '0');
  const seconds = String(total % 60).padStart(2, '0');
  return `${hours}:${minutes}:${seconds}.000`;
}

/**
 * WebVTT generated from the same timeline the capture is driven by.
 *
 * Written here rather than maintained by hand, because a caption file that
 * drifts from the picture is worse than none: it states, with apparent
 * precision, that something is on screen when it is not.
 */
function captions(timeline) {
  const lines = ['WEBVTT', ''];
  let at = 0;
  for (const [index, stage] of timeline.stages.entries()) {
    const end = at + stage.seconds * 1000;
    lines.push(String(index + 1));
    lines.push(`${timecode(at)} --> ${timecode(end)}`);
    for (const caption of stage.caption) lines.push(caption);
    lines.push('');
    at = end;
  }
  return lines.join('\n');
}

/**
 * Cut the recording to the length the timeline plans, by stream copy.
 *
 * The browser stops emitting frames when nothing repaints, and with motion
 * suppressed the final stage repaints not at all — so the container ends up
 * advertising twenty seconds of held still frame that nobody walked through.
 * Both passes walk the same 188 seconds; only the file disagreed, and a player
 * showing 3:28 for a three-minute demo is a real defect in the deliverable.
 *
 * A stream copy, never a re-encode: the frames that ship are the frames the
 * browser drew, and the trim removes trailing time rather than touching
 * picture. The ffmpeg used is the one Playwright already installs, so this adds
 * no dependency of its own.
 */
async function ffmpegBinary() {
  if (process.env.DEMO_FFMPEG) return process.env.DEMO_FFMPEG;
  const root = process.env.PLAYWRIGHT_BROWSERS_PATH;
  if (root) {
    for (const entry of await readdir(root).catch(() => [])) {
      if (entry.startsWith('ffmpeg-')) return join(root, entry, 'ffmpeg-linux');
    }
  }
  throw new Error(
    'no ffmpeg found. Set DEMO_FFMPEG to the binary Playwright installed, or ' +
      'PLAYWRIGHT_BROWSERS_PATH to the directory holding it.',
  );
}

async function trim(source, target, seconds) {
  const binary = await ffmpegBinary();
  const args = [
    '-hide_banner',
    '-loglevel',
    'error',
    '-y',
    '-i',
    source,
    '-t',
    String(seconds),
    '-c',
    'copy',
    target,
  ];
  await new Promise((done, fail) => {
    const child = spawn(binary, args, { stdio: ['ignore', 'inherit', 'inherit'] });
    child.on('error', fail);
    child.on('exit', (code) =>
      code === 0 ? done(undefined) : fail(new Error(`ffmpeg exited ${code}`)),
    );
  });
}

async function capture({ timeline, mode, reducedMotion, videoDir }) {
  const browser = await chromium.launch({
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
    args: ['--force-device-scale-factor=1', '--hide-scrollbars'],
  });
  const context = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: 1,
    reducedMotion: reducedMotion ? 'reduce' : 'no-preference',
    recordVideo: { dir: videoDir, size: VIEWPORT },
  });
  const page = await context.newPage();

  // Anything leaving this origin is a defect in the build, not something to
  // record around. Aborted, and reported at the end.
  const escaped = [];
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname === '127.0.0.1' || url.hostname === 'localhost') {
      await route.fallback();
      return;
    }
    escaped.push(url.toString());
    await route.abort();
  });

  await page.goto(`http://127.0.0.1:${PORT}/#/`);
  await page.locator('main h1').waitFor();

  // The document must be the one this demo is for. A private script over a
  // public-safe dataset, or the reverse, would be a demo of the wrong thing.
  const loaded = await page.evaluate(async () => {
    const response = await fetch('data/dataset.json');
    return (await response.json()).visibility_mode;
  });
  if (loaded !== mode) {
    throw new Error(
      `--mode ${mode} was asked for and the build serves a ${loaded} dataset. ` +
        `Build the viewer from the matching export first.`,
    );
  }

  const button = page.getByRole('button', {
    name: mode === 'private' ? 'Guided walkthrough' : 'Guided walkthrough (synthetic)',
    exact: true,
  });
  await button.click();
  const dialog = page.getByRole('dialog', { name: 'Guided walkthrough' });
  await dialog.waitFor();

  const observed = [];
  for (const [index, stage] of timeline.stages.entries()) {
    const element = dialog.locator('.guided__stage');
    await element.waitFor();
    const id = await element.getAttribute('data-stage-id');
    observed.push(id);
    if (id !== stage.id) {
      throw new Error(
        `stage ${index + 1} of the timeline expects "${stage.id}" and the walkthrough ` +
          `showed "${id}". The timeline and the script have diverged.`,
      );
    }
    await page.waitForTimeout(stage.seconds * 1000);
    if (index < timeline.stages.length - 1) await page.keyboard.press('ArrowRight');
  }

  // Closed before the context so the recording stops at the end of the last
  // stage rather than while the context is still tearing down.
  const walked = Date.now();
  await page.close();
  await context.close();
  await browser.close();
  return { escaped, observed, walked };
}

async function main() {
  const mode = arg('mode', 'public-safe');
  const dist = resolve(arg('dist', 'viewer/dist'));
  const out = resolve(arg('out', `demos/${mode}`));

  const timeline = JSON.parse(await readFile(join(out, 'timeline.json'), 'utf-8'));
  const planned = timeline.stages.reduce((total, stage) => total + stage.seconds, 0);
  console.log(`${mode}: ${timeline.stages.length} stages, ${planned}s planned`);
  if (planned < 165 || planned > 195) {
    throw new Error(`the timeline runs ${planned}s; the demo must land between 2:45 and 3:15`);
  }

  const recordings = join(out, 'recording');
  await rm(recordings, { recursive: true, force: true });
  await mkdir(recordings, { recursive: true });

  const server = await serve(dist);
  try {
    for (const reduced of [false, true]) {
      const label = reduced ? 'reduced-motion' : 'standard';
      const dir = join(recordings, `.${label}`);
      await mkdir(dir, { recursive: true });

      const started = Date.now();
      const { escaped, observed, walked } = await capture({
        timeline,
        mode,
        reducedMotion: reduced,
        videoDir: dir,
      });
      if (escaped.length > 0) {
        throw new Error(`the page reached outside its own origin: ${escaped.join(', ')}`);
      }

      const [file] = await readdir(dir);
      const target = join(recordings, `${mode}-${label}-1920x1080.webm`);
      await trim(join(dir, file), target, planned);
      await rm(dir, { recursive: true, force: true });
      const elapsed = (walked - started) / 1000;
      console.log(
        `  ${label}: ${observed.length} stages captured, ${elapsed.toFixed(1)}s walked`,
      );
    }

    await writeFile(join(out, 'captions.vtt'), captions(timeline), 'utf-8');
    console.log(`  captions written from the same timeline`);
  } finally {
    server.close();
  }
}

await main();
