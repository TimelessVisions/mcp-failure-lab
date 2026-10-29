# CI baseline regression gate

A baseline is an **approved** record of what a matrix currently does, including
failures that are known and accepted. The gate compares a fresh run against it.

## Commands

```bash
# Record what the matrix currently does. A reason is mandatory, and every
# failing result must be approved by name with its own justification.
uv run mcp-failure-lab baseline create \
  --matrix targets/mvp-matrix.yaml \
  --suite suites/release_gate/mvp.yaml \
  --out baselines/mvp.json \
  --reason "Initial approved synthetic baseline" \
  --by "you@example.com" \
  --approve-failure fixture.faulty_result_type:version.result_type_required \
  --approval-reason "Synthetic fixture, defect deliberately injected to prove detection"

# See what a baseline approves, and why.
uv run mcp-failure-lab baseline inspect baselines/mvp.json

# Gate a run against it.
uv run mcp-failure-lab ci \
  --baseline baselines/mvp.json \
  --matrix targets/mvp-matrix.yaml \
  --suite suites/release_gate/mvp.yaml \
  --json artifacts/gate/gate.json \
  --junit artifacts/gate/junit.xml \
  --sarif artifacts/gate/results.sarif
```

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | Gate passed |
| 1 | Target or behaviour regression |
| 2 | Configuration or baseline error |
| 3 | Harness or environment failure |

## Rules the gate will not bend

- **A baseline is never updated automatically.** Absorbing a new failure because
  it happened twice is how a regression gate stops detecting regressions.
- **A missing result is never a pass.** Neither is `NOT_APPLICABLE`.
- **Same label is not the same finding.** If the classification matches but a
  different set of assertions failed, that is a regression.
- **Fingerprints are compared.** If the target definition or the scenario's
  executable content changed, the baseline no longer describes what is being
  measured, and that needs approval.
- **An improvement is reported, not absorbed.** `SPEC_VIOLATION -> PASS` is
  surfaced for review; re-approve the baseline deliberately.
- **An unbaselined result is not a pass — not even when it is a `PASS`.** A
  scenario nobody approved is contributing to the verdict, and that widens the
  approved measurement surface. `unknown_result` defaults to `fail`; `warn`
  exists but must be chosen explicitly, and still records the finding.
- **A failure cannot enter a baseline on a blanket reason.** `baseline create`
  refuses to record any failing result unless it is named with
  `--approve-failure target:scenario` and given its own `--approval-reason`.
  "The build was red when I made the baseline" is exactly what the gate exists
  to prevent.

### Behavioural divergence

Configurable per baseline: `fail`, `warn` (default), or `allow_if_approved`.

`allow_if_approved` means *approved*, and the gate checks that the approval
actually exists. A divergence is allowed only when the baseline entry records
an `approved_differential_verdict` of `BEHAVIORAL_DIVERGENCE`, an
`approval_reason`, an `evidence` reference, **and** both fingerprints still
match — an approval describes a specific build of a specific scenario, not a
target's name. A divergence accompanied by a failing `SPEC_VIOLATION` assertion
is never approved: a dated requirement was violated, and the divergence label
does not cover that.

### A new security-relevant result

`fail_on_new_security_relevant_result` defaults to `true`, and it is checked
**independently of the classification** — including on runs whose classification
is entirely unchanged.

That independence is the whole point. A result's primary classification says what
the target did; whether the result touches a security property is a separate,
typed dimension recorded beside it. A run can keep its approved classification —
or move to one policy tolerates, such as an allowed `BEHAVIORAL_DIVERGENCE` —
while acquiring a security dimension it did not have, because a different
assertion inside the same scenario began failing or a scenario's security
declaration was widened. A gate comparing only classifications reports nothing
there and stays green.

The gate makes no security *claim* when it blocks. It quotes the category, states
the primary classification beside it, and records that `vulnerability_status` is
`unconfirmed`. Whether a newly security-relevant result is acceptable is a
judgement, and the gate's job is to stop until someone has made it.

### An approved result that became undecidable

`inconclusive_result` defaults to `fail`. `INCONCLUSIVE` is the harness saying it
could not reach a verdict, and the entry it replaces was an approved statement
about the target — so letting it through converts approved coverage into no
coverage while the gate stays green.

`PASS -> INCONCLUSIVE` always blocks. `NOT_APPLICABLE -> INCONCLUSIVE` blocks
when the entry's `capability_required` is set, meaning the original skip rested
on an observation: the entry recorded *"the target does not advertise this"*, and
an unresolved run has not re-established it. A declarative skip — wrong
transport, wrong profile, wrong role — needed no observation, so nothing was lost
by failing to make one, and that case does not block.

It is never reported as a skip. A skip states a reason about the target, and an
undecided run established none.

### Reproduction expectations

An entry may declare a `reproduction`: the package URI, the classification and
failing-assertion signature it must still demonstrate, a minimum number of
consecutive stable runs, and optionally the target and scenario fingerprints it
was approved against. The gate re-checks the committed package on every run and
reports `reproduction_missing`, `reproduction_unstable`, or
`reproduction_mismatch`. Evidence that has quietly stopped reproducing is worse
than no evidence, because it is still being cited.

### Official conformance outcome

Where an entry records an `official_conformance_outcome`, that outcome is a
required check. The gate compares it against the outcome supplied for that run
and reports `official_conformance_outcome_changed`. This stays in the official
framework's own vocabulary and is never merged into a Failure Lab
classification. If no conformance run was supplied, the gate reports
`official_conformance_unmeasured` with observed value `NOT_MEASURED` and the
run fails. A missing execution is not a pass and not a changed verdict.
Entries that record no official outcome do not demand one.

### SARIF output, and what this distribution cannot check about it

SARIF 2.1.0 is emitted in full: `--sarif` produces the same document it always
did, with the same stable rule ids, and nothing about generation depends on
having a schema on disk.

**Offline schema validation is not included in this distribution.** Development
validates emitted SARIF against a pinned local copy of the OASIS SARIF 2.1.0
JSON Schema, digest-verified before use, because shape assertions only check
what someone thought to check. That copy is not redistributed here: the OASIS
IPR Policy is not an open source license and its terms for redistributing the
schema file have not been confirmed, so the file is withheld rather than
shipped on an assumption. See [`THIRD_PARTY_NOTICES`](../THIRD_PARTY_NOTICES).

What this means concretely:

- `mcp-failure-lab ci --sarif …` works and is unaffected.
- `interop_lab.reporting.sarif.validate()` raises `SarifSchemaError` explaining
  the schema is absent. Ask `sarif.pinned_schema_available()` first if you need
  to branch on it.
- The four tests that validate against the schema are skipped, with that reason
  printed, rather than deleted — so a test run here shows exactly which check is
  missing instead of looking complete.

To restore validation locally, obtain
`Schemata/sarif-schema-2.1.0.json` from the `oasis-tcs/sarif-spec` repository
under OASIS's terms, place it at `schemas/sarif-2.1.0.json`, and the validation
path and its tests activate with no code change.

## What is fingerprinted

**Target:** id, implementation, version, commit SHA, transport, protocol
profile, launch command, environment. *Not* `runtime_version` — that is a
declaration about the environment, not the identity of the thing under test, and
including it would make every machine look like a drift.

**Scenario:** id, version, steps, faults, applicability. A prose edit to a title
does not invalidate a baseline; a change to what actually runs does.

**Capability surface:** the negotiated profile, the advertised tool names, each
tool's input- and output-schema hash, the server capability object, and the
observed transport features.

The third one exists because the first two do not cover it. Both are computed
from files in this repository, so neither moves when the *target* changes what it
advertises — an upstream release could add a tool, remove one, or re-specify one
that kept its name, and every approved entry would still compare clean while the
decision it records had been made against a surface that no longer existed.

A moved surface is `CAPABILITY_DRIFT`. It blocks by default, and it is never
converted into a verdict: a capability change is not a defect, and the gate does
not have the standing to decide whether a particular one is acceptable. Blocking
is how that decision reaches a person. The drift is *described* rather than
merely detected — which tool, which schema, which server capability, which
profile — because two hex strings that differ tell a reviewer something changed
and give them nothing to decide with. `GatePolicy.capability_drift = warn` keeps
the gate green for a matrix where the surface is expected to move, and the drift
is still reported either way.

A surface that could not be observed at all is `CAPABILITY_UNRESOLVED`, and it
always blocks. It is deliberately not `NOT_APPLICABLE`, which would state a
reason — that the target lacks the tool — that nobody established, and the
previously approved record is never reused in its place. A scenario the executor
skips on transport, profile or role is decided before any target is contacted, so
those runs carry no capability record by design and none is demanded of them.

### Which entries require an observation

`capability_required` is set from the scenario's applicability, and it covers
**three** declarations, not one:

| Declaration | Answered by |
|---|---|
| `requires_tools` | what the target advertised in `tools/list` |
| `requires_safe_tool_invocation` | the advertised surface *and* the approved contract's schema hash |
| `requires_transport_features` | a handshake — whether a session identifier was issued, and in which header |

It previously recognised only the first. The consequence was quiet and specific:
a safe-tool-call entry and a session-lifecycle entry both recorded
`capability_required: false`, so the gate skipped the capability comparison for
exactly the entries whose applicability had been decided by observing the server —
and a run that failed discovery could be compared against an approval whose
conditions it had not reproduced.

Everything else — profile, dated version, transport, role — is decided from the
target file and the profile registry, which a run always has, and needs no
traffic with the target at all.

**Not fingerprinted:** the builder's local image ID, its storage driver, the
build timestamp, and the target's declared `runtime_version`. Each describes one
execution rather than the thing under test, and including any of them would make
the same approved target fingerprint differently on every machine.

## Proving the gate can fail

A gate that cannot fail is not a gate. CI runs it twice on every push:

```bash
# Must exit 0
uv run mcp-failure-lab ci --baseline baselines/mvp.json \
  --matrix targets/mvp-matrix.yaml --suite suites/release_gate/mvp.yaml

# Must exit 1 — same target id, regressed implementation
uv run mcp-failure-lab ci --baseline baselines/mvp.json \
  --matrix suites/release_gate/regression-matrix.yaml \
  --suite suites/release_gate/mvp.yaml
```

The regression matrix swaps the conforming 2026 HTTP fixture for one that
ignores header/body disagreement. The gate reports both the fingerprint drift
and the `PASS -> SPEC_VIOLATION` new failure.

## Committed baseline

`baselines/mvp.json` covers four local fixtures across stdio and Streamable
HTTP. It records **28 results, two of which are approved synthetic failures**
(`fixture.faulty_result_type` and `http.fixture.faulty_header_mismatch`). Those
failures stay visible rather than being baselined away — the point is to prove
the gate tolerates a *known* failure without going blind to a new one.

A baseline is never migrated. `baseline_schema_version` is checked by name on
load, and an older document is refused rather than reinterpreted — including one
that would load perfectly well. Schema v3 added the capability binding, and a v2
document reads cleanly under this code while recording no surface at all: every
capability comparison would be skipped, and the gate would enforce nothing while
looking exactly as approved as before. Re-create it deliberately with
`baseline create`, carrying forward the approval reasons and reproduction
expectations the existing file records.

## Regenerating committed examples

```bash
uv run mcp-failure-lab examples regenerate          # rebuild
uv run mcp-failure-lab examples regenerate --check  # fail if anything changed
```

Deterministic by construction: volatile values (timestamps, durations, ephemeral
ports, run ids, and the machine-specific runtime record) are scrubbed in the
committed copies, so a clean tree stays clean when nothing changed. Live runs
keep all of it. Do not hand-edit artifacts under `artifacts/examples/`.
