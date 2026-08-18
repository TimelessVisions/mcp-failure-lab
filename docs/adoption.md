# Adopting the gate in your repository

This document was written **from a clean-room install**, not from memory. The
harness was built as a wheel from a fresh source checkout, installed into a
git repository with no shared history, pointed at a server that repository
owned, and driven until the gate both passed and failed. Every friction point
below is one that actually happened, and the ones that were fixable were fixed
rather than documented around.

## Requirements

| | |
|---|---|
| Python | **3.12 or newer** |
| Docker | Only for contained targets. A host-executed target needs none. |
| Node | Only to run the official conformance suite as a baseline. |

> The first install attempt failed on Python 3.11 with
> `Package 'interop-lab' requires a different Python: 3.11.15 not in '>=3.12'`.
> That message is clear, so nothing was changed — but 3.12 is a hard floor and
> a repository still on 3.11 needs a separate interpreter for the gate.

## Install

```bash
# From a built wheel — no source checkout, no shared history required.
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python interop_lab-0.1.0-py3-none-any.whl
```

## Scaffold

```bash
.venv/bin/mcp-failure-lab init --launch '${PYTHON} path/to/your_server.py' \
                           --target-id my.server
```

This writes `interop/target.yaml`, `interop/matrix.yaml` and
`interop/suite.yaml`, then prints the exact next four commands.

> `init` exists **because of this exercise.** The first attempt hand-wrote a
> matrix file and failed with `matrix_id — Field required`. The message names
> the missing field but not what a valid file looks like, which is only obvious
> to someone who already has one. Two fixes followed: `init` scaffolds working
> files, and a malformed matrix or suite now prints a complete worked example
> alongside the validation error.

## The four commands

```bash
# 1. See what your server does. Nothing is approved yet.
mcp-failure-lab run -t interop/target.yaml --suite smoke

# 2. Approve the current behaviour. A reason is mandatory.
mcp-failure-lab baseline create \
  --matrix interop/matrix.yaml \
  --suite interop/suite.yaml \
  --out interop/baseline.json \
  --reason "Initial approved baseline" \
  --by "you@example.com"

# 3. Gate a run against it.
mcp-failure-lab ci \
  --baseline interop/baseline.json \
  --matrix interop/matrix.yaml \
  --suite interop/suite.yaml \
  --junit interop/junit.xml \
  --sarif interop/results.sarif

# 4. Prove it can fail. Break something on purpose and re-run step 3.
```

Step 4 is not optional. A gate nobody has seen fail is a gate nobody should
trust. In the clean-room run, changing the adopter's server to answer unknown
methods with `-32602` instead of `-32601` produced:

```
gate regression (exit 1)
  REGRESSION new_failure: adopter.server / wire.unknown_method
      PASS -> SPEC_VIOLATION
```

## Approving a failure

A failing result cannot enter a baseline on the strength of the document-level
reason. Each one must be named with its own justification:

```bash
mcp-failure-lab baseline create ... \
  --approve-failure my.server:wire.unknown_method \
  --approval-reason "Known, tracked in ISSUE-123; fix scheduled for 2.1"
```

"The build was red when I made the baseline" is exactly what the gate exists to
prevent, so the tool refuses to record a failure nobody named.

## Exit codes

| Code | Meaning | CI should |
|---:|---|---|
| 0 | Passed | continue |
| 1 | Regression | fail the build |
| 2 | Configuration or baseline error | fail the build — the gate did not run |
| 3 | Harness or environment failure | fail the build; this is not a verdict about your server |

**2 and 3 are not "soft" failures.** They mean the measurement did not happen.
Treating them as passes is how a gate goes quiet.

## What makes the gate go red

- A new failure, or a classification change.
- The same classification with a **different set of failing assertions** — same
  label, different finding.
- A result **not in the baseline at all**, including a new `PASS`: a scenario
  nobody approved is now contributing to the verdict.
- A scenario that was applicable at approval time and is now skipped.
- A missing result. Absence is never a pass.
- Target or scenario **fingerprint drift** — the baseline no longer describes
  what is being measured.

The gate **never updates the baseline**. An improvement is reported for review.

## When the gate is red for a good reason

You changed the server deliberately, or corrected a scenario. Re-approve on
purpose:

```bash
mcp-failure-lab baseline create ... --reason "Deliberate change: <what and why>"
```

Then commit the new baseline in the same change as the code. A baseline updated
in a separate commit loses the connection between the behaviour change and its
approval.

## CI wiring

```yaml
- name: MCP interop gate
  run: |
    mcp-failure-lab ci \
      --baseline interop/baseline.json \
      --matrix interop/matrix.yaml \
      --suite interop/suite.yaml \
      --junit interop/junit.xml \
      --sarif interop/results.sarif
```

`junit.xml` feeds a test-report UI. `results.sarif` feeds code scanning; it is
validated against a pinned copy of the OASIS 2.1.0 schema whose SHA-256 is
checked before use.

`NOT_APPLICABLE` is emitted as `skipped`, never as a pass.

## Sign-off

Who approves a baseline change is a decision for your team, but the file makes
it reviewable: `created_by`, `created_reason`, and a per-entry
`approval_reason` for every approved failure. Treat `interop/baseline.json` as
code — require a review on it.

## Verified

Everything above was executed against two independent throwaway repositories
with no shared history with this project:

- wheel built from a clean checkout ✅
- installed with no source tree present ✅
- `init` scaffold, then its own printed steps, verbatim ✅
- gate passes on the approved baseline (exit 0) ✅
- gate catches a real regression in the adopter's own server (exit 1) ✅
- JUnit and SARIF written and well-formed ✅

## Known rough edges

- **Contained targets need images built beforehand.** There is no `build`
  subcommand; the Dockerfile is yours to build and pin by digest.
- **Streamable HTTP under containment is refused**, not run on the host. See
  [`containment.md`](containment.md).
- **Scenario coverage is narrow.** Passing means "passed these scenarios".
