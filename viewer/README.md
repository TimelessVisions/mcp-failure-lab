# Evidence viewer

**The source here is public. No dataset it renders is.** Every dataset is
generated into a git-ignored directory, so none is a tracked file and none can
reach a published tree. See
[`../docs/responsible-disclosure.md`](../docs/responsible-disclosure.md).

A static, offline evidence viewer built from the benchmark schema and scenario
library. React, TypeScript, Vite, no backend, no network. It renders one JSON
file and makes no other request at any point.

It has two builds, and they are not the same document. The **public-safe** build
renders _MCP Interoperability Evidence — Public-Safe Synthetic Demonstration_:
invented implementations, invented measurements, no third-party benchmark result
shown. The **private** build renders a real benchmark execution and is never
published. The dataset carries which of the two it is, and the exporter refuses
to write a public-safe document that still carries a private execution's name
— so a synthetic matrix cannot appear under a private benchmark's masthead.

## The one rule

**The frontend is not a source of truth.** Every value on a screen was read from
a benchmark artifact whose hash the execution manifest names, exported into a
typed dataset by `mcp-failure-lab viewer export`, and validated in the browser
against a schema generated from the exporter's own models.

Nothing is computed here. A rate assembled in a component is a rate no test in
this repository can hold to the benchmark's denominator rules, so a matrix cell
carries its own applicable denominator, its own repetition count and its own
limitations rather than looking any of them up.

## Two modes

|                                               | Private                  | Public-safe         |
| --------------------------------------------- | ------------------------ | ------------------- |
| Real target identities                        | yes                      | **no**              |
| Versions, commits, fingerprints, build hashes | yes                      | **no**              |
| Observed tool names                           | yes                      | positional labels   |
| Private finding packages                      | summarised, never opened | **absent entirely** |
| Harness commit, runner identity               | yes                      | **no**              |
| Guided walkthrough                            | yes                      | not offered         |
| Dataset may be published                      | never                    | synthetic only      |

A public-safe export from real third-party evidence **refuses** unless the owner
supplies `--approved-anonymization`. Replacing a repository name with
"Implementation A" does not make a row anonymous: language, transport and result
narrow the field in combination, and for any given language there are few enough
independent MCP servers that the combination identifies one.

## Running it

```bash
# From the repository root. The synthetic fixture invents three implementations
# that do not exist, so nothing here can leak anyone's identity.
uv run mcp-failure-lab viewer fixture --out artifacts/viewer-fixture
uv run mcp-failure-lab viewer export \
  --benchmark artifacts/viewer-fixture --mode public-safe --out viewer/public/data

npm --prefix viewer ci
npm --prefix viewer run dev
```

The private viewer is the same application pointed at a real benchmark output
directory with `--mode private`. It stays local: the dataset directory is
git-ignored, and no workflow deploys it. The synthetic fixture also exports in
private mode, which is how the private code path is exercised without private
subject matter:

```bash
uv run mcp-failure-lab viewer export \
  --benchmark artifacts/viewer-fixture --mode private --out artifacts/viewer-data-private
```

## Gates

```bash
npm --prefix viewer run schema:check   # the compiled validator matches the schema
npm --prefix viewer run typecheck      # strict, with noUncheckedIndexedAccess
npm --prefix viewer run lint
npm --prefix viewer run format:check
npm --prefix viewer run test           # Vitest: contract, semantics, privacy, rendering
npm --prefix viewer run e2e            # Playwright: desktop + mobile, axe, offline, keyboard
uv run mcp-failure-lab viewer scan viewer/dist   # leak scan over the built bundle
```

The privacy scan runs over the **built bundle**, not only the dataset. The gap
between the two — an inlined constant, a source map, a stray fixture — is exactly
where a leak survives a clean dataset scan.

Some development machines ship a Chromium under a different build number than
this Playwright expects. Point at it rather than downloading a second copy:

```bash
PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium-*/chrome-linux/chrome \
  npm --prefix viewer run e2e
```

## Generated files

Two files are generated and committed, each with a `--check` that fails the
build when it drifts from its source:

- `src/data/viewer-dataset.schema.json` — from the pydantic models, by
  `mcp-failure-lab viewer schema`.
- `src/data/validate-dataset.js` — compiled from that schema by
  `scripts/compile-schema.mjs`. Ajv's usual path builds its validator with
  `new Function`, which this page's Content-Security-Policy forbids; a viewer
  that renders evidence has no business evaluating strings as code.

## What the viewer will not do

- No overall score, no ranking, no ordering of implementations by outcome.
- No percentage where a denominator is stated — `4 of 11 applicable`, never `36%`.
- No median without every individual sample beside it.
- No omitted `NOT_MEASURED`. Every promised timing dimension appears, measured
  or not, with its reason.
- No merged requested/observed containment value.
- No private payload, in either mode.
