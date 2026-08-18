"""Dated MCP protocol profiles.

There is no single timeless MCP lifecycle. Everything the harness does is
parameterised by an explicit dated profile so a scenario written against one
specification revision is never silently executed against another.

Facts here were read from the official JSON schemas
(``schema/<date>/schema.json`` in modelcontextprotocol/modelcontextprotocol),
not from prose. See ``docs/protocol-profiles.md`` for the derivation.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field


class Lifecycle(StrEnum):
    """How a session is established for a given profile."""

    #: `initialize` / `notifications/initialized` handshake, then stateful session.
    STATEFUL_INITIALIZE = "stateful_initialize"
    #: No handshake. Every request is self-describing.
    STATELESS_SELF_DESCRIBING = "stateless_self_describing"


class Transport(StrEnum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class Role(StrEnum):
    SERVER = "server"
    CLIENT = "client"
    FIXTURE = "fixture"


class ProtocolProfile(BaseModel):
    """A dated specification revision and the behaviour it mandates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str
    spec_version: str = Field(description="Dated revision, e.g. '2026-07-28'.")
    schema_source: str
    lifecycle: Lifecycle
    supported_transports: tuple[Transport, ...]

    #: Wire-level requirements, used to build requests and to assert responses.
    requires_initialize: bool
    requires_request_meta: bool = Field(
        description="Whether params._meta is a required member of every request."
    )
    meta_protocol_version_key: str | None = None
    meta_client_capabilities_key: str | None = None
    #: HTTP headers the transport must send. Empty for stdio-only concerns.
    required_http_headers: tuple[str, ...] = ()
    #: Result envelope requirements.
    requires_result_type: bool = False

    removed_methods: tuple[str, ...] = ()
    added_methods: tuple[str, ...] = ()
    deprecated_features: tuple[str, ...] = ()

    #: MCP-specific JSON-RPC error codes introduced by this revision.
    error_codes: dict[str, int] = Field(default_factory=dict)

    def supports(self, transport: Transport) -> bool:
        return transport in self.supported_transports


# Standard JSON-RPC 2.0 codes, common to every profile.
JSONRPC_PARSE_ERROR: Final = -32700
JSONRPC_INVALID_REQUEST: Final = -32600
JSONRPC_METHOD_NOT_FOUND: Final = -32601
JSONRPC_INVALID_PARAMS: Final = -32602
JSONRPC_INTERNAL_ERROR: Final = -32603


PROFILE_2025_11_25: Final = ProtocolProfile(
    profile_id="mcp-2025-11-25",
    spec_version="2025-11-25",
    schema_source=(
        "https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol"
        "/main/schema/2025-11-25/schema.json"
    ),
    lifecycle=Lifecycle.STATEFUL_INITIALIZE,
    supported_transports=(Transport.STDIO, Transport.STREAMABLE_HTTP),
    requires_initialize=True,
    requires_request_meta=False,
    required_http_headers=("MCP-Protocol-Version",),
    requires_result_type=False,
    added_methods=(
        "initialize",
        "ping",
        "logging/setLevel",
        "resources/subscribe",
        "resources/unsubscribe",
        "tasks/list",
        "tasks/get",
        "tasks/cancel",
    ),
    error_codes={
        "parse_error": JSONRPC_PARSE_ERROR,
        "invalid_request": JSONRPC_INVALID_REQUEST,
        "method_not_found": JSONRPC_METHOD_NOT_FOUND,
        "invalid_params": JSONRPC_INVALID_PARAMS,
        "internal_error": JSONRPC_INTERNAL_ERROR,
    },
)

PROFILE_2026_07_28: Final = ProtocolProfile(
    profile_id="mcp-2026-07-28",
    spec_version="2026-07-28",
    schema_source=(
        "https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol"
        "/main/schema/2026-07-28/schema.json"
    ),
    lifecycle=Lifecycle.STATELESS_SELF_DESCRIBING,
    supported_transports=(Transport.STDIO, Transport.STREAMABLE_HTTP),
    requires_initialize=False,
    requires_request_meta=True,
    meta_protocol_version_key="io.modelcontextprotocol/protocolVersion",
    meta_client_capabilities_key="io.modelcontextprotocol/clientCapabilities",
    required_http_headers=("MCP-Protocol-Version", "Mcp-Method", "Mcp-Name"),
    requires_result_type=True,
    removed_methods=(
        "initialize",
        "notifications/initialized",
        "ping",
        "logging/setLevel",
        "resources/subscribe",
        "resources/unsubscribe",
        "tasks/list",
        "tasks/get",
        "tasks/cancel",
    ),
    added_methods=("server/discover", "subscriptions/listen"),
    deprecated_features=("roots", "sampling", "logging", "http+sse transport"),
    error_codes={
        "parse_error": JSONRPC_PARSE_ERROR,
        "invalid_request": JSONRPC_INVALID_REQUEST,
        "method_not_found": JSONRPC_METHOD_NOT_FOUND,
        "invalid_params": JSONRPC_INVALID_PARAMS,
        "internal_error": JSONRPC_INTERNAL_ERROR,
        "header_mismatch": -32020,
        "missing_required_client_capability": -32021,
        "unsupported_protocol_version": -32022,
    },
)


#: What a Streamable HTTP client must say it accepts, in both dated revisions.
#:
#: Not a preference. The transport lets a server answer a POST with either a
#: single JSON object or an SSE stream, at the server's discretion, so a client
#: that advertises only `application/json` is asking for something the server is
#: not obliged to be able to give. The official TypeScript SDK enforces this
#: literally: it answers such a request with HTTP 406 and never reaches the
#: protocol at all.
#:
#: This harness sent `Accept: application/json` until an external implementation
#: refused it. That was a defect in the harness, and it is recorded as one —
#: nothing about it is a finding against any third party.
STREAMABLE_HTTP_ACCEPT: Final[str] = "application/json, text/event-stream"


PROFILES: Final[dict[str, ProtocolProfile]] = {
    PROFILE_2025_11_25.profile_id: PROFILE_2025_11_25,
    PROFILE_2026_07_28.profile_id: PROFILE_2026_07_28,
    # Convenience aliases by bare date.
    PROFILE_2025_11_25.spec_version: PROFILE_2025_11_25,
    PROFILE_2026_07_28.spec_version: PROFILE_2026_07_28,
}


def get_profile(profile_id: str) -> ProtocolProfile:
    """Resolve a profile by id or bare date, failing loudly on an unknown name."""
    try:
        return PROFILES[profile_id]
    except KeyError:
        known = sorted({p.profile_id for p in PROFILES.values()})
        raise KeyError(f"unknown protocol profile {profile_id!r}; known: {known}") from None
