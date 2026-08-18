# Protocol profiles

There is no single timeless MCP lifecycle. This project treats every dated
specification revision as an explicit **profile**, and every scenario declares
which profiles it may run against. A behavioural difference is never called a
defect until the correct dated specification has been applied.

Facts in this document were read from the **official JSON schemas**, not from
prose or blog posts:

- `schema/2025-11-25/schema.json`
- `schema/2026-07-28/schema.json`

both retrieved from `raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/`
on 2026-08-12.

> **Limitation, stated up front.** The prose specification at
> `modelcontextprotocol.io` is blocked by the network egress policy of the
> environment this work was done in. Everything below is derived from the
> machine-readable schemas, which are authoritative for structure and error
> codes but do not carry all transport-level prose requirements. Claims that
> would need the prose spec are marked **UNCONFIRMED** rather than asserted.

---

## Profile: `mcp-2025-11-25`

| Property | Value |
|---|---|
| Spec version | `2025-11-25` |
| Lifecycle | Stateful — `initialize` / `notifications/initialized` handshake |
| `params._meta` | Not required |
| Transports | stdio, Streamable HTTP |
| Required HTTP headers | `MCP-Protocol-Version` |
| `resultType` on results | Not present in this schema |

Methods present in this revision and **removed** in 2026-07-28: `initialize`,
`ping`, `logging/setLevel`, `resources/subscribe`, `resources/unsubscribe`, and
the task methods (`tasks/list`, `tasks/get`, `tasks/cancel`, task payload).

Schema definition count: 145.

---

## Profile: `mcp-2026-07-28`

| Property | Value |
|---|---|
| Spec version | `2026-07-28` |
| Lifecycle | **Stateless** — no handshake; every request is self-describing |
| `params._meta` | **Required** (`RequestParams.required = ["_meta"]`) |
| Transports | stdio, Streamable HTTP |
| Required HTTP headers | `MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name` |
| `resultType` on `CallToolResult` | **Required** |

Schema definition count: 155.

### Removed

`initialize`, `notifications/initialized`, `ping`, `logging/setLevel`,
`resources/subscribe`, `resources/unsubscribe`, and the task methods. The
`InitializeRequest` definition is absent from the 2026-07-28 schema entirely.

### Added

- `server/discover` (`DiscoverRequest`). The schema states servers **MUST**
  implement it; clients **MAY** call it, since version negotiation can also
  happen inline via per-request `_meta`.
- `subscriptions/listen` (`SubscriptionsListenRequest`).
- Multi Round-Trip Requests: `InputRequest` is `anyOf` `CreateMessageRequest`,
  `ListRootsRequest`, `ElicitRequest`.

### The per-request `_meta` envelope

`RequestParams` requires `_meta`, and `RequestMetaObject` defines these keys:

| Key | Requirement |
|---|---|
| `io.modelcontextprotocol/protocolVersion` | **Required.** For HTTP it MUST match the `MCP-Protocol-Version` header, else the server MUST return `400 Bad Request`. |
| `io.modelcontextprotocol/clientCapabilities` | **Required.** Declared per request; servers MUST NOT infer capabilities from prior requests. |
| `io.modelcontextprotocol/clientInfo` | Clients SHOULD include it. Self-reported, not verified; servers SHOULD NOT use it for security decisions. |
| `io.modelcontextprotocol/logLevel` | Optional. Replaces the former `logging/setLevel` RPC. If absent the server MUST NOT send `notifications/message`. |

### Result envelope

`CallToolResult.required = ["content", "resultType"]`. The schema is explicit:

> Servers implementing this protocol version MUST include this field. For
> backward compatibility, when a client receives a result from a server
> implementing an earlier protocol version (which does not include
> `resultType`), the client MUST treat the absent field as `"complete"`.

That backward-compatibility clause is exactly why the harness gates the
`resultType` assertion to 2026-only targets. Asserting it against a 2025-11-25
target would be a false positive.

### Cacheability

List and discover results carry `ttlMs` and `cacheScope` (`"public"` /
`"private"`, modelled on HTTP `Cache-Control`). These are advisory and are
treated as non-semantic by the differential engine.

### Error codes

Standard JSON-RPC 2.0 codes apply in both profiles:

| Code | Meaning |
|---|---|
| `-32700` | Parse error |
| `-32600` | Invalid request |
| `-32601` | Method not found |
| `-32602` | Invalid params |
| `-32603` | Internal error |

MCP-specific codes introduced by 2026-07-28, all of which MUST be `400 Bad
Request` over HTTP:

| Code | Definition | Meaning |
|---|---|---|
| `-32020` | `HeaderMismatchError` | HTTP header values disagree with the body, or required headers are missing/malformed |
| `-32021` | `MissingRequiredClientCapabilityError` | The request needs a capability the client did not declare; `data.requiredCapabilities` lists them |
| `-32022` | `UnsupportedProtocolVersionError` | Requested version unknown/unsupported; `data.supported` lists usable versions |

### Deprecations

Roots, Sampling and Logging are deprecated with a stated minimum 12-month
window. The legacy HTTP+SSE transport is deprecated.

---

## Observed SDK support

Measured directly from installed packages on 2026-08-12, not from documentation.

| SDK | Version | Protocol support |
|---|---|---|
| `mcp` (Python) | 2.0.0 | `SUPPORTED_PROTOCOL_VERSIONS` = 2024-11-05 … 2026-07-28. Split into `HANDSHAKE_PROTOCOL_VERSIONS` (≤ 2025-11-25) and `MODERN_PROTOCOL_VERSIONS` (`2026-07-28` only). |
| `@modelcontextprotocol/sdk` (TypeScript) | 1.30.0 | Bundled types enumerate 2024-10-07 … **2025-11-25**. 2026-07-28 does not appear. |

**Consequence, and the reason version gating is load-bearing:** the Python SDK
can be driven at either era, while the TypeScript SDK at 1.30.0 cannot be driven
at 2026-07-28 at all. Running a 2026-only scenario against it and reporting a
failure would be a fabricated finding. The harness reports `NOT_APPLICABLE`
instead, with the reason recorded in the artifact.

Cross-language comparison is therefore performed at the **shared** 2025-11-25
profile, where both SDKs are legitimately comparable.

---

## Scenario applicability

Every scenario declares an `Applicability` gate:

- `profiles` — the profile ids it may run under
- `min_spec_version` / `max_spec_version`
- `transports`
- `roles`
- `requires_tools` — capability prerequisites

`Applicability.reason_not_applicable()` returns a human-readable reason, which
is stored on the run artifact. An unsupported scenario is `NOT_APPLICABLE`,
never `PASS` — a skipped check must never be mistaken for a passing one.
