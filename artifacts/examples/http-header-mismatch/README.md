# Example artifact — http-header-mismatch

**This demonstrates a deliberately faulty local fixture. It is not a defect in
any third-party implementation, and must never be reported as one.**

## What happened

A POST whose body method was `tools/list` was sent with header `Mcp-Method: tools/call`. The fixture answered HTTP 200 with a normal `tools/list` result instead of rejecting the request.

## Minimisation

**Already minimal.** Nothing could be removed without losing the failure signature. No reduction is claimed.

| Metric | Original | Minimised |
|---|---:|---:|
| events | 1 | 1 |
| body bytes | 2 | 2 |
| header-override bytes | 28 | 28 |
| mutation surface bytes | 30 | 30 |

Preserved failing assertions: `['is_error', 'code_32020']`
Stable across 3 consecutive runs.

## Reproduce

```bash
./run.sh              # or: pwsh run.ps1
```

Both wrappers resolve their own directory, so this package works from anywhere.

## Limitation

THIS IS A DELIBERATELY DEFECTIVE LOCAL FIXTURE, not a third-party implementation. The defect is switched on by INTEROP_FIXTURE_DEFECTS=ignore_header_body_mismatch and exists solely to prove the harness detects this condition. This scenario is ALREADY MINIMAL: one step, one constitutive header, a two-byte body. No reduction is claimed for it.

## Regenerate

```bash
uv run mcp-failure-lab examples regenerate
```

Do not hand-edit these files.
