#!/usr/bin/env python3
"""Local Streamable HTTP MCP fixture. Dependency-free, localhost-only.

Serves both dated profiles so the harness has a controlled HTTP target whose
conformance is fully owned by this repository:

  INTEROP_FIXTURE_PROFILE   "2026-07-28" (default) or "2025-11-25"
  INTEROP_FIXTURE_PORT      port to bind (default 0 = ephemeral, printed)
  INTEROP_FIXTURE_DEFECTS   comma-separated switches (see DEFECTS)
  INTEROP_FIXTURE_DELAY_MS  delay before the response body completes
  INTEROP_FIXTURE_HEADER_DELAY_MS  delay before response headers are sent
  INTEROP_FIXTURE_BIND      bind address (default 127.0.0.1)
  INTEROP_FIXTURE_SESSIONS  "1" to issue Mcp-Session-Id (2025-11-25 only)

Binds to 127.0.0.1 by default, so a fixture started on a developer machine can
never be reached from anywhere else.

`INTEROP_FIXTURE_BIND=0.0.0.0` exists for contained execution only. Inside a
container on a Docker `--internal` network with no published port, binding all
interfaces exposes the fixture to exactly one thing: the probe sidecar on the
same network. There the isolation is provided by the network rather than by the
bind address, and binding loopback would make the fixture unreachable by the
only client allowed to talk to it. It must never be set for host execution.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PROFILE = os.environ.get("INTEROP_FIXTURE_PROFILE", "2026-07-28")

#: Dated revisions this fixture will answer for. Anything else on the
#: MCP-Protocol-Version header is refused with 400, as both bindings require.
KNOWN_PROTOCOL_VERSIONS = frozenset({"2025-11-25", "2026-07-28", "2025-06-18", "2025-03-26"})


def _is_permitted_origin(origin: str) -> bool:
    """Whether an Origin names a host this fixture is willing to serve.

    Loopback only, and by literal comparison rather than by parsing: this
    fixture binds to localhost and exists to answer the harness. Anything else
    is a page that found it, which is exactly the case Origin validation is for.
    """
    if origin.strip().lower() == "null":
        return False
    for scheme in ("http://", "https://"):
        if origin.startswith(scheme):
            host = origin[len(scheme) :].split("/", 1)[0].rsplit(":", 1)[0]
            return host in ("127.0.0.1", "localhost", "[::1]", "::1")
    return False


PORT = int(os.environ.get("INTEROP_FIXTURE_PORT", "0"))
#: Loopback by default. See the module docstring before changing it.
BIND = os.environ.get("INTEROP_FIXTURE_BIND", "127.0.0.1")
DELAY_MS = int(os.environ.get("INTEROP_FIXTURE_DELAY_MS", "0"))
HEADER_DELAY_MS = int(os.environ.get("INTEROP_FIXTURE_HEADER_DELAY_MS", "0"))

#: Whether this fixture issues a session id on `initialize`.
#:
#: The 2025-11-25 Streamable HTTP binding says a server **MAY** assign one, and
#: that a client which received one **MUST** send it back on later requests. Both
#: halves of that are worth exercising, and neither is universal — so it is a
#: switch rather than a default, and the harness never assumes a session id
#: exists. Meaningless outside the 2025 profile, which has no handshake to
#: attach a session to.
SESSIONS = os.environ.get("INTEROP_FIXTURE_SESSIONS") == "1" and PROFILE == "2025-11-25"

#: Session ids this fixture has issued and not yet been asked to forget.
SESSIONS_ISSUED: set[str] = set()

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022

META_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"

DEFECTS = {
    # Accept a Mcp-Method header that disagrees with the body instead of
    # rejecting it with 400 / -32020.
    "ignore_header_body_mismatch",
    # Answer a header/body mismatch with 200 and a result.
    "mismatch_returns_200_result",
    # Drop the request id from the error response.
    "error_loses_request_id",
    # Reset the connection mid-body.
    "reset_connection",
    # Omit the required resultType member.
    "omit_result_type",
}


def active_defects() -> set[str]:
    raw = os.environ.get("INTEROP_FIXTURE_DEFECTS", "")
    requested = {item.strip() for item in raw.split(",") if item.strip()}
    unknown = requested - DEFECTS
    if unknown:
        print(f"fixture: unknown defect switches {sorted(unknown)}", file=sys.stderr, flush=True)
    return requested & DEFECTS


DEFECT = active_defects()

TOOLS: list[dict[str, Any]] = [
    {
        "name": "echo",
        "description": "Echo the provided text back to the caller.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }
]


def with_result_type(result: dict[str, Any]) -> dict[str, Any]:
    if PROFILE != "2026-07-28" or "omit_result_type" in DEFECT:
        return result
    return {**result, "resultType": "complete"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "interop-lab-http-fixture/0.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Per-request access logging, off by default.

        stderr is drained by a separate task, so a log line can land between two
        protocol events and shift every subsequent sequence number. That makes
        captured traces non-deterministic for no diagnostic benefit. Set
        INTEROP_FIXTURE_VERBOSE=1 to turn it back on when debugging the fixture.
        """
        if os.environ.get("INTEROP_FIXTURE_VERBOSE") != "1":
            return
        sys.stderr.write("fixture: " + (format % args) + "\n")
        sys.stderr.flush()

    # ------------------------------------------------------------ responses

    def _send(
        self,
        status: int,
        payload: dict[str, Any] | None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = b"" if payload is None else json.dumps(payload).encode()

        if HEADER_DELAY_MS > 0:
            time.sleep(HEADER_DELAY_MS / 1000)

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("MCP-Protocol-Version", PROFILE)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()

        if DELAY_MS > 0:
            # Deliberately stall between headers and body completion so the
            # harness can distinguish a header timeout from a body timeout.
            self.wfile.write(body[: len(body) // 2])
            self.wfile.flush()
            time.sleep(DELAY_MS / 1000)
            self.wfile.write(body[len(body) // 2 :])
        else:
            self.wfile.write(body)
        self.wfile.flush()

    def _error(self, status: int, rpc_id: Any, code: int, message: str, data: Any = None) -> None:
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        response_id = None if "error_loses_request_id" in DEFECT else rpc_id
        self._send(status, {"jsonrpc": "2.0", "id": response_id, "error": err})

    # -------------------------------------------------------------- routing

    def _host_allowed(self) -> bool:
        """DNS-rebinding protection: only local Host values are served.

        A browser can be tricked into resolving an attacker-controlled name to
        127.0.0.1 and posting to a local MCP server. Validating Host is the
        defence, and the official conformance suite checks for it — it caught
        this fixture missing it.
        """
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        allowed = {"127.0.0.1", "localhost", "::1", "[::1]", ""}
        if BIND != "127.0.0.1":
            # Under sidecar containment the probe addresses the fixture by its
            # Docker network alias, so that name is a legitimate Host. This is
            # not a weakening of the rebinding defence: the fixture is on an
            # internal network with no published port, so no browser can reach
            # it at all.
            allowed.add("target")
        return host in allowed

    def _reject_unconforming_request(self) -> bool:
        """Transport-binding checks both dated revisions state identically.

        A conforming fixture is the positive control every third-party result is
        compared against. One that skipped these would make the harness's own
        detection of them unproven — and would quietly report a passing run for
        a server that does not validate Origin, which is the check standing
        between a locally bound MCP server and any web page the user loads.
        """
        origin = self.headers.get("Origin")
        if origin is not None and not _is_permitted_origin(origin):
            # "If the Origin header is present and invalid, servers MUST respond
            # with HTTP 403 Forbidden" — 2025-11-25 and 2026-07-28 alike.
            self._error(403, None, INVALID_REQUEST, f"Origin {origin!r} is not permitted")
            return True

        version = self.headers.get("MCP-Protocol-Version")
        if version is not None and version not in KNOWN_PROTOCOL_VERSIONS:
            # "If the server receives a request with an invalid or unsupported
            # MCP-Protocol-Version, it MUST respond with 400 Bad Request."
            self._error(
                400,
                None,
                INVALID_REQUEST,
                f"unsupported MCP-Protocol-Version {version!r}",
            )
            return True
        return False

    def do_POST(self) -> None:
        if not self._host_allowed():
            self._error(
                403,
                None,
                INVALID_REQUEST,
                f"Host header {self.headers.get('Host')!r} is not a permitted local host",
            )
            return
        if "reset_connection" in DEFECT:
            # Abort without any response at all.
            self.close_connection = True
            with contextlib.suppress(OSError):
                self.connection.shutdown(socket.SHUT_RDWR)
            return

        # Before the body is read, because both checks are about the request
        # rather than the message, and a server that parsed a body from a
        # disallowed Origin first has already done the work the check exists to
        # prevent.
        if self._reject_unconforming_request():
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""

        try:
            request = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._error(400, None, PARSE_ERROR, f"invalid JSON: {exc.msg}")
            return
        except UnicodeDecodeError as exc:
            # `json.loads` on bytes decodes as UTF-8 first, so a body that is not
            # valid UTF-8 raises here rather than as a JSONDecodeError. Catching
            # only the latter killed the handler thread and dropped the
            # connection with no response — found by the probe's raw-bytes test,
            # which is the first thing able to send such a body.
            self._error(400, None, PARSE_ERROR, f"body is not valid UTF-8: {exc.reason}")
            return
        if not isinstance(request, dict):
            self._error(400, None, INVALID_REQUEST, "top-level JSON value must be an object")
            return

        rpc_id = request.get("id")
        method = request.get("method")
        params = request.get("params")

        if request.get("jsonrpc") != "2.0":
            self._error(400, rpc_id, INVALID_REQUEST, "jsonrpc member must be '2.0'")
            return
        if not isinstance(method, str):
            self._error(400, rpc_id, INVALID_REQUEST, "method member must be a string")
            return

        if PROFILE == "2026-07-28":
            failure = self._check_2026_headers(method, params, is_request="id" in request)
            if failure is not None:
                status, code, message = failure
                self._error(status, rpc_id, code, message)
                return

        if SESSIONS and method != "initialize":
            # A client that received a session id must send it back. Enforcing
            # it is what makes the harness's setup-state capture load-bearing:
            # without the captured id, every scenario request here fails, so a
            # passing run proves the capture and substitution actually worked.
            session_id = self.headers.get("Mcp-Session-Id")
            if session_id is None:
                self._error(400, rpc_id, INVALID_REQUEST, "Mcp-Session-Id header is required")
                return
            if session_id not in SESSIONS_ISSUED:
                self._error(404, rpc_id, INVALID_REQUEST, "unknown or terminated session")
                return

        if not isinstance(params, dict):
            params = {}

        # A notification carries no id, and both dated bindings say the same
        # thing about one the server accepts: HTTP 202 Accepted with no body.
        #
        # This fixture used to fall through to the method dispatch below, which
        # answered `notifications/initialized` with a 200 and an error body
        # addressed to a null id — a JSON-RPC response to a message that must
        # not be answered at all. It went unnoticed because the harness only
        # ever sent notifications it did not read the answer to. A conforming
        # fixture is the positive control every third-party result is compared
        # against, so a divergence here is a fixture defect and is fixed rather
        # than baselined.
        if "id" not in request:
            self._send(202, None)
            return

        match method:
            case "initialize" if PROFILE == "2025-11-25":
                headers: dict[str, str] = {}
                if SESSIONS:
                    session_id = uuid.uuid4().hex
                    SESSIONS_ISSUED.add(session_id)
                    headers["Mcp-Session-Id"] = session_id
                self._send(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "result": {
                            "protocolVersion": PROFILE,
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "interop-lab-http-fixture", "version": "0.1.0"},
                        },
                    },
                    headers,
                )
            case "server/discover":
                self._send(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "result": with_result_type(
                            {
                                "capabilities": {"tools": {}},
                                "serverInfo": {
                                    "name": "interop-lab-http-fixture",
                                    "version": "0.1.0",
                                },
                                "protocolVersions": [PROFILE],
                                "cacheScope": "public",
                                "ttlMs": 60_000,
                            }
                        ),
                    },
                )
            case "tools/list":
                self._send(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "result": with_result_type(
                            {"tools": TOOLS, "cacheScope": "public", "ttlMs": 60_000}
                        ),
                    },
                )
            case "tools/call":
                name = params.get("name")
                if name != "echo":
                    self._error(200, rpc_id, INVALID_PARAMS, f"unknown tool {name!r}")
                    return
                text = (params.get("arguments") or {}).get("text", "")
                self._send(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "result": with_result_type(
                            {"content": [{"type": "text", "text": str(text)}], "isError": False}
                        ),
                    },
                )
            case _:
                self._error(200, rpc_id, METHOD_NOT_FOUND, f"method {method!r} is not implemented")

    def _check_2026_headers(
        self, method: str, params: Any, *, is_request: bool = True
    ) -> tuple[int, int, str] | None:
        """Validate the 2026-07-28 routing headers against the body.

        Returns ``(status, code, message)`` when the request must be rejected.

        ``is_request`` decides whether the ``params._meta`` requirement applies.
        The 2026-07-28 schema puts it on ``RequestParams`` and deliberately not
        on ``NotificationParams``, which has no required members at all — and
        this fixture applied it to both, so it rejected a valid
        ``notifications/cancelled`` with -32602. That is the same error the whole
        project is organised against, committed in this repository's own
        conforming fixture: a requirement asserted outside the shape it governs.
        """
        header_version = self.headers.get("MCP-Protocol-Version")
        header_method = self.headers.get("Mcp-Method")

        if "ignore_header_body_mismatch" not in DEFECT:
            if header_method is not None and header_method != method:
                return (
                    400,
                    HEADER_MISMATCH,
                    f"Mcp-Method header {header_method!r} does not match body method {method!r}",
                )

            meta = params.get("_meta") if isinstance(params, dict) else None
            body_version = meta.get(META_VERSION_KEY) if isinstance(meta, dict) else None
            if (
                header_version is not None
                and body_version is not None
                and header_version != body_version
            ):
                return (
                    400,
                    HEADER_MISMATCH,
                    f"MCP-Protocol-Version header {header_version!r} does not match "
                    f"body {META_VERSION_KEY} {body_version!r}",
                )

            if body_version is not None and body_version != PROFILE:
                return (
                    400,
                    UNSUPPORTED_PROTOCOL_VERSION,
                    f"unsupported protocol version {body_version}",
                )
            if isinstance(meta, dict) and META_CAPABILITIES_KEY not in meta:
                return (400, INVALID_PARAMS, f"params._meta.{META_CAPABILITIES_KEY} is required")
            if meta is None and is_request:
                return (400, INVALID_PARAMS, "params._meta is required")

        return None

    def do_DELETE(self) -> None:
        """Session termination, as defined for the 2025-11-25 HTTP binding.

        A fixture that issues session ids but never lets one be released would
        make the harness's cleanup path untestable, and an untested cleanup path
        is how a "no resource survives" claim quietly stops being true.
        """
        if not self._host_allowed():
            self._error(403, None, INVALID_REQUEST, "Host header is not a permitted local host")
            return
        if not SESSIONS:
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.send_header("Allow", "POST")
            self.end_headers()
            return

        session_id = self.headers.get("Mcp-Session-Id")
        if session_id is None:
            self._error(400, None, INVALID_REQUEST, "Mcp-Session-Id header is required")
            return
        SESSIONS_ISSUED.discard(session_id)
        self._send(200, None)

    def do_GET(self) -> None:
        # No SSE stream is offered by this fixture; say so explicitly.
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.send_header("Allow", "POST")
        self.end_headers()


def main() -> int:
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    host, port = server.server_address[:2]
    # The harness reads this line to learn the ephemeral port.
    print(f"INTEROP_FIXTURE_READY http://{host}:{port}/mcp", flush=True)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        while True:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
