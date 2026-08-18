# Example artifact — http-header-mismatch-minimized

**This demonstrates a deliberately faulty local fixture. It is not a defect in
any third-party implementation, and must never be reported as one.**

## What happened

The same header/body disagreement was submitted inside a three-step scenario carrying unused body fields and a decorative header. Automatic reduction removed the unrelated steps, the unused fields and the decorative header, while preserving the Mcp-Method mismatch.

## Minimisation

**Reduced** via remove_optional_steps, remove_payload_fields, remove_header_overrides.

| Metric | Original | Minimised |
|---|---:|---:|
| events | 3 | 1 |
| body bytes | 629 | 2 |
| header-override bytes | 79 | 28 |
| mutation surface bytes | 708 | 30 |

Preserved failing assertions: `['is_error', 'code_32020']`
Stable across 3 consecutive runs.

## Reproduce

```bash
./run.sh              # or: pwsh run.ps1
```

Both wrappers resolve their own directory, so this package works from anywhere.

## Limitation

THIS IS A DELIBERATELY DEFECTIVE LOCAL FIXTURE. The surrounding noise is synthetic material added to demonstrate reduction; it is not something a real client would send. It proves the reducer works, not that any real implementation is defective.

## Regenerate

```bash
uv run mcp-failure-lab examples regenerate
```

Do not hand-edit these files.
