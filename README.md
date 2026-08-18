# MCP Failure Lab

> Apache-2.0. Not published to any package registry — installed from source.

This is not another static MCP scanner or generic agent-evaluation dashboard.

It runs identical hostile protocol scenarios across Model Context Protocol
implementations, compares their behaviour, reduces failures to the smallest
reproduction, and emits evidence you can hand to someone else.

Four things it does that a happy-path conformance run does not:

- **Version-aware testing.** Scenarios are gated to dated specification
  revisions. A 2026-07-28 requirement is never asserted against a 2025-11-25
  target — it is reported `NOT_APPLICABLE`, with the reason recorded.
- **Official conformance as the baseline, not a competitor.** The official suite
  is pinned and invoked as a subprocess; its results are recorded in a separate
  vocabulary and never merged with this harness's own findings. See
  [`docs/conformance-cross-check.md`](docs/conformance-cross-check.md).
- **Deterministic fault injection** at explicit wire and process boundaries.
- **Differential comparison** that separates "differs" from "differs
  meaningfully" from "violates a dated requirement".
- **Automatic minimisation** that preserves the *failure signature*, not just
  the classification label.

## Names

One project, several identifiers, each doing a different job:

| | |
|---|---|
| Product | **MCP Failure Lab** |
| Distribution and CLI | `mcp-failure-lab` |
| Python import package | `interop_lab` |

`interop_lab` is an internal identifier and has deliberately not been renamed:
renaming it would rewrite every import and every artifact path that records a
module, for no reader's benefit. The distribution and the command carry the
public name.

## Status

Working end to end on stdio and Streamable HTTP, with the official conformance
suite pinned as a baseline. See [Limitations](#limitations) before drawing
conclusions from anything it prints.

| Area | State |
|---|---|
| Transports | stdio ✅ · Streamable HTTP ✅ |
| Profiles | `mcp-2025-11-25` ✅ · `mcp-2026-07-28` ✅ |
| Scenarios | 32 deterministic — 17 in the stdio library, 15 in the Streamable HTTP library |
| Public targets | official Python SDK ✅ · official TypeScript SDK ✅ · 5 local stdio fixtures ✅ · 12 local HTTP fixtures ✅ |
| Official conformance | `@modelcontextprotocol/conformance@0.2.0-alpha.11`, pinned, invoked as a baseline ✅ |
| Replay · Differential · Minimisation | ✅ on both transports |
| Reports | terminal ✅ · JSON ✅ · JSONL trace ✅ · JUnit ✅ · SARIF 2.1.0 generation ✅ |
| SARIF offline schema validation | ❌ — the copied OASIS schema is not redistributed ([License](#license)) |
| CI gate | baseline regression gate ✅ · GitHub Actions workflows ✅ |
| Docker containment | stdio ✅ · Streamable HTTP ✅ isolated sidecar, no published port · both verified by hostile probes |
| Contained HTTP execution | ✅ `mcp-failure-lab run` selects the sidecar path automatically · 2025 stateful and 2026 stateless |
| Static evidence viewer | ✅ offline, no backend — public-safe synthetic mode ships here ([`viewer/`](viewer/)) |
| Public-safe demo | ✅ deterministic recording over synthetic evidence ([`demos/public-safe/`](demos/public-safe/)) |
| Benchmark methodology | ✅ code and schema ship ([`benchmarks/`](benchmarks/)) · comparative third-party results do not |

## Quick start

```bash
uv sync --all-extras --dev
# `npm ci` — not `npm install` — so the pinned lockfile is installed exactly and
# a reproduction cannot silently drift onto a newer transitive dependency.
npm ci --prefix targets/typescript   # pinned TypeScript SDK target
npm ci --prefix tools/conformance    # pinned official conformance framework

uv run mcp-failure-lab list-profiles
uv run mcp-failure-lab list-scenarios

# Run a suite against the official Python SDK and the reference fixture
uv run mcp-failure-lab run \
  -t targets/python/sdk-server.yaml \
  -t targets/fixtures/reference.yaml \
  --suite hostile

# Compare two implementations on identical scenarios
uv run mcp-failure-lab diff \
  --left targets/python/sdk-server-2025.yaml \
  --right targets/typescript/sdk-server.yaml \
  --suite core

# Run the official conformance suite as a baseline (separate vocabulary)
uv run mcp-failure-lab conformance -t targets/http/fixture-2026.yaml --scenario tools-list

# Reduce a failure to its smallest reproduction and package it
uv run mcp-failure-lab minimize \
  -t targets/fixtures/faulty-result-type.yaml \
  --scenario version.result_type_required \
  --finding-id ILAB-0001

# Reproduce it later, from the package alone
uv run mcp-failure-lab replay artifacts/repros/ILAB-0001/repro.yaml

# Gate a matrix against an approved baseline (exit 1 on regression)
uv run mcp-failure-lab ci \
  --baseline baselines/mvp.json \
  --matrix targets/mvp-matrix.yaml \
  --suite suites/release_gate/mvp.yaml \
  --junit artifacts/gate/junit.xml \
  --sarif artifacts/gate/results.sarif

# Build the synthetic evidence viewer, end to end, with no private input
uv run mcp-failure-lab viewer fixture --out artifacts/viewer-fixture
uv run mcp-failure-lab viewer export \
  --benchmark artifacts/viewer-fixture --mode public-safe --out viewer/public/data
npm ci --prefix viewer && npm run --prefix viewer build
```

See [`docs/ci-gate.md`](docs/ci-gate.md) for the baseline workflow and the rules
the gate will not bend.

## What this public distribution proves

Everything in this list can be checked by running the commands above, in this
tree, with no private input:

- The harness runs 32 deterministic scenarios over stdio and Streamable HTTP.
- Scenario applicability is decided by dated protocol profile, and an
  inapplicable scenario is reported `NOT_APPLICABLE` with its reason, never
  silently passed.
- A failure can be reduced to a minimal reproduction and re-executed from the
  reproduction package alone.
- Two implementations can be run over identical scenarios and their behaviour
  compared, with meaningful divergence separated from expected difference.
- JUnit and SARIF 2.1.0 are generated, and a CI gate fails on regression against
  an approved baseline.
- Targets this project did not write run under containment, or do not run.
- The evidence viewer builds, renders one JSON document offline, and makes no
  network request.
- The export boundary is enforced by an allowlist with tests, and the sanitized
  tree installs, lints, type-checks, tests, builds and verifies on its own.

## What it does not prove

- **Nothing about any specific third-party implementation.** No comparative
  result, no ranking, no finding, and no target-specific outcome is present in
  this distribution or derivable from it.
- **Not correctness.** Passing 32 scenarios means those 32 scenarios passed.
- **Not security.** A `SECURITY_RELEVANT` classification is an observation that
  warrants review, not a vulnerability.
- **Not certification, endorsement, or completeness.** This project is not
  affiliated with, endorsed by, or speaking for the Model Context Protocol
  specification or any implementation of it.
- **Not a survey of the ecosystem.** The scenario battery is narrow and the
  transports are two.

## What is not in this distribution

Stated up front so a missing path reads as a decision rather than a bug:
findings against third-party implementations, comparative benchmark results, the
external target definitions those were produced against, the private viewer
dataset, the private demo, internal commercial documents, and the copied OASIS
SARIF schema. The harness, its tests, its fixtures, its documentation, the
benchmark methodology code, the viewer source and the public-safe demo are all
here. See [License](#license).

Private comparative measurements and third-party findings are deliberately
absent from this distribution. The public viewer and demo use synthetic evidence
only — invented implementations and invented measurements, authored for
demonstration. They are not anonymised or redacted private evidence, and no
private result can be recovered from them.

## How it works

```
                              ┌─> StdioSession ─────────> subprocess, contained
target YAML ─> ScenarioExecutor┤
                      │       ├─> StreamableHttpSession ─> localhost fixture
                      │       │                            (direct, loopback only)
                      │       └─> ProbeSidecar ──────────> contained HTTP target
                      │                    │               (no published port)
                      │                    └──> WireTrace (JSONL, redacted)
                      │
                      ├──> assertions ──> Classification
                      ├──> normalisation ──> differential comparison
                      └──> minimiser ──> reproduction package

official conformance ──> ConformanceRun (separate vocabulary, never merged)
```

- **Profiles** (`profiles/registry.py`) encode what each dated revision requires.
  Facts are derived from the official JSON schemas, cited in
  [`docs/protocol-profiles.md`](docs/protocol-profiles.md).
- **Scenarios** are declarative data, which is what makes replay exact and
  minimisation possible.
- **Two HTTP paths, chosen by the target, never by a flag.** A local fixture is
  driven directly over loopback. A contained target is driven by a probe sidecar
  on a private network: the target publishes no port, has no default route, and
  the harness never opens a socket to it. `mcp-failure-lab run` selects the path
  and refuses every combination it does not implement — there is no fallback to
  a host socket, and none to host execution when Docker is unavailable.
- **Artifacts are the source of truth.** Every run embeds its fully resolved
  target, so a result stays reproducible after the YAML is edited.

## Classification vocabulary

`PASS` · `SPEC_VIOLATION` · `INVALID_WIRE_MESSAGE` · `CRASH` · `HANG` ·
`TIMEOUT` · `RECOVERY_FAILURE` · `CLEANUP_FAILURE` · `SECURITY_RELEVANT` ·
`BEHAVIORAL_DIVERGENCE` · `EXPECTED_DIFFERENCE` · `HARNESS_ERROR` ·
`INCONCLUSIVE` · `NOT_APPLICABLE`

The distinctions are the point. A divergence is not automatically a defect. A
security-relevant behaviour is not automatically a vulnerability. An unsupported
scenario is `NOT_APPLICABLE`, never `PASS`. An ambiguous result is
`INCONCLUSIVE`. A timeout records *whose* deadline expired.

Every finding is reported as **fact → normative basis → interpretation →
limitation → reproduction**.

## Safety boundaries

Targets are treated as untrusted:

- Local, explicitly authorised targets only. No Internet scanning.
- Environment is built from an allowlist, never inherited. A credential-shaped
  variable is refused even if a caller explicitly asks for it.
- Structured redaction of secrets before anything is written to an artifact.
- Full process-tree termination via process groups, SIGTERM then SIGKILL, with
  cleanup failure recorded rather than ignored.
- Bounded output capture.
- **Containment for code this project did not write.** Such code runs in a
  container or not at all — enforced in the model, so a target file declaring
  `provenance: external` with host execution fails to load. Images are pinned by
  digest; the run record carries what was *observed* via `docker inspect`
  alongside what was requested, and a container missing any requested control
  makes the run unusable as evidence rather than a target verdict.

This is not a claim that the harness is a security boundary. Docker is not one
either — see [`docs/containment.md`](docs/containment.md) for exactly what each
mode denies and what it does not.

Findings against third-party implementations are private by default and stay
that way. See [`docs/responsible-disclosure.md`](docs/responsible-disclosure.md).

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest                        # full suite
uv run pytest -m "not integration"   # unit + property only
```

**Type checking, stated accurately:** `pyright` runs with
`typeCheckingMode = "strict"`, but six `Unknown*` diagnostics are currently
disabled — see the commented block in `pyproject.toml`. Re-enabling them
produces errors from untyped JSON boundaries (`dict[str, Any]`) and pydantic
`default_factory`. This is a strict baseline with documented exceptions, **not**
unmodified strict mode. No module is excluded to obtain a green result, no
error-level diagnostic is disabled, and tests are not weakened to pass. Typing
the JSON boundary properly is a tracked open item.

Some tests skip by construction in this tree: the four SARIF assertions that
need the withheld OASIS schema, and the export tests that need a `.git`
directory. Both print their reason rather than being deleted, so a test run
shows the gap instead of hiding it.

## Limitations

Read these before quoting any output.

- **Two transports, narrow coverage.** 32 scenarios — 17 applicable over stdio,
  21 over Streamable HTTP, with 6 applicable to both. Passing means "passed
  these", nothing more.
- **Applicability is uneven across revisions.** Of the 32, 19 apply to both
  dated profiles, 8 only to `mcp-2026-07-28`, and 5 only to `mcp-2025-11-25`. A
  target that negotiates one revision will therefore see a subset of the battery
  reported `NOT_APPLICABLE`, with the reason recorded. How large that subset is
  for any particular implementation is a property of that implementation and is
  not reported here.
- **Authorization is untested.** No OAuth, no token validation, no real
  credentials anywhere.
- **The prose specification was unreachable** during development (egress
  policy), so profile facts come from the official JSON schemas alone. Claims
  that would need the prose spec are marked UNCONFIRMED where they are used.
- **Fixtures with `faulty` in the name are synthetic defects** created to prove
  detection works. They are not defects in any third-party implementation and
  must never be reported as such.
- **SSE response framing, but no streaming, on the contained path.** The probe
  speaks HTTP/1.1 request/response and decodes an SSE-framed *response body*,
  because an implementation may frame every POST reply as `text/event-stream`,
  including `initialize`. It does not hold a connection open: the long-lived
  `GET` subscription, `Last-Event-ID` resumption and server-initiated messages
  are unimplemented and reported `NOT_APPLICABLE`. Nothing here should be read
  as support for the SSE transport.
- **A contained timeout is reported as `TIMEOUT`, never `HANG`.** Distinguishing
  the two needs to know whether the target's process was still alive at the
  deadline. The target lives in a container the probe cannot inspect, so that
  would be an inference presented as an observation.
- **SARIF is generated but cannot be schema-validated here.** The copied OASIS
  schema is not redistributed; see [License](#license). Generation is
  unaffected, four validation tests skip with that reason, and restoring the
  check locally takes one file.
- **The benchmark ships as methodology, not as results.** The runner, the
  schema and the vocabulary are here; comparative measurements of third-party
  implementations are not, and no overall ranking is produced by design. See
  [`benchmarks/README.md`](benchmarks/README.md).
- **The viewer's shipped evidence is synthetic.** Its dataset is generated by
  `mcp-failure-lab viewer fixture` and describes invented implementations. It
  demonstrates the document, not a measurement of anyone's software.
- **External implementations are exercised only in the private development
  program**, under containment and pinned by digest. Neither those target
  definitions nor anything observed against them is part of this distribution.

## License

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

This distribution redistributes no third-party code. Every dependency is
resolved by you, from your own registry, under its own license.
[`THIRD_PARTY_NOTICES`](THIRD_PARTY_NOTICES) records what those dependencies
are, and — more usefully — what is deliberately *not* here.

One thing is not here, and it costs you a check:

**The OASIS SARIF 2.1.0 JSON Schema is not included.** SARIF 2.1.0 output is
generated exactly as before and is unaffected. What is missing is the ability to
validate that output offline against the copied OASIS schema: the OASIS IPR
Policy is not an open source license, its terms for redistributing the schema
file have not been confirmed, and the file is therefore withheld rather than
shipped on an assumption. The four tests that need it skip with that reason
printed instead of being deleted, so a test run here shows the gap rather than
hiding it. [`docs/ci-gate.md`](docs/ci-gate.md#sarif-output-and-what-this-distribution-cannot-check-about-it)
says how to restore validation locally in one step.
