# Public-safe demo — runbook

This demo is safe to show publicly once it has been through the privacy review
at the bottom of this page. It contains no third-party evidence because there
was none in its input: the viewer it records is built from the synthetic
fixture.

## Building the viewer it records

```bash
uv run mcp-failure-lab viewer fixture --out artifacts/viewer-fixture

uv run mcp-failure-lab viewer export \
  --benchmark artifacts/viewer-fixture \
  --mode public-safe \
  --out viewer/public/data

# Both gates. The dataset is what the redaction produced; the bundle is what
# would actually be served, and the gap between them is where a leak survives a
# clean dataset scan.
uv run mcp-failure-lab viewer scan viewer/public/data/dataset.json
npm --prefix viewer run build
uv run mcp-failure-lab viewer scan viewer/dist
```

## Recording

```bash
node demos/capture.mjs --mode public-safe --dist viewer/dist --out demos/public-safe
node demos/verify.mjs demos/public-safe
```

`capture.mjs` refuses to run if the build serves anything other than a
public-safe dataset, aborts any request that leaves the page's own origin and
fails if one was attempted, and asserts every stage id against `timeline.json`.
It produces two 1920×1080 masters — standard and reduced-motion — each trimmed
to the timeline's length, and regenerates `captions.vtt` from that same
timeline.

`verify.mjs` decodes both files in a browser and reports their real dimensions
and duration.

Roughly seven minutes: each master is recorded in real time, twice.

## Determinism

Same build in, same route and focus sequence out. The fixture is generated
deterministically, the walkthrough advances only by `ArrowRight`, the dwell
times come from the committed timeline, and nothing in the page reaches a
network. The encoder's byte output is not reproducible; the content is.

If a capture fails with a stage-id mismatch, the walkthrough changed. Fix the
timeline and the script together — that failure is the mechanism working.

## Playing it

WebM (VP8), no audio. Load `captions.vtt` as a text track; `script.md` is what a
presenter says over it. Prefer the reduced-motion master anywhere the sample
strip's animation would distract.

## Privacy review before any public use

Run through this list against the actual recording, not against the process:

1. Play both masters end to end. Every stage that shows a number carries the
   synthetic note in-frame.
2. The opening and closing frames both state that no third-party benchmark
   result is shown.
3. No implementation name appears other than `Implementation A`, `B`, `C`.
4. No version, commit, digest, fingerprint or `ILAB-` identifier is legible in
   any frame.
5. No branch name, workflow run id, artifact URL or absolute path is visible.
6. The private benchmark's name does not appear. The schema lineage
   ("Benchmark 0.3 schema and scenario library") may — it is true, and it is a
   statement about the schema, not about the results.
7. `captions.vtt` contains none of the above either.
8. Confirm nothing in the recording implies a private finding exists.

The build carries most of these already — the dataset went through the denylist
scan, and the walkthrough is asserted to contain no commit-shaped or
finding-shaped string. Step 8 and the narration are the parts no scanner checks.

## Release

The public-safe recording may enter the sanitized export once this review has
been done and the owner has authorized publication. Publication is a separate
decision from passing the checks.
