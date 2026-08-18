#!/usr/bin/env python3
"""Black-box target: official Python SDK (`mcp`) server over stdio.

This is a thin, unmodified use of the official SDK. Its purpose is to be a real
implementation under test — the harness must not special-case it, and this file
must not work around SDK behaviour. Whatever the SDK does on the wire is the
observation.

Pinned by `targets/python/sdk-server.yaml`.
"""

from __future__ import annotations

import asyncio

from mcp.server import MCPServer

server = MCPServer(name="interop-lab-python-sdk-target", version="0.1.0")


@server.tool()
def echo(text: str) -> str:
    """Echo the provided text back to the caller."""
    return text


@server.tool()
async def slow_echo(text: str, delay_ms: int = 0) -> str:
    """Echo after an optional caller-supplied delay, for timeout scenarios."""
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000)
    return text


if __name__ == "__main__":
    asyncio.run(server.run_stdio_async())
