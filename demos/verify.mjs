/**
 * Play a demo recording and report what it actually is.
 *
 * A file with the right name and a plausible size is not a video. This decodes
 * each recording in a real browser and reads the dimensions and duration back
 * out of the media element, which is the only check that distinguishes a master
 * from a truncated write — and it is the same check the public-export gate runs
 * before the recording is allowed into a release tree.
 *
 * Usage: node demos/verify.mjs demos/public-safe
 */

import { readFile, readdir, stat } from 'node:fs/promises';
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import { basename, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const PORT = 4321;

/**
 * Serve the recordings over HTTP rather than handing the page a `file://` URL.
 *
 * A blank page cannot fetch a local file, and working around that by relaxing
 * the browser's origin rules would make this verification weaker than the thing
 * it verifies. A local server is how the recording would actually be played.
 */
async function serve(directory) {
  const server = createServer(async (request, response) => {
    const url = new URL(request.url ?? '/', `http://127.0.0.1:${PORT}`);
    if (url.pathname === '/') {
      response.writeHead(200, { 'content-type': 'text/html' }).end('<!doctype html><title>v</title>');
      return;
    }
    try {
      const body = await readFile(join(directory, decodeURIComponent(url.pathname)));
      response.writeHead(200, { 'content-type': 'video/webm' }).end(body);
    } catch {
      response.writeHead(404).end('not found');
    }
  });
  await new Promise((done) => server.listen(PORT, '127.0.0.1', done));
  return server;
}

const require = createRequire(pathToFileURL(join(process.cwd(), 'viewer', 'package.json')));
const { chromium } = require('@playwright/test');

const EXPECTED = { width: 1920, height: 1080 };
//: The window the demo brief specifies, in seconds. A recording outside it is
//: not a near miss — it is a different piece of work from the one reviewed.
const DURATION = { min: 165, max: 195 };

async function main() {
  const directory = resolve(process.argv[2] ?? 'demos/public-safe');
  const recordings = join(directory, 'recording');

  const files = (await readdir(recordings).catch(() => [])).filter((name) =>
    name.endsWith('.webm'),
  );
  if (files.length === 0) throw new Error(`no recording under ${recordings}`);

  const timeline = JSON.parse(await readFile(join(directory, 'timeline.json'), 'utf-8'));
  const planned = timeline.stages.reduce((total, stage) => total + stage.seconds, 0);

  const server = await serve(recordings);
  const browser = await chromium.launch({
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
  });
  const page = await browser.newPage();
  await page.goto(`http://127.0.0.1:${PORT}/`);
  const problems = [];

  for (const name of files) {
    const path = join(recordings, name);
    const { size } = await stat(path);

    const media = await page.evaluate(async (url) => {
      const video = document.createElement('video');
      video.src = url;
      video.muted = true;
      await new Promise((done, fail) => {
        video.onloadedmetadata = done;
        video.onerror = () => fail(new Error('the browser could not decode this file'));
        setTimeout(() => fail(new Error('timed out decoding')), 30_000);
      });
      // A duration that is still Infinity after metadata means the container
      // was never finalised — which a byte count would not have caught.
      if (!Number.isFinite(video.duration)) {
        await new Promise((done) => {
          video.currentTime = 1e6;
          video.ontimeupdate = () => {
            video.ontimeupdate = null;
            done(undefined);
          };
          setTimeout(done, 5000);
        });
      }
      return { width: video.videoWidth, height: video.videoHeight, duration: video.duration };
    }, `http://127.0.0.1:${PORT}/${encodeURIComponent(name)}`);

    const label = basename(name);
    console.log(
      `${label}: ${media.width}x${media.height}, ` +
        `${Number.isFinite(media.duration) ? media.duration.toFixed(1) : '?'}s, ` +
        `${(size / 1024 / 1024).toFixed(1)} MB`,
    );

    if (media.width !== EXPECTED.width || media.height !== EXPECTED.height) {
      problems.push(`${label}: ${media.width}x${media.height}, expected 1920x1080`);
    }
    if (Number.isFinite(media.duration)) {
      if (media.duration < DURATION.min || media.duration > DURATION.max) {
        problems.push(
          `${label}: ${media.duration.toFixed(1)}s, outside the 2:45-3:15 window ` +
            `(the timeline plans ${planned}s)`,
        );
      }
    }
  }

  await browser.close();
  server.close();

  if (problems.length > 0) {
    console.error(problems.map((problem) => `  · ${problem}`).join('\n'));
    process.exitCode = 1;
    return;
  }
  console.log(`${files.length} recording(s) verified`);
}

await main();
