# Official conformance boundary

A pinned, unmodified installation of the official MCP conformance framework.

```
@modelcontextprotocol/conformance@0.2.0-alpha.11
repository: modelcontextprotocol/conformance
source commit: c321dd32035556e6769d3724a8ee97d87c3faaac
```

## Rules

- **Not vendored.** No source from the official framework is copied into this
  repository. It is installed as a pinned dependency and invoked as a subprocess.
- **Not duplicated.** Failure Lab does not reimplement official happy-path
  checks, wire-schema validation, requirement-set logic, or expected-failure
  handling. Those belong to the official suite and are used as a baseline.
- **Not forked.** If a hostile scenario belongs upstream, it is prepared as a
  contribution draft and submitted only with explicit approval.

## Install

```bash
cd tools/conformance && npm ci
```

`npm ci` installs `package-lock.json` exactly and fails if the lockfile and
`package.json` disagree. `npm install` may resolve a newer transitive dependency,
which would silently change the baseline this project compares against.

## What the official suite covers

At 2026-07-28 it declares **69 required scenarios**, including `tools-list`,
`tools-call-*`, `resources-*`, `prompts-*`, `caching`, `dns-rebinding-protection`,
`server-stateless`, and the `input-required-*` multi-round-trip family.

Failure Lab's own scenarios that overlap this surface exist as the harness's
positive controls, not as competing checks. The differentiation is recorded per
scenario in `docs/conformance-cross-check.md`.
