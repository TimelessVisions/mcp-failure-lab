"""Profile-aware JSON-RPC message construction.

The whole point of dated profiles is that the harness must not force a 2025-era
initialize handshake onto a 2026-era stateless target, nor omit the per-request
`_meta` envelope that the 2026 schema makes mandatory.
"""

from __future__ import annotations

from typing import Any

from interop_lab.naming import HARNESS_VERSION, PRODUCT_NAME
from interop_lab.profiles.registry import Lifecycle, ProtocolProfile

CLIENT_INFO: dict[str, Any] = {"name": PRODUCT_NAME, "version": HARNESS_VERSION}
CLIENT_CAPABILITIES: dict[str, Any] = {}


def build_request(
    profile: ProtocolProfile,
    *,
    rpc_id: int | str,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a JSON-RPC request shaped for the given dated profile."""
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    request_params: dict[str, Any] = dict(params or {})

    if profile.requires_request_meta:
        meta: dict[str, Any] = dict(request_params.get("_meta") or {})
        if profile.meta_protocol_version_key:
            meta.setdefault(profile.meta_protocol_version_key, profile.spec_version)
        if profile.meta_client_capabilities_key:
            meta.setdefault(profile.meta_client_capabilities_key, CLIENT_CAPABILITIES)
        meta.setdefault("io.modelcontextprotocol/clientInfo", CLIENT_INFO)
        request_params["_meta"] = meta
        # params is a required member when _meta is required.
        body["params"] = request_params
    elif request_params:
        body["params"] = request_params

    return body


def build_notification(
    profile: ProtocolProfile, *, method: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params:
        body["params"] = params
    return body


def initialize_params(profile: ProtocolProfile) -> dict[str, Any]:
    return {
        "protocolVersion": profile.spec_version,
        "capabilities": CLIENT_CAPABILITIES,
        "clientInfo": CLIENT_INFO,
    }


def handshake_methods(profile: ProtocolProfile) -> list[str]:
    """Methods that must be exchanged before ordinary traffic, if any."""
    if profile.lifecycle is Lifecycle.STATEFUL_INITIALIZE:
        return ["initialize", "notifications/initialized"]
    return []


def list_tools_method(profile: ProtocolProfile) -> str:
    return "tools/list"


def call_tool_params(
    profile: ProtocolProfile, *, name: str, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {"name": name, "arguments": arguments or {}}
