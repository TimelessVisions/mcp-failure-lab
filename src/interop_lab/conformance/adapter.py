"""Adapter to the official MCP conformance framework.

The official suite is invoked as a subprocess and its results are *mapped*, not
replaced. This project does not reimplement happy-path checks, wire-schema
validation, or requirement-set logic — the official framework owns those, and
duplicating them would produce a second, worse answer to a question already
answered well.

The boundary is deliberately thin so the official package can move independently:
everything version-specific lives in ``tools/conformance/package.json`` and in
``conformance/models.py``.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

from interop_lab.conformance.models import (
    CONFORMANCE_PACKAGE,
    CONFORMANCE_VERSION,
    ConformanceOutcome,
    ConformanceRun,
)
from interop_lab.conformance.parser import (
    ConformanceParseError,
    find_checks_files,
    load_all_checks,
    summarise,
)
from interop_lab.models.core import TargetDefinition
from interop_lab.profiles.registry import get_profile

#: Where the pinned official package is installed.
TOOL_ROOT = Path("tools/conformance")
CLI_ENTRY = TOOL_ROOT / "node_modules" / CONFORMANCE_PACKAGE / "dist" / "index.js"

MAX_RAW_CAPTURE = 200_000


class ConformanceUnavailableError(Exception):
    """The pinned official framework is not installed."""


def is_available() -> bool:
    return CLI_ENTRY.is_file() and shutil.which("node") is not None


def unavailable_reason() -> str:
    if shutil.which("node") is None:
        return "node is not on PATH"
    if not CLI_ENTRY.is_file():
        return (
            f"{CLI_ENTRY} is missing; run `npm ci` in {TOOL_ROOT} to install "
            f"{CONFORMANCE_PACKAGE}@{CONFORMANCE_VERSION}"
        )
    return "available"


class ConformanceAdapter:
    """Runs the official CLI against a live local server and maps its results."""

    def __init__(self, *, output_root: Path) -> None:
        self.output_root = output_root

    async def run_server(
        self,
        *,
        target: TargetDefinition,
        url: str,
        scenario: str | None = None,
        timeout_s: float = 180.0,
    ) -> ConformanceRun:
        """Execute the official server suite against ``url``.

        ``--spec-version`` is derived from the target's dated profile, so the
        official suite is never asked to test a target at a revision the target
        does not implement.
        """
        profile = get_profile(target.protocol_profile)
        # A fresh directory per invocation: the CLI writes one subdirectory per
        # scenario, and reusing a directory would mix this run's results with a
        # previous one's.
        output_dir = (
            self.output_root / "conformance" / target.target_id / f"run-{int(time.time() * 1000)}"
        )
        output_dir.mkdir(parents=True, exist_ok=True)

        command = [
            "node",
            str(CLI_ENTRY),
            "server",
            "--url",
            url,
            "--spec-version",
            profile.spec_version,
            "--verbose",
            "--output-dir",
            str(output_dir),
        ]
        if scenario:
            command.extend(["--scenario", scenario])

        base = ConformanceRun(
            requirements=profile.spec_version,
            target_id=target.target_id,
            url=url,
            command=command,
            outcome=ConformanceOutcome.HARNESS_FAILURE,
        )

        if not is_available():
            base.parse_note = unavailable_reason()
            return base

        started = time.perf_counter()
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
            exit_code = process.returncode
        except TimeoutError:
            base.parse_note = f"official conformance run exceeded {timeout_s}s"
            base.duration_ms = (time.perf_counter() - started) * 1000
            return base
        except OSError as exc:
            base.parse_note = f"could not launch official conformance CLI: {exc}"
            return base

        base.duration_ms = (time.perf_counter() - started) * 1000
        base.exit_code = exit_code
        base.raw_stdout = stdout.decode("utf-8", errors="replace")[:MAX_RAW_CAPTURE]
        base.raw_stderr = stderr.decode("utf-8", errors="replace")[:MAX_RAW_CAPTURE]

        checks_files = find_checks_files(output_dir)
        if not checks_files:
            base.parse_note = (
                "official CLI produced no checks.json; treating as a harness failure "
                "rather than inferring a result from the exit code"
            )
            return base

        try:
            checks = load_all_checks(output_dir)
        except ConformanceParseError as exc:
            base.parse_note = f"could not parse official output: {exc}"
            return base

        total, passed, failed = summarise(checks)
        base.checks = checks
        base.scenarios_total = total
        base.scenarios_passed = passed
        base.scenarios_failed = failed
        base.outcome = (
            ConformanceOutcome.OFFICIAL_CONFORMANCE_FAILURE
            if failed
            else ConformanceOutcome.OFFICIAL_CONFORMANCE_PASS
            if passed
            else ConformanceOutcome.OFFICIAL_CONFORMANCE_SKIPPED
        )
        return base
