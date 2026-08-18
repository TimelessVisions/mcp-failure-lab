#!/usr/bin/env python3
"""HTTP probe sidecar: the only thing that ever talks to a contained target.

Runs inside a container on a Docker ``--internal`` network, alongside the
target. The target publishes no port to the host, so the harness cannot reach it
and never tries; this script is the sole path between the two.

It is deliberately small and dependency-free. Everything here runs next to code
the project does not trust, so it uses only the standard library and can be read
end to end in one sitting.

**It speaks no MCP.** It sends the exact bytes a plan gives it, records exactly
what came back, and lets the harness on the host do every interpretation. The
generic capabilities below — capture a header, capture a JSON value, substitute
a variable into a later request — are transport plumbing, not protocol
knowledge: nothing here knows what a session id *is*, only that a plan asked for
a named header to be carried forward. A probe that made judgements would be a
second, unreviewed implementation of the assertion engine.

Contract::

    argv:      --plan /path/plan.json --out /path/out
    plan:      see src/interop_lab/probe/plan.py (schema version is checked)
    writes:    <out>/wire.jsonl   one JSON event per line
               <out>/result.json  typed summary
    exit:      0 when the plan ran, non-zero only on probe failure

An exit code of 0 means "the probe did its job", not "the target behaved". Those
are different claims, and conflating them would let a target's failure look like
the harness's.

Unknown plan fields are a hard error. A probe that ignored a member it did not
recognise would report a run that never happened the way the plan asked for.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import hashlib
import json
import re
import socket
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

#: Must match interop_lab.probe.plan.PLAN_SCHEMA_VERSION.
PLAN_SCHEMA_VERSION = "1"
#: Must match interop_lab.probe.plan.PROBE_VERSION.
PROBE_VERSION = "2"

#: Absolute ceiling on a single response body, regardless of what a step asks
#: for. A plan is trusted; the target on the other end of the socket is not.
HARD_BODY_LIMIT = 1024 * 1024

_METHODS = ("POST", "GET", "DELETE")
_PHASES = ("setup", "scenario", "cleanup")
_BODY_MODES = ("none", "json", "text", "base64")
_ABORTS = ("none", "after_request", "after_headers", "after_partial_body")
_CAPTURE_SOURCES = ("response_header", "json_pointer", "status")
_SUBSTITUTION_TARGETS = ("header", "path", "body")


class ProbeError(Exception):
    """The probe itself could not run. Never a statement about the target."""


class PlanError(ProbeError):
    """The plan is not one this probe can execute exactly as written."""


# --------------------------------------------------------------- validation
#
# Hand-written rather than schema-driven because this file must stay
# dependency-free. `tests/unit/test_probe_plan.py` holds it to the same contract
# as the pydantic models on the host, so the two cannot drift apart unnoticed.


def _check_keys(value: Any, allowed: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlanError(f"{where}: expected an object, got {type(value).__name__}")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PlanError(
            f"{where}: unknown field(s) {unknown}. This probe implements plan schema "
            f"{PLAN_SCHEMA_VERSION}; refusing to run a plan it would only partly honour."
        )
    return value


def _enum(value: Any, allowed: tuple[str, ...], where: str) -> str:
    if value not in allowed:
        raise PlanError(f"{where}: expected one of {list(allowed)}, got {value!r}")
    return str(value)


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PlanError(f"{where}: expected a number, got {value!r}")
    return float(value)


def _string_map(value: Any, where: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise PlanError(f"{where}: expected an object of strings")
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise PlanError(f"{where}: header names and values must be strings")
        out[key] = item
    return out


CAPTURE_KEYS = {"variable", "source", "header", "pointer", "required"}
SUBSTITUTION_KEYS = {"variable", "target", "header", "pointer", "placeholder", "required"}
VARIABLE_KEYS = {"name", "description", "required"}
STEP_KEYS = {
    "step_id",
    "phase",
    "method",
    "path",
    "headers",
    "body_mode",
    "json_body",
    "text_body",
    "base64_body",
    "timeout_s",
    "delay_before_ms",
    "body_chunk_delay_ms",
    "abort",
    "response_body_limit_bytes",
    "reuse_connection",
    "keep_connection_open",
    "captures",
    "substitutions",
}
CLEANUP_KEYS = {"close_connections", "steps"}
PLAN_KEYS = {
    "plan_schema_version",
    "probe_version",
    "run_id",
    "protocol_profile",
    "base_url",
    "timeout_s",
    "ready_timeout_s",
    "max_events",
    "max_total_output_bytes",
    "retain_cookies",
    "variables",
    "setup_steps",
    "scenario_steps",
    "cleanup",
}


def validate_capture(raw: Any, where: str) -> dict[str, Any]:
    data = _check_keys(raw, CAPTURE_KEYS, where)
    source = _enum(data.get("source"), _CAPTURE_SOURCES, f"{where}.source")
    if not isinstance(data.get("variable"), str) or not data["variable"]:
        raise PlanError(f"{where}.variable must be a non-empty string")
    if source == "response_header" and not data.get("header"):
        raise PlanError(f"{where}: source=response_header requires `header`")
    if source == "json_pointer" and data.get("pointer") is None:
        raise PlanError(f"{where}: source=json_pointer requires `pointer`")
    if source == "status" and (data.get("header") or data.get("pointer")):
        raise PlanError(f"{where}: source=status takes neither `header` nor `pointer`")
    return {
        "variable": data["variable"],
        "source": source,
        "header": data.get("header"),
        "pointer": data.get("pointer"),
        "required": bool(data.get("required", False)),
    }


def validate_substitution(raw: Any, where: str) -> dict[str, Any]:
    data = _check_keys(raw, SUBSTITUTION_KEYS, where)
    target = _enum(data.get("target"), _SUBSTITUTION_TARGETS, f"{where}.target")
    if not isinstance(data.get("variable"), str) or not data["variable"]:
        raise PlanError(f"{where}.variable must be a non-empty string")
    if target == "header" and not data.get("header"):
        raise PlanError(f"{where}: target=header requires `header`")
    if target == "body" and data.get("pointer") is None:
        raise PlanError(f"{where}: target=body requires `pointer`")
    if target == "path" and not data.get("placeholder"):
        raise PlanError(f"{where}: target=path requires `placeholder`")
    return {
        "variable": data["variable"],
        "target": target,
        "header": data.get("header"),
        "pointer": data.get("pointer"),
        "placeholder": data.get("placeholder"),
        "required": bool(data.get("required", False)),
    }


def validate_step(raw: Any, where: str, *, expected_phase: str | None = None) -> dict[str, Any]:
    data = _check_keys(raw, STEP_KEYS, where)
    step_id = data.get("step_id")
    if not isinstance(step_id, str) or not step_id:
        raise PlanError(f"{where}.step_id must be a non-empty string")

    phase = _enum(data.get("phase"), _PHASES, f"{where}.phase")
    if expected_phase is not None and phase != expected_phase:
        raise PlanError(f"{where}: expected phase={expected_phase}, got {phase}")

    body_mode = _enum(data.get("body_mode", "none"), _BODY_MODES, f"{where}.body_mode")
    bodies = {
        "json": data.get("json_body"),
        "text": data.get("text_body"),
        "base64": data.get("base64_body"),
    }
    for name, value in bodies.items():
        if value is not None and body_mode != name:
            raise PlanError(f"{where}: sets a {name} body but body_mode={body_mode}")
    if body_mode != "none" and bodies[body_mode] is None:
        raise PlanError(f"{where}: body_mode={body_mode} but the matching body member is unset")

    path = data.get("path", "/")
    if not isinstance(path, str) or not path.startswith("/") or "://" in path:
        raise PlanError(f"{where}.path must be an absolute path, not a URL; got {path!r}")

    limit = int(_number(data.get("response_body_limit_bytes", 256 * 1024), f"{where}.limit"))
    return {
        "step_id": step_id,
        "phase": phase,
        "method": _enum(data.get("method", "POST"), _METHODS, f"{where}.method"),
        "path": path,
        "headers": _string_map(data.get("headers", {}), f"{where}.headers"),
        "body_mode": body_mode,
        "json_body": data.get("json_body"),
        "text_body": data.get("text_body"),
        "base64_body": data.get("base64_body"),
        "timeout_s": _number(data.get("timeout_s", 10.0), f"{where}.timeout_s"),
        "delay_before_ms": _number(data.get("delay_before_ms", 0.0), f"{where}.delay_before_ms"),
        "body_chunk_delay_ms": _number(
            data.get("body_chunk_delay_ms", 0.0), f"{where}.body_chunk_delay_ms"
        ),
        "abort": _enum(data.get("abort", "none"), _ABORTS, f"{where}.abort"),
        "response_body_limit_bytes": max(0, min(limit, HARD_BODY_LIMIT)),
        "reuse_connection": bool(data.get("reuse_connection", False)),
        "keep_connection_open": bool(data.get("keep_connection_open", False)),
        "captures": [
            validate_capture(item, f"{where}.captures[{i}]")
            for i, item in enumerate(data.get("captures", []) or [])
        ],
        "substitutions": [
            validate_substitution(item, f"{where}.substitutions[{i}]")
            for i, item in enumerate(data.get("substitutions", []) or [])
        ],
    }


def validate_plan(raw: Any) -> dict[str, Any]:
    """Fully validate a plan, or refuse to run it."""
    data = _check_keys(raw, PLAN_KEYS, "plan")

    version = data.get("plan_schema_version")
    if version != PLAN_SCHEMA_VERSION:
        raise PlanError(
            f"plan schema version {version!r} is not {PLAN_SCHEMA_VERSION!r}. Running it "
            "anyway would produce evidence for a plan this probe cannot execute."
        )

    variables: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(data.get("variables", []) or []):
        declared = _check_keys(item, VARIABLE_KEYS, f"plan.variables[{index}]")
        name = declared.get("name")
        if not isinstance(name, str) or not name:
            raise PlanError(f"plan.variables[{index}].name must be a non-empty string")
        variables[name] = {
            "name": name,
            "description": str(declared.get("description", "")),
            "required": bool(declared.get("required", False)),
        }

    setup = [
        validate_step(item, f"plan.setup_steps[{i}]", expected_phase="setup")
        for i, item in enumerate(data.get("setup_steps", []) or [])
    ]
    scenario = [
        validate_step(item, f"plan.scenario_steps[{i}]", expected_phase="scenario")
        for i, item in enumerate(data.get("scenario_steps", []) or [])
    ]
    cleanup_raw = _check_keys(data.get("cleanup", {}) or {}, CLEANUP_KEYS, "plan.cleanup")
    cleanup_steps = [
        validate_step(item, f"plan.cleanup.steps[{i}]", expected_phase="cleanup")
        for i, item in enumerate(cleanup_raw.get("steps", []) or [])
    ]

    seen: set[str] = set()
    for step in [*setup, *scenario, *cleanup_steps]:
        if step["step_id"] in seen:
            raise PlanError(f"duplicate step id {step['step_id']!r}; events are keyed by it")
        seen.add(step["step_id"])
        for capture in step["captures"]:
            if capture["variable"] not in variables:
                raise PlanError(
                    f"step {step['step_id']!r} captures into undeclared variable "
                    f"{capture['variable']!r}"
                )
        for substitution in step["substitutions"]:
            if substitution["variable"] not in variables:
                raise PlanError(
                    f"step {step['step_id']!r} substitutes undeclared variable "
                    f"{substitution['variable']!r}"
                )

    return {
        "plan_schema_version": version,
        "probe_version": str(data.get("probe_version", PROBE_VERSION)),
        "run_id": str(data.get("run_id", "")),
        "protocol_profile": str(data.get("protocol_profile", "")),
        "base_url": str(data.get("base_url", "")),
        "timeout_s": _number(data.get("timeout_s", 10.0), "plan.timeout_s"),
        "ready_timeout_s": _number(data.get("ready_timeout_s", 30.0), "plan.ready_timeout_s"),
        "max_events": int(_number(data.get("max_events", 512), "plan.max_events")),
        "max_total_output_bytes": int(
            _number(data.get("max_total_output_bytes", 4 * 1024 * 1024), "plan.max_output")
        ),
        "retain_cookies": bool(data.get("retain_cookies", False)),
        "variables": variables,
        "setup_steps": setup,
        "scenario_steps": scenario,
        "cleanup": {
            "close_connections": bool(cleanup_raw.get("close_connections", True)),
            "steps": cleanup_steps,
        },
    }


# ------------------------------------------------------------------- helpers


def json_pointer(document: Any, pointer: str) -> tuple[bool, Any]:
    """Resolve an RFC 6901 pointer. Returns ``(found, value)``."""
    if pointer == "":
        return True, document
    if not pointer.startswith("/"):
        return False, None
    current = document
    for token in pointer.split("/")[1:]:
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if key not in current:
                return False, None
            current = current[key]
        elif isinstance(current, list):
            if not key.lstrip("-").isdigit():
                return False, None
            index = int(key)
            if index < 0 or index >= len(current):
                return False, None
            current = current[index]
        else:
            return False, None
    return True, current


def json_pointer_set(document: Any, pointer: str, value: Any) -> bool:
    """Write through a pointer whose parent already exists. Never creates structure.

    Inventing missing parents would mean guessing the shape of a message body,
    which is exactly the protocol judgement the probe must not make.
    """
    if not pointer.startswith("/"):
        return False
    tokens = [t.replace("~1", "/").replace("~0", "~") for t in pointer.split("/")[1:]]
    if not tokens:
        return False
    found, parent = json_pointer(document, "/" + "/".join(tokens[:-1]) if tokens[:-1] else "")
    if not found:
        return False
    leaf = tokens[-1]
    if isinstance(parent, dict):
        parent[leaf] = value
        return True
    if isinstance(parent, list) and leaf.isdigit() and int(leaf) < len(parent):
        parent[int(leaf)] = value
        return True
    return False


def parse_headers(block: str) -> list[tuple[str, str]]:
    headers: list[tuple[str, str]] = []
    for line in block.split("\r\n"):
        if ":" in line:
            name, _, value = line.partition(":")
            headers.append((name.strip(), value.strip()))
    return headers


def header_value(headers: list[tuple[str, str]], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers:
        if key.lower() == lowered:
            return value
    return None


#: Content type an MCP server may use to frame a POST response. Both supported
#: profiles allow it, and the official TypeScript SDK uses it unconditionally —
#: a plain `initialize` comes back as `text/event-stream`, not JSON.
SSE_CONTENT_TYPE = "text/event-stream"


def decode_sse_response(text: str) -> tuple[Any, str | None]:
    """Pull the JSON-RPC message out of a *complete* SSE response body.

    This is response framing, not streaming. It applies to the body of a POST
    that the server has already finished writing and closed: the bytes are all
    present, and the only question is which of them are the message. Anything
    that would require staying connected — the long-lived GET subscription,
    resumption via `Last-Event-ID`, server-initiated messages — is deliberately
    not implemented here, and the scenarios that need it are reported
    NOT_APPLICABLE rather than approximated.

    The grammar is the one WHATWG defines: `field: value` lines, an event
    terminated by a blank line, and `data` accumulating across lines with `\\n`
    between them. A leading space after the colon is part of the delimiter and
    is stripped; further spaces are data.

    Returns the decoded message and a parse error, exactly one of which is set.
    The *last* JSON-decodable data payload wins: a server may emit a stream of
    notifications before the response, and the response is what a step asserts
    on. Returning the first would silently assert against a progress
    notification.
    """
    message: Any = None
    error: str | None = None
    seen_any_data = False

    for block in re.split(r"\r\n\r\n|\n\n|\r\r", text):
        data_lines: list[str] = []
        for line in re.split(r"\r\n|\n|\r", block):
            if not line or line.startswith(":"):
                # Blank lines separate events; a leading colon is a comment,
                # which the SDK uses as a keep-alive.
                continue
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "data":
                data_lines.append(value)
        if not data_lines:
            continue
        payload = "\n".join(data_lines)
        if not payload.strip():
            continue
        seen_any_data = True
        try:
            message = json.loads(payload)
            error = None
        except json.JSONDecodeError as exc:
            # Kept only if nothing later decodes, so one malformed keep-alive
            # frame cannot hide a valid response that follows it.
            if message is None:
                error = f"SSE data frame is not JSON: {exc}"

    if message is None and not seen_any_data:
        error = "SSE response contained no data frame"
    return message, error


class Connection:
    """A raw HTTP/1.1 connection.

    Raw sockets rather than ``http.client`` because the interesting behaviours
    here are the ones a polite client library hides: aborting after headers,
    chunking a request body slowly, and reading a bounded prefix of a response
    that never ends.
    """

    def __init__(self, host: str, port: int, timeout_s: float) -> None:
        self.socket = socket.create_connection((host, port), timeout=timeout_s)
        self.buffer = b""
        self.host = host
        self.port = port

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.socket.close()

    def send(self, payload: bytes, *, chunk_delay_ms: float) -> None:
        if chunk_delay_ms > 0 and len(payload) > 1:
            half = len(payload) // 2
            self.socket.sendall(payload[:half])
            time.sleep(chunk_delay_ms / 1000.0)
            self.socket.sendall(payload[half:])
        else:
            self.socket.sendall(payload)

    def read_until_headers(self, deadline: float) -> bytes:
        while b"\r\n\r\n" not in self.buffer:
            self._fill(deadline)
        head, _, rest = self.buffer.partition(b"\r\n\r\n")
        self.buffer = rest
        return head

    def read_exactly(self, count: int, deadline: float) -> bytes:
        while len(self.buffer) < count:
            self._fill(deadline)
        taken, self.buffer = self.buffer[:count], self.buffer[count:]
        return taken

    def read_line(self, deadline: float) -> bytes:
        while b"\r\n" not in self.buffer:
            self._fill(deadline)
        line, _, rest = self.buffer.partition(b"\r\n")
        self.buffer = rest
        return line

    def read_until_close(self, limit: int, deadline: float) -> tuple[bytes, bool]:
        """Read until the peer closes or ``limit`` bytes arrive."""
        while len(self.buffer) < limit:
            try:
                self._fill(deadline)
            except ConnectionError:
                break
        if len(self.buffer) >= limit:
            taken, self.buffer = self.buffer[:limit], self.buffer[limit:]
            return taken, True
        taken, self.buffer = self.buffer, b""
        return taken, False

    def _fill(self, deadline: float) -> None:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError("deadline expired")
        self.socket.settimeout(remaining)
        chunk = self.socket.recv(65536)
        if not chunk:
            raise ConnectionError("peer closed the connection")
        self.buffer += chunk


def _now_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


class Probe:
    """Executes one plan against one target, recording exactly what happened."""

    def __init__(self, plan: dict[str, Any], out: Path) -> None:
        self.plan = plan
        self.out = out
        self.events: list[dict[str, Any]] = []
        self.seq = 0
        self.started = time.perf_counter()
        self.output_bytes = 0
        self.truncated_output = False
        self.variables: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.connection: Connection | None = None
        self.stopped_early: str | None = None

        parsed = urlparse(plan["base_url"])
        if parsed.scheme != "http":
            raise ProbeError(f"unsupported scheme {parsed.scheme!r}")
        self.host = parsed.hostname or ""
        self.port = parsed.port or 80
        self.base_path = parsed.path or "/"

    # ----------------------------------------------------------- recording

    def record(self, **fields: Any) -> None:
        """Append one event, respecting the plan's output budget.

        Both caps are enforced here rather than at write time so a truncated run
        still says so in its own output instead of simply having fewer lines
        than it should.
        """
        if self.truncated_output:
            return
        if len(self.events) >= int(self.plan["max_events"]):
            self.truncated_output = True
            self.events.append(
                {
                    "seq": self.seq,
                    "at_ms": round(_now_ms(self.started), 3),
                    "direction": "lifecycle",
                    "note": f"output truncated: max_events {self.plan['max_events']} reached",
                    "output_truncated": True,
                }
            )
            return

        event: dict[str, Any] = {"seq": self.seq, "at_ms": round(_now_ms(self.started), 3)}
        event.update(fields)
        encoded = len(json.dumps(event, sort_keys=True, default=str))
        if self.output_bytes + encoded > int(self.plan["max_total_output_bytes"]):
            self.truncated_output = True
            self.events.append(
                {
                    "seq": self.seq,
                    "at_ms": round(_now_ms(self.started), 3),
                    "direction": "lifecycle",
                    "note": "output truncated: max_total_output_bytes reached",
                    "output_truncated": True,
                }
            )
            return
        self.output_bytes += encoded
        self.seq += 1
        self.events.append(event)

    # ------------------------------------------------------------- running

    def wait_for_target(self, deadline_s: float) -> bool:
        """Wait for the target to accept connections, without sending protocol."""
        deadline = time.perf_counter() + deadline_s
        while time.perf_counter() < deadline:
            try:
                with socket.create_connection((self.host, self.port), timeout=1.0):
                    self.record(direction="lifecycle", note="target accepting connections")
                    return True
            except OSError:
                time.sleep(0.2)
        self.record(direction="lifecycle", note="target never accepted a connection")
        return False

    def run(self) -> dict[str, Any]:
        if not self.wait_for_target(float(self.plan["ready_timeout_s"])):
            return self._result("target_unreachable", "target never accepted a connection")

        self.record(direction="lifecycle", note="setup phase begins", phase="setup")
        self._run_steps(self.plan["setup_steps"])

        missing = [
            name
            for name, declared in self.plan["variables"].items()
            if declared["required"] and name not in self.variables
        ]
        if missing:
            # Continuing would run the scenario against a target that never
            # granted the state the plan said was required, and the artifact
            # would show scenario failures caused by the missing setup.
            detail = f"required variable(s) never captured during setup: {missing}"
            self.record(direction="lifecycle", note=detail, phase="setup")
            self._teardown()
            return self._result("setup_incomplete", detail)

        self.record(direction="lifecycle", note="scenario phase begins", phase="scenario")
        self._run_steps(self.plan["scenario_steps"])

        self._teardown()
        if self.stopped_early is not None:
            return self._result("completed", f"plan executed; {self.stopped_early}")
        return self._result("completed", "plan executed")

    def _teardown(self) -> None:
        self.record(direction="lifecycle", note="cleanup phase begins", phase="cleanup")
        self._run_steps(self.plan["cleanup"]["steps"])
        if self.plan["cleanup"]["close_connections"]:
            self._drop_connection("cleanup closed the connection")

    def _run_steps(self, steps: list[dict[str, Any]]) -> None:
        for step in steps:
            try:
                self._execute(step)
            except TimeoutError:
                self.record(
                    direction="lifecycle",
                    phase=step["phase"],
                    step_id=step["step_id"],
                    note=f"step {step['step_id']} timed out after {step['timeout_s']}s",
                    timed_out=True,
                    timeout_cause="target_no_response",
                )
                self._drop_connection("connection dropped after timeout")
            except OSError as exc:
                # A reset is a real observation about the target, not a probe
                # failure, so it is recorded and the plan continues.
                self.record(
                    direction="lifecycle",
                    phase=step["phase"],
                    step_id=step["step_id"],
                    note=f"step {step['step_id']} transport error: {type(exc).__name__}: {exc}",
                    transport_error=f"{type(exc).__name__}: {exc}",
                )
                self._drop_connection("connection dropped after transport error")
            except PlanError as exc:
                if step["phase"] == "cleanup":
                    # Cleanup is best effort by construction: a teardown step
                    # whose precondition never materialised is skipped and said
                    # so, not treated as a failure of the run it follows.
                    self.record(
                        direction="lifecycle",
                        phase=step["phase"],
                        step_id=step["step_id"],
                        note=f"cleanup step {step['step_id']} skipped: {exc}",
                        skipped=True,
                    )
                    continue
                # A setup or scenario step that cannot be executed exactly is
                # stopped rather than approximated: a partly-honoured step is
                # evidence for a run nobody asked for.
                self.stopped_early = str(exc)
                self.record(
                    direction="lifecycle",
                    phase=step["phase"],
                    step_id=step["step_id"],
                    note=f"step {step['step_id']} could not be executed as written: {exc}",
                    plan_error=str(exc),
                )
                return

    # ------------------------------------------------------------ one step

    def _body_bytes(self, step: dict[str, Any]) -> tuple[bytes, dict[str, Any] | None]:
        """Produce the exact request body, and the JSON object it came from."""
        mode = step["body_mode"]
        if mode == "none":
            return b"", None
        if mode == "json":
            document = json.loads(json.dumps(step["json_body"]))  # deep copy
            self._substitute_body(step, document)
            return json.dumps(document, separators=(",", ":")).encode(), document
        if mode == "text":
            # Byte-exact. No JSON round trip, so a truncated or malformed
            # payload stays truncated and malformed on the wire.
            return str(step["text_body"]).encode("utf-8"), None
        try:
            return base64.b64decode(str(step["base64_body"]), validate=True), None
        except (binascii.Error, ValueError) as exc:
            raise PlanError(f"base64_body is not valid base64: {exc}") from exc

    def _substitute_body(self, step: dict[str, Any], document: Any) -> None:
        for substitution in step["substitutions"]:
            if substitution["target"] != "body":
                continue
            value = self.variables.get(substitution["variable"])
            if value is None:
                if substitution["required"]:
                    raise PlanError(
                        f"required variable {substitution['variable']!r} is unset, so the "
                        f"body substitution at {substitution['pointer']!r} cannot be made"
                    )
                continue
            if not json_pointer_set(document, str(substitution["pointer"]), value):
                raise PlanError(
                    f"body pointer {substitution['pointer']!r} has no existing parent; the "
                    "probe does not invent message structure"
                )

    def _resolve_path(self, step: dict[str, Any]) -> str:
        path = step["path"]
        for substitution in step["substitutions"]:
            if substitution["target"] != "path":
                continue
            value = self.variables.get(substitution["variable"])
            if value is None:
                if substitution["required"]:
                    raise PlanError(
                        f"required variable {substitution['variable']!r} is unset, so the "
                        "path substitution cannot be made"
                    )
                continue
            path = path.replace(str(substitution["placeholder"]), value)
        return path

    def _resolve_headers(self, step: dict[str, Any]) -> dict[str, str]:
        headers = dict(step["headers"])
        for substitution in step["substitutions"]:
            if substitution["target"] != "header":
                continue
            value = self.variables.get(substitution["variable"])
            if value is None:
                if substitution["required"]:
                    raise PlanError(
                        f"required variable {substitution['variable']!r} is unset, so header "
                        f"{substitution['header']!r} cannot be set"
                    )
                continue
            headers[str(substitution["header"])] = value
        if self.plan["retain_cookies"] and self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in sorted(self.cookies.items()))
        return headers

    def _execute(self, step: dict[str, Any]) -> None:
        if step["delay_before_ms"]:
            time.sleep(step["delay_before_ms"] / 1000.0)

        body, _ = self._body_bytes(step)
        path = self._resolve_path(step)
        headers = self._resolve_headers(step)
        timeout_s = float(step["timeout_s"])
        keep_open = bool(step["keep_connection_open"])

        connection = self._connection_for(step, timeout_s)
        request_lines = [
            f"{step['method']} {path} HTTP/1.1",
            f"Host: {self.host}:{self.port}",
        ]
        request_lines += [f"{name}: {value}" for name, value in headers.items()]
        if body or step["method"] == "POST":
            request_lines.append(f"Content-Length: {len(body)}")
        request_lines.append("Connection: keep-alive" if keep_open else "Connection: close")
        head = ("\r\n".join(request_lines) + "\r\n\r\n").encode()

        self.record(
            direction="outbound",
            phase=step["phase"],
            step_id=step["step_id"],
            method=step["method"],
            path=path,
            headers=headers,
            body=_safe_text(body, step["response_body_limit_bytes"]),
            body_bytes=len(body),
            body_sha256=hashlib.sha256(body).hexdigest(),
            reused_connection=bool(step["reuse_connection"] and self.connection is not None),
        )

        started = time.perf_counter()
        deadline = started + timeout_s
        connection.send(head, chunk_delay_ms=0.0)
        connection.send(body, chunk_delay_ms=float(step["body_chunk_delay_ms"]))

        if step["abort"] == "after_request":
            self._drop_connection("client aborted after the request, reading nothing")
            self.record(
                direction="lifecycle",
                phase=step["phase"],
                step_id=step["step_id"],
                note="client aborted after request",
                aborted="after_request",
            )
            return

        try:
            head_bytes = connection.read_until_headers(deadline)
        except TimeoutError:
            # No status line at all. Recorded distinctly from a stalled body:
            # the two are different defects, and one undifferentiated "timeout"
            # cannot tell a server that never answered from one that answered
            # promptly and then stopped writing.
            self.record(
                direction="lifecycle",
                phase=step["phase"],
                step_id=step["step_id"],
                method=step["method"],
                path=path,
                note=f"header timeout after {timeout_s}s (no status line)",
                timed_out=True,
                timeout_cause="target_no_response",
            )
            self._drop_connection("connection dropped after a header timeout")
            return

        first_byte_ms = (time.perf_counter() - started) * 1000.0
        head_text = head_bytes.decode("utf-8", errors="replace")
        status_line, _, header_block = head_text.partition("\r\n")
        parts = status_line.split(" ", 2)
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        response_headers = parse_headers(header_block)

        if self.plan["retain_cookies"]:
            self._absorb_cookies(response_headers)

        if step["abort"] == "after_headers":
            self._drop_connection("client aborted after headers")
            self.record(
                direction="inbound",
                phase=step["phase"],
                step_id=step["step_id"],
                method=step["method"],
                path=path,
                status=status,
                headers=dict(response_headers),
                first_byte_ms=round(first_byte_ms, 3),
                body_bytes=0,
                body_truncated=True,
                aborted="after_headers",
                note="body deliberately not read",
            )
            self._apply_captures(step, status, response_headers, None)
            return

        try:
            payload, truncated, body_error = self._read_body(
                connection,
                response_headers,
                limit=int(step["response_body_limit_bytes"]),
                deadline=deadline,
                abort_partial=step["abort"] == "after_partial_body",
            )
        except TimeoutError:
            # Headers arrived and the body did not finish. The header timing is
            # carried on the event, because "the server answered in 4ms and then
            # stalled" and "the server never answered" are opposite findings.
            self.record(
                direction="lifecycle",
                phase=step["phase"],
                step_id=step["step_id"],
                method=step["method"],
                path=path,
                status=status,
                headers=dict(response_headers),
                first_byte_ms=round(first_byte_ms, 3),
                body_truncated=True,
                note=(
                    f"body-completion timeout after {timeout_s}s "
                    f"(headers arrived at {first_byte_ms:.0f}ms)"
                ),
                timed_out=True,
                timeout_cause="target_no_response",
            )
            self._drop_connection("connection dropped after a body-completion timeout")
            self._apply_captures(step, status, response_headers, None)
            return

        body_complete_ms = (time.perf_counter() - started) * 1000.0

        text = payload.decode("utf-8", errors="replace")
        parsed: Any = None
        parse_error: str | None = None
        # The raw bytes are recorded either way, below and unaltered. Only the
        # decoded `message` depends on the framing, so a trace still shows
        # exactly what came off the wire.
        framing = (header_value(response_headers, "Content-Type") or "").lower()
        if not text.strip():
            parse_error = "response had an empty body"
        elif framing.startswith(SSE_CONTENT_TYPE):
            parsed, parse_error = decode_sse_response(text)
        else:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                parse_error = str(exc)

        self.record(
            direction="inbound",
            phase=step["phase"],
            step_id=step["step_id"],
            method=step["method"],
            path=path,
            status=status,
            headers=dict(response_headers),
            body=_safe_text(payload, int(step["response_body_limit_bytes"])),
            body_bytes=len(payload),
            body_sha256=hashlib.sha256(payload).hexdigest(),
            body_truncated=truncated,
            message=parsed if isinstance(parsed, dict) else None,
            parse_error=parse_error,
            first_byte_ms=round(first_byte_ms, 3),
            body_complete_ms=round(body_complete_ms, 3),
            aborted="after_partial_body" if step["abort"] == "after_partial_body" else None,
            note=body_error,
        )

        self._apply_captures(step, status, response_headers, parsed)

        if not keep_open or step["abort"] == "after_partial_body":
            self._drop_connection("connection closed after the exchange")

    def _connection_for(self, step: dict[str, Any], timeout_s: float) -> Connection:
        if step["reuse_connection"] and self.connection is not None:
            return self.connection
        self._drop_connection("connection replaced for a step requesting a new one")
        connection = Connection(self.host, self.port, timeout_s)
        self.connection = connection
        return connection

    def _drop_connection(self, note: str) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None
            self.record(direction="lifecycle", note=note)

    def _read_body(
        self,
        connection: Connection,
        headers: list[tuple[str, str]],
        *,
        limit: int,
        deadline: float,
        abort_partial: bool,
    ) -> tuple[bytes, bool, str | None]:
        """Read a bounded response body. Returns ``(bytes, truncated, note)``."""
        encoding = (header_value(headers, "Transfer-Encoding") or "").lower()
        length_header = header_value(headers, "Content-Length")

        if "chunked" in encoding:
            return self._read_chunked(connection, limit=limit, deadline=deadline)

        if length_header is not None and length_header.isdigit():
            declared = int(length_header)
            if declared > limit:
                return (
                    connection.read_exactly(limit, deadline),
                    True,
                    (f"body declared {declared} bytes, read {limit} before the cap"),
                )
            if abort_partial and declared > 1:
                half = declared // 2
                return (
                    connection.read_exactly(half, deadline),
                    True,
                    ("client aborted part-way through the body"),
                )
            return connection.read_exactly(declared, deadline), False, None

        payload, hit_limit = connection.read_until_close(limit, deadline)
        return (
            payload,
            hit_limit,
            ("no Content-Length; read until close" if not hit_limit else None),
        )

    def _read_chunked(
        self, connection: Connection, *, limit: int, deadline: float
    ) -> tuple[bytes, bool, str | None]:
        body = b""
        while True:
            line = connection.read_line(deadline)
            size_text = line.split(b";", 1)[0].strip()
            try:
                size = int(size_text, 16)
            except ValueError:
                return body, True, f"malformed chunk size {size_text!r}"
            if size == 0:
                connection.read_line(deadline)  # trailing CRLF
                return body, False, None
            body += connection.read_exactly(size, deadline)
            connection.read_line(deadline)  # CRLF after each chunk
            if len(body) >= limit:
                return body[:limit], True, "chunked body exceeded the cap"

    def _absorb_cookies(self, headers: list[tuple[str, str]]) -> None:
        for name, value in headers:
            if name.lower() != "set-cookie":
                continue
            pair = value.split(";", 1)[0]
            if "=" in pair:
                key, _, cookie = pair.partition("=")
                self.cookies[key.strip()] = cookie.strip()

    def _apply_captures(
        self,
        step: dict[str, Any],
        status: int,
        headers: list[tuple[str, str]],
        parsed: Any,
    ) -> None:
        for capture in step["captures"]:
            value: str | None = None
            if capture["source"] == "response_header":
                value = header_value(headers, str(capture["header"]))
            elif capture["source"] == "status":
                value = str(status)
            else:
                found, found_value = json_pointer(parsed, str(capture["pointer"]))
                if found and found_value is not None and not isinstance(found_value, dict | list):
                    value = str(found_value)

            if value is None:
                if capture["required"]:
                    raise PlanError(
                        f"required capture into {capture['variable']!r} found no value at "
                        f"{capture['source']} on step {step['step_id']!r}"
                    )
                self.record(
                    direction="lifecycle",
                    phase=step["phase"],
                    step_id=step["step_id"],
                    note=f"optional capture into {capture['variable']!r} found no value",
                    capture_missed=capture["variable"],
                )
                continue

            self.variables[capture["variable"]] = value
            self.record(
                direction="lifecycle",
                phase=step["phase"],
                step_id=step["step_id"],
                note=f"captured {capture['variable']!r} from {capture['source']}",
                captured=capture["variable"],
            )

    # ------------------------------------------------------------- output

    def _result(self, outcome: str, detail: str) -> dict[str, Any]:
        return {
            "probe_version": PROBE_VERSION,
            "plan_schema_version": PLAN_SCHEMA_VERSION,
            "run_id": self.plan.get("run_id", ""),
            "outcome": outcome,
            "detail": detail,
            "event_count": len(self.events),
            "output_truncated": self.truncated_output,
            "captured_variables": sorted(self.variables),
            "duration_ms": round(_now_ms(self.started), 3),
        }

    def write(self, result: dict[str, Any]) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        with (self.out / "wire.jsonl").open("w", encoding="utf-8") as handle:
            for event in self.events:
                handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")
        (self.out / "result.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def _safe_text(payload: bytes, limit: int) -> str:
    text = payload[:limit].decode("utf-8", errors="replace")
    if len(payload) > limit:
        text += f"\n<truncated at {limit} bytes>"
    return text


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Contained HTTP probe.")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--print-version", action="store_true")
    args = parser.parse_args(argv[1:])

    if args.print_version:
        print(json.dumps({"probe_version": PROBE_VERSION, "plan_schema": PLAN_SCHEMA_VERSION}))
        return 0

    try:
        raw = json.loads(args.plan.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"probe: cannot read plan: {exc}", file=sys.stderr)
        return 2

    try:
        plan = validate_plan(raw)
    except PlanError as exc:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "result.json").write_text(
            json.dumps(
                {
                    "probe_version": PROBE_VERSION,
                    "plan_schema_version": PLAN_SCHEMA_VERSION,
                    "outcome": "plan_rejected",
                    "detail": str(exc),
                    "event_count": 0,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"probe: {exc}", file=sys.stderr)
        return 4

    probe = Probe(plan, args.out)
    try:
        result = probe.run()
    except ProbeError as exc:
        result = probe._result("probe_error", str(exc))
        probe.write(result)
        print(f"probe: {exc}", file=sys.stderr)
        return 3

    probe.write(result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
