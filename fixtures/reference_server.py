#!/usr/bin/env python3
"""Local reference MCP server fixture, stdio, protocol profile 2026-07-28.

Hand-written against the official 2026-07-28 JSON schema so the harness has a
target whose conformance is fully controlled and whose defects are deliberate.

Behaviour is switched by environment variables, all defaulting to conforming:

  INTEROP_FIXTURE_DEFECTS   comma-separated defect switches (see DEFECTS)
  INTEROP_FIXTURE_DELAY_MS  milliseconds to sleep before answering tools/call

This fixture is deliberately dependency-free: it must not drift with an SDK.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_VERSIONS = [PROTOCOL_VERSION]

META_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
UNSUPPORTED_PROTOCOL_VERSION = -32022

DEFECTS = {
    # Omit the `resultType` member the 2026-07-28 schema marks required.
    "omit_result_type",
    # Answer tools/call with the wrong JSON-RPC id.
    "wrong_response_id",
    # Accept any protocol version instead of rejecting unknown ones.
    "accept_any_version",
    # Emit a non-protocol line on stdout before the response.
    "stdout_noise",
    # Return `content` as an object rather than the required array.
    "content_not_array",
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "echo",
        "description": "Echo the provided text back to the caller.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "slow_echo",
        "description": "Echo after a fixture-controlled delay, for timeout testing.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
]


def active_defects() -> set[str]:
    raw = os.environ.get("INTEROP_FIXTURE_DEFECTS", "")
    requested = {item.strip() for item in raw.split(",") if item.strip()}
    unknown = requested - DEFECTS
    if unknown:
        print(f"fixture: unknown defect switches {sorted(unknown)}", file=sys.stderr, flush=True)
    return requested & DEFECTS


DEFECT = active_defects()
DELAY_MS = int(os.environ.get("INTEROP_FIXTURE_DELAY_MS", "0"))


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def error(rpc_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": err}


def with_result_type(result: dict[str, Any], value: str = "complete") -> dict[str, Any]:
    """Attach the required `resultType`, unless the omission defect is active."""
    if "omit_result_type" in DEFECT:
        return result
    return {**result, "resultType": value}


def check_version(params: dict[str, Any]) -> dict[str, Any] | None:
    """Return an error payload when the request's protocol version is unusable."""
    if "accept_any_version" in DEFECT:
        return None
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return {"code": INVALID_PARAMS, "message": "params._meta is required"}
    requested = meta.get(META_VERSION_KEY)
    if not isinstance(requested, str):
        return {
            "code": INVALID_PARAMS,
            "message": f"params._meta.{META_VERSION_KEY} is required",
        }
    if requested not in SUPPORTED_VERSIONS:
        return {
            "code": UNSUPPORTED_PROTOCOL_VERSION,
            "message": f"unsupported protocol version {requested}",
            "data": {"requested": requested, "supported": SUPPORTED_VERSIONS},
        }
    if META_CAPABILITIES_KEY not in meta:
        return {
            "code": INVALID_PARAMS,
            "message": f"params._meta.{META_CAPABILITIES_KEY} is required",
        }
    return None


def handle_discover(rpc_id: Any) -> dict[str, Any]:
    result = with_result_type(
        {
            "capabilities": {"tools": {}},
            "instructions": "Reference fixture server for MCP Failure Lab.",
            "cacheScope": "public",
            "ttlMs": 60_000,
            "serverInfo": {"name": "interop-lab-reference-fixture", "version": "0.1.0"},
            "protocolVersions": SUPPORTED_VERSIONS,
        }
    )
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def handle_list_tools(rpc_id: Any) -> dict[str, Any]:
    result = with_result_type({"tools": TOOLS, "cacheScope": "public", "ttlMs": 60_000})
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def handle_call_tool(rpc_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(name, str):
        return error(rpc_id, INVALID_PARAMS, "params.name must be a string")

    known = {tool["name"] for tool in TOOLS}
    if name not in known:
        return error(rpc_id, INVALID_PARAMS, f"unknown tool {name!r}")

    if name == "slow_echo" and DELAY_MS > 0:
        time.sleep(DELAY_MS / 1000)

    text = arguments.get("text", "")
    if not isinstance(text, str):
        return error(rpc_id, INVALID_PARAMS, "arguments.text must be a string")

    content: Any = [{"type": "text", "text": text}]
    if "content_not_array" in DEFECT:
        content = {"type": "text", "text": text}

    result = with_result_type({"content": content, "isError": False})
    response_id = rpc_id
    if "wrong_response_id" in DEFECT:
        response_id = f"{rpc_id}-wrong" if isinstance(rpc_id, str) else (rpc_id or 0) + 1000

    if "stdout_noise" in DEFECT:
        sys.stdout.write("fixture: this line is not protocol traffic\n")
        sys.stdout.flush()

    return {"jsonrpc": "2.0", "id": response_id, "result": result}


def dispatch(request: dict[str, Any]) -> dict[str, Any] | None:
    rpc_id = request.get("id")
    method = request.get("method")

    if request.get("jsonrpc") != "2.0":
        return error(rpc_id, INVALID_REQUEST, "jsonrpc member must be exactly '2.0'")
    if not isinstance(method, str):
        return error(rpc_id, INVALID_REQUEST, "method member must be a string")
    if rpc_id is None:
        return None  # Notification: no response permitted.

    params = request.get("params")
    if not isinstance(params, dict):
        return error(rpc_id, INVALID_PARAMS, "params is required and must be an object")

    version_error = check_version(params)
    if version_error is not None:
        return error(
            rpc_id, version_error["code"], version_error["message"], version_error.get("data")
        )

    match method:
        case "server/discover":
            return handle_discover(rpc_id)
        case "tools/list":
            return handle_list_tools(rpc_id)
        case "tools/call":
            return handle_call_tool(rpc_id, params)
        case _:
            return error(rpc_id, METHOD_NOT_FOUND, f"method {method!r} is not implemented")


def main() -> int:
    for line in sys.stdin:
        payload = line.strip()
        if not payload:
            continue
        try:
            request = json.loads(payload)
        except json.JSONDecodeError as exc:
            emit(error(None, PARSE_ERROR, f"invalid JSON: {exc.msg}"))
            continue
        if not isinstance(request, dict):
            emit(error(None, INVALID_REQUEST, "top-level JSON value must be an object"))
            continue
        response = dispatch(request)
        if response is not None:
            emit(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
