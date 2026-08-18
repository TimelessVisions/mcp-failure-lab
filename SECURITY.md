# Security

## Scope

This project launches and drives untrusted local processes on purpose. It is a
testing harness, not a security boundary, and it makes no claim to contain a
hostile target.

## Protections currently implemented

- Local, explicitly authorised targets only. No Internet scanning, and hostile
  scenarios are never sent to third-party systems without written authorisation.
- Target environments are built from an allowlist, never inherited. Credential-
  shaped variable names are refused even when explicitly requested.
- Structured redaction of bearer tokens, API keys, private keys and
  credential-shaped JSON members before anything is persisted.
- Full process-tree termination (process group, SIGTERM then SIGKILL), with
  cleanup failure recorded as `CLEANUP_FAILURE` rather than ignored.
- Bounded output capture.
- **Containment for code this project did not write.** A target declaring
  `provenance: external` cannot run on the host: it runs in a container with
  resource limits, a read-only root filesystem and network isolation, or the
  load fails. The run record carries what `docker inspect` *observed* alongside
  what was requested, and a container missing a requested control makes the run
  unusable as evidence rather than producing a verdict about the target. There
  is no fallback to host execution when Docker is unavailable.

See [`docs/containment.md`](docs/containment.md) for exactly what each mode
denies and what it does not. Containment narrows the blast radius; it is not a
claim that a hostile target is contained.

## Reporting a defect in this project

Open an issue describing what you ran and what happened. If you believe the
issue is exploitable, say so in the first line and omit the working details
until someone replies.

## Reporting a defect this project found in someone else's implementation

Don't — not through this repository. Suspected defects in third-party
implementations are handled under
[`docs/responsible-disclosure.md`](docs/responsible-disclosure.md): private by
default, and never published, filed, or sent to maintainers without the
owner's explicit approval. Findings produced by this harness are not part of
this distribution.
