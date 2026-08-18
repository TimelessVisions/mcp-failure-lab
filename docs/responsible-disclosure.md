# Responsible disclosure

A real implementation difference may be a bug, a specification ambiguity, or a
harness error. The default is **private**, and nothing leaves the development
repository without explicit approval from the owner.

## Before anything is called a finding

1. Reproduce at least three consecutive times.
2. Pin the exact affected version and commit SHA.
3. Rule out harness error. Two harness defects were found this way during the
   first vertical slice; assume a third is likelier than a third-party bug.
4. Compare against the correct **dated** specification. A 2026 requirement does
   not apply to a 2025 target.
5. Minimise the reproduction, preserving the failure signature.
6. Test an adjacent or corrected version where practical.

## Required framing

Every finding states, in this order:

- **Fact** — what was observed.
- **Normative basis** — what the dated specification requires, if anything. If
  the requirement could not be confirmed, say so explicitly.
- **Interpretation** — what it may mean.
- **Limitation** — what is not proven.
- **Reproduction** — how to reproduce it.

## Prohibited without explicit approval

Publishing exploit details · contacting maintainers · opening a public issue ·
submitting a pull request · publishing benchmark results · publishing any
artifact naming a third-party implementation.

## Synthetic defects are not findings

Fixtures under `fixtures/` with deliberate defects exist to prove the harness
detects them. Reporting one as a third-party defect would be fabrication.
