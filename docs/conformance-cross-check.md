# Official conformance cross-check

Pinned baseline:

```
@modelcontextprotocol/conformance@0.2.0-alpha.11
repository:    modelcontextprotocol/conformance
source commit: c321dd32035556e6769d3724a8ee97d87c3faaac
```

Installed via `tools/conformance/package.json`. Never vendored, never forked,
invoked as a subprocess. Its results are recorded in a **separate vocabulary**
(`ConformanceOutcome`) from Failure Lab findings (`Classification`); a unit test
asserts the two enums are disjoint so one can never be reported as the other.

## What the official framework already covers

At `--requirements 2026-07-28` it declares **69 required scenarios**, "frozen;
run at the 2026-07-28 wire", including `tools-list`, `tools-call-*`,
`resources-*`, `prompts-*`, `completion-complete`, `caching`,
`dns-rebinding-protection`, `server-stateless`, `server-sse-multiple-streams`,
and the `input-required-*` multi-round-trip family. It also runs
`wire-schema-valid`, validating every message against the official schema.

That is a large, well-built surface. Failure Lab does not reimplement any of it.

## Per-scenario cross-check

### `baseline.http_tools_list`

- **Official baseline:** fully covered by `tools-list`, `tools-name-format` and
  `wire-schema-valid`. Verified: 3/3 pass against `http.fixture.2026`.
- **Failure Lab addition:** none of substance. This scenario is retained only as
  the harness's own positive control, so a hostile scenario's failure can be
  distinguished from a broken harness.
- **Overlap:** total. **This is deliberately not a differentiator.**

### `version.http_header_body_mismatch`

- **Official baseline:** the official suite validates well-formed traffic and
  the wire schema. It does not send a request whose `Mcp-Method` header
  deliberately disagrees with the body.
- **Failure Lab addition:** the hostile condition itself, plus differential
  comparison against a conforming fixture, a minimised reproduction that
  preserves the mismatch, and one-command replay.
- **Overlap:** none observed at 0.2.0-alpha.11.

### `timing.http_slow_body`

- **Official baseline:** none observed. The suite tests protocol correctness,
  not deadline behaviour.
- **Failure Lab addition:** separates connect / header / first-byte /
  body-completion timeouts and attributes each. Evidence records
  `body-completion timeout after 2.5s (headers arrived at 1ms)`.
- **Overlap:** none observed.

### `recovery.http_connection_reset`

- **Official baseline:** none observed.
- **Failure Lab addition:** partial-response state, connection failure, and
  whether the next request succeeds — separating a transient reset from a
  wedged server.
- **Overlap:** none observed.

## What the official suite caught that Failure Lab did not

Running the full 2026-07-28 suite against `http.fixture.2026` produced
**22/41 checks passing**, and among the failures was
`localhost-host-rebinding-rejected`: the fixture did not validate the `Host`
header, leaving it open to DNS rebinding. None of this project's own scenarios
tested for that.

The fixture was fixed and both rebinding checks now pass. This is the clearest
argument for integration over duplication: the official suite found a real gap
this harness was blind to.

The remaining failures are unimplemented features — the fixture serves only
`tools/*` and `server/discover`, so `resources-*`, `prompts-*` and
`completion-complete` legitimately fail. That is a property of a deliberately
minimal fixture, not a finding.

## Division of labour

| Question | Owner |
|---|---|
| Does the implementation satisfy the dated requirement set? | Official conformance |
| Is every message schema-valid? | Official conformance |
| What happens under malformed, mismatched, delayed, or severed traffic? | Failure Lab |
| Do two implementations differ meaningfully on identical input? | Failure Lab |
| What is the smallest reproduction of this failure? | Failure Lab |
| Can the failure be replayed from an artifact alone? | Failure Lab |

If the official framework grows a scenario covering one of the right-hand rows,
the correct response is to delete the Failure Lab version and use theirs.
