# Containment

Targets are untrusted. This document says exactly what "contained" means here,
what it does not mean, and how the claim is verified rather than asserted.

## The rule

**Code this project did not write runs in a container or not at all.**

It is enforced in the model, not in the runner: a target file that declares
`provenance: external` with `execution_mode: host` fails to load.

```
provenance: local_fixture      # written here, under fixtures/
provenance: pinned_dependency  # an official SDK installed here at a pinned version
provenance: external           # third-party code — containment is mandatory
```

## There is no `auto`

`execution_mode` is `host` or `container`. There is deliberately no mode that
falls back to host execution when Docker is missing.

A fallback would mean the same command, on two machines, produced two results
that look identical in the artifact and were produced under completely different
isolation. When containment is requested and unavailable, the run fails with
`HARNESS_ERROR` and a `containment_unavailable` outcome. It is never a `PASS`,
and it is never a verdict about the target.

## Requested versus observed

Every control appears twice in a run record: what the target file asked for, and
what `docker inspect` says the running container actually had.

This is not defensive paperwork. Docker accepts `--pids-limit` on hosts without
the pids cgroup controller and `--cpus` on hosts without CFS quota support, and
reports success either way. A record built from the flags the harness passed
would state containment that the kernel never applied.

If any requested control is absent from the running container, the outcome is
`containment_weaker_than_requested`, the run is `HARNESS_ERROR`, and
`run.is_usable_evidence` is false. Nothing may be concluded about the target
from a run whose isolation was not what the artifact claims.

## Defaults

| Control | Default | Flag |
|---|---|---|
| Network | `none` | `--network none` |
| Memory | 512 MB, swap capped equal | `--memory` + `--memory-swap` |
| CPU | 1.0 | `--cpus` |
| Processes | 128 | `--pids-limit` |
| Root filesystem | read-only | `--read-only` |
| User | `65534:65534` (nobody) | `--user` |
| Capabilities | all dropped | `--cap-drop ALL` |
| Privilege escalation | blocked | `--security-opt no-new-privileges` |
| Scratch | `/tmp` tmpfs only | `--tmpfs` |
| Repo mounts | none; when present, read-only | `--volume …:ro` |

A memory limit without a swap limit is not a limit, so both are always set.

## How an image is identified

A target declares an `image_source`, and there are two of them because a pulled
image and a built one are identified by genuinely different things.

**`kind: registry`** carries a `repository` and a mandatory `repository_digest`.
That digest is what the runtime is given; `display_tag` exists for a human
reading the file and is never resolved, because a tag is a name that can be
moved. A registry source with anything other than a `sha256:` digest is rejected
at load.

**`kind: local_build`** carries **no digest at all**, deliberately. It names the
build key, the committed build context and Dockerfile, the base images it pulls
and the toolchain versions that decide the output — everything a second machine
needs in order to build the same thing. The identity of what comes *out* is
resolved at run time from `artifacts/external/build-resolution.json`, written by
`mcp-failure-lab external-build`.

That split is measured, not stylistic. A builder-local image ID is not portable:
the containerd snapshotter exports a manifest digest with `rewrite-timestamp`
applied while the classic storage driver writes a config ID, for the same build.
Committing one would put a value no second machine can reproduce into a field
that reads as a reproducibility guarantee, and would make the same approved
target fingerprint differently on every runner.

A local build launches by **raw image ID**, never by its tag: a tag could have
been retargeted between the build recording it and the run using it. Before the
handshake — or, for a contained HTTP target, before the probe sends the scenario
plan — the started container's `.Image` is read back and compared against the
resolved ID. A mismatch is `HARNESS_ERROR`: the harness could not run what it was
asked to run, which says nothing about the target and is never a skipped test.

## The network model

Three modes, and the differences are not cosmetic.

**`none`** — `--network none`. No interfaces but loopback. Correct for stdio,
where the harness communicates over the process's own stdin and stdout and needs
no socket to the target at all.

**`published_loopback`** — required for Streamable HTTP, because the harness has
to open a socket to the target. A user-defined bridge with
`com.docker.network.bridge.enable_ip_masquerade=false`, and a single port
published to `127.0.0.1` explicitly. Publishing to `0.0.0.0` would expose a
deliberately hostile target to the local network.

`--internal` would be the stronger choice and was tested first. It also breaks
published ports, so the harness could not reach an HTTP target at all. Disabling
IP masquerading removes the NAT rule that egress depends on while leaving
inbound DNAT intact.

**`isolated_sidecar`** — the required mode for untrusted HTTP targets, and
strictly stronger than the other two. A Docker `--internal` network with **no
port published to the host at all**. The harness never opens a socket to the
target; a probe sidecar on the same network is the only thing that can reach it.

```
HARNESS (host)
    │  docker control only — never an HTTP socket to the target
    ▼
PROBE SIDECAR  ◄──── isolated internal network ────►  UNTRUSTED TARGET
    │                                                  (publishes nothing)
    ▼
output volume (probe writes; harness reads)
```

Measured: a container on an `--internal` network has **no default route at
all**, so it reaches neither the Internet nor the container host. That closes
the gateway hole below.

The probe is contained too — read-only root, `cap-drop ALL`,
`no-new-privileges`, memory and pid limits, no published port. It handles bytes
from an untrusted target, so treating it as trusted infrastructure would move
the boundary rather than enforce it. Its script and plan are mounted read-only;
the single writable mount is the output directory, which is the only channel
back to the harness.

The probe speaks **no MCP semantics**. It sends the bytes it is given and
records what came back, with connect, first-byte and body-complete timings. All
interpretation stays on the host, where the assertion engine and its tests live;
a probe that made judgements would be a second, unreviewed implementation of the
assertion engine.

Neither container gets a Docker socket, host credentials, or inherited
environment — only explicitly declared values cross the boundary.

### What `published_loopback` does not deny

Measured, not assumed: a container on a no-masquerade bridge **can still reach
services listening on the container host**. Connections to the Docker gateway
address are refused rather than timing out, which means the packets arrive.

Egress here is denied by the absence of NAT, not by a firewall rule. A target
that wanted to reach something on the host machine could. This limit is recorded
in `stated_limits` on every `published_loopback` run rather than left for a
reader to discover.

**This is the reason `isolated_sidecar` exists.** Under it there is no default
route, so there is no gateway address to try, and a test asserts exactly that.

## What containment does not protect against

Attached to every contained run, because "contained" invites a stronger reading
than is warranted:

- Docker is not a security boundary against a kernel exploit. A container escape
  defeats every control above.
- The image is trusted. Containment isolates the target's *behaviour at runtime*;
  it says nothing about what the image contains.
- Under `published_loopback`, host services remain reachable (above).
- Wall-clock and CPU limits bound resource use, not information disclosure.

## How the claim is verified

Two independent layers, because a correct `docker run` argv and a container that
actually denied something are different claims that fail separately.

**Pure argv tests** (`tests/unit/test_containment_rules.py`) assert what is
requested — every control present, the port bound to loopback, mounts read-only,
`${PYTHON}` rewritten to the image's interpreter rather than the harness's host
path.

**Hostile probes** (`fixtures/hostile_containment.py`, exercised by
`tests/integration/test_containment.py`) attempt exactly what containment is
supposed to prevent: reaching the Internet, writing `/`, forking past the pids
limit, allocating past the memory limit, and running as root.

**Sidecar isolation** (`tests/integration/test_sidecar_containment.py`) attacks
the HTTP path specifically: reaching the Internet, reaching ordinary host
services, writing outside the tmpfs, reading a synthetic host-only file,
inheriting a synthetic credential, exceeding memory, and exceeding the process
limit — plus the argv-level guarantees that no port is published, no Docker
socket is mounted, and the probe cannot rewrite its own script or plan.

It also reads **both** containers back through `docker inspect`. The probe is
held to the target's standard, not a lower one: non-root `65534:65534`, a CPU
limit, a memory and swap limit, a pids limit, a read-only root, `cap-drop ALL`,
`no-new-privileges`, no Docker socket, no inherited environment, no published
port, and the exact internal network created for that run. It handles bytes from
an untrusted target, so a probe running as root would move the boundary rather
than enforce it.

The probe container is **created, inspected, and only then started**. Validating
containment after the run would mean the evidence had already been produced
under whatever isolation actually applied; validating it before means a weakness
stops the run before a single byte crosses. It is inspected again while running,
because `docker create` records a request and only a running container shows
what the kernel applied.

Each probe runs **twice** — once uncontained, where it must succeed, and once
contained, where it must be denied. Without the uncontained half, a probe that
was simply broken would be indistinguishable from containment working.

The probes are bounded on purpose. `fork_bomb` stops at 512 and `memory` at
768 MB: a probe that could damage the machine it runs on would be a worse defect
than the one it tests for.

## Running a contained target

```bash
# Pull the pinned image once.
docker pull mirror.gcr.io/library/python:3.12-slim

# Contained stdio.
uv run mcp-failure-lab run -t targets/fixtures/reference-contained.yaml \
  --scenario baseline.tools_list

# Contained Streamable HTTP, through the probe sidecar.
uv run mcp-failure-lab run -t targets/http/fixture-2026-contained.yaml \
  --scenario baseline.http_tools_list
```

Both are local fixtures, so containment is not required for either — they are
contained anyway, because a containment path exercised only against third-party
code is a path that has never been tested.

## The contained HTTP path

`mcp-failure-lab run` selects an execution strategy from the target's transport,
execution mode and network mode (`src/interop_lab/runner/strategy.py`):

| transport | mode | network | strategy |
|---|---|---|---|
| stdio | host | — | `InteractiveSessionStrategy` |
| stdio | container | `none` | `InteractiveSessionStrategy` |
| streamable_http | host | — | `InteractiveSessionStrategy` |
| streamable_http | container | `isolated_sidecar` | `ContainedHttpPlanStrategy` |
| streamable_http | container | anything else | refused |

There is no silent fallback, and a contained HTTP target can never reach
`StreamableHttpSession` on the host. That session opens a socket from the host to
the target; taking it would either fail confusingly or — if a port were ever
published — succeed while writing `contained` into an artifact describing an
ordinary local exchange.

The contained path is genuinely a different shape, which is why it is a separate
strategy rather than another `TransportSession`. There is no `send` and no
`receive` on the host side, because the host is not on the wire: the scenario is
compiled into a single versioned **probe plan**
(`src/interop_lab/probe/plan.py`), the plan runs inside the sidecar, and the host
interprets what came back. Implementing `send`/`receive`/`request` as methods
that queue work for a batch that has not run yet would satisfy the interface and
describe something the code does not do.

Everything after interpretation is shared: the same `evaluate`, the same
`classify`, the same `WireTrace`, the same differential engine, minimiser,
replay package and CI gate. There is no contained-HTTP-only assertion engine.

### What the probe does and does not know

The probe speaks no MCP. It sends the exact bytes the plan gives it and records
what came back. Its state handling is generic transport plumbing — capture a
response header into a named variable, capture a JSON value by JSON Pointer,
substitute a variable into a later header, path or body, optionally retain
cookies, optionally reuse a connection. Nothing in it knows what a session id
*is*, only that a plan asked for a named header to be carried forward.

That generic capability is what supports the 2025-11-25 stateful lifecycle:
the plan captures `Mcp-Session-Id` from the `initialize` response, replays it on
scenario traffic, and releases it with a `DELETE` on cleanup. The 2025-11-25
Streamable HTTP binding makes the session id a server's **MAY**, so the capture
and the substitution are both optional and a server that issues none is not
treated as defective. The 2026-07-28 profile is stateless and gets no handshake
at all — sending an `initialize` to it would be sending a method that revision
removed.

The probe validates its plan strictly and refuses anything it cannot execute
exactly as written, including any field it does not recognise. A probe that
silently ignored an unsupported member would produce evidence for a run nobody
asked for. Because there are two validators for one document — pydantic on the
host, hand-written stdlib in the probe — `tests/unit/test_probe_plan.py` holds
both to the same contract field by field.

## Network ownership

A sidecar run creates its own network and never reuses one because the name
matches. Names carry a random suffix rather than only a truncated run id: two
runs of the same scenario against the same target share a prefix, and a
collision there does not fail cleanly — it makes one run adopt another's
resources.

Every resource is labelled `interop-lab.owner=true`, `interop-lab.run-id=<id>`,
`interop-lab.resource=<kind>` and `interop-lab.harness-version=<version>`. After
creation the network is inspected and checked: `Internal` is true, the driver is
`bridge`, the labels are ours, no driver options were set, and no container is
attached that this run did not start.

If a network with our name already exists, the run **fails**. It is not reused —
its properties and its neighbours are unknown, and attaching an untrusted target
to it would put that target somewhere nobody chose. It is not deleted either: it
may belong to something running, and reclaiming another process's resource to
make our own run proceed is how a test harness becomes an outage.

## The output channel

The probe writes to one directory, and that directory is the only writable mount
it has. Two configurations bound it, and the run record says which one applied:

- **owned** — the harness can `chown` (it is root, or already owns uid 65534),
  so the directory belongs to the probe and is mode `0700`.
- **parent_restricted** — the harness cannot `chown`, so the directory is mode
  `0777` inside a `mkdtemp` parent at mode `0700`.

The second is not the weaker option it looks like. Unix requires execute
permission on *every* parent directory to traverse into one, and the parent
denies it to group and other — so the set of accounts that can reach the output
is identical either way: the harness user and root. This is the unavoidable
Docker Desktop and rootless-Docker difference, since both remap bind-mount
ownership, and it is also what happens on any CI runner that is not root.

An earlier version treated `parent_restricted` as a containment weakness, which
made every contained run on an ordinary non-root runner report
`containment_weaker_than_requested` and produce unusable evidence. That was
overstating in a way worth naming: it says nothing about the isolation of the
*target*, which is what "usable evidence" means, and a check that fails on a
correct configuration is a check someone eventually turns off.

What genuinely does expose the channel is a parent that permits traversal, so
that is verified rather than assumed — and *that* is recorded as a weakness.

Output is read only after the probe has exited, and is treated as untrusted
input throughout: bounded total size, bounded per-event size, bounded file
count, a truncation flag when a cap was hit, no symlink traversal, and no path
outside the directory. A symlink in the output directory would otherwise turn
the evidence channel into an arbitrary host read.

## Cleanup

A surviving container is a cleanup failure exactly as a surviving process tree
is. Killing the local `docker run` client does not necessarily stop the
container, so the session removes the container by name and then verifies it is
gone. A leak is recorded as `cleanup_ok: false` with a reason — a harness fault,
never reported as a target defect.

A sidecar run reports cleanup for the target container, the probe container, the
isolated network and the temporary plan and output directory, and that report
**reaches the result**. Removal is retried once, forcibly; if the resource is
still there it is named as a leak, `cleanup_ok` becomes false, and `classify`
returns `CLEANUP_FAILURE`. A leaked resource means the next run starts from a
state nobody accounted for, so the evidence this run produced cannot be trusted
either. Each failure mode — target removal, probe removal, network removal, and
a stale network collision — has a test that simulates it
(`tests/unit/test_sidecar_pure.py`).
