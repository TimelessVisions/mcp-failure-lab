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
    ConformanceCheck,
    ConformanceOutcome,
    ConformanceRun,
    ConformanceSelector,
)
from interop_lab.conformance.parser import (
    ConformanceParseError,
    count_non_verdicts,
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


def build_server_command(
    *,
    cli: Path,
    url: str,
    spec_version: str,
    output_dir: Path,
    scenario: str | None,
) -> tuple[list[str], ConformanceSelector]:
    """Build the official server argv for one dated question.

    An unscoped run passes ``--requirements``. That flag replaces
    ``--suite``, ``--spec-version`` and ``--scenario``: the pinned CLI exits
    before writing checks if they are combined. A named scenario cannot be a
    requirement-set run, so it is pinned with ``--spec-version`` instead and
    reported under :class:`ConformanceSelector.SCENARIO`.
    """
    if not spec_version:
        raise ValueError("spec_version is required")
    command = [
        "node",
        str(cli),
        "server",
        "--url",
        url,
        "--verbose",
        "--output-dir",
        str(output_dir),
    ]
    if scenario:
        command.extend(["--scenario", scenario, "--spec-version", spec_version])
        return command, ConformanceSelector.SCENARIO
    command.extend(["--requirements", spec_version])
    return command, ConformanceSelector.REQUIREMENTS


def official_outcome(
    *,
    selector: ConformanceSelector,
    exit_code: int | None,
    checks: list[ConformanceCheck],
) -> tuple[ConformanceOutcome, str]:
    """Map an official invocation to one outcome without re-scoring the set.

    For ``--requirements``, the pinned CLI's exit code is the scored verdict
    (``Bc`` in ``@modelcontextprotocol/conformance@0.2.0-alpha.11``): a
    not-scored scenario's FAILURE checks do not flip it, and this harness does
    not keep a second copy of that list. A non-zero exit is never a pass.

    For a single scenario there is no not-scored split. Check counts decide,
    so a zero exit cannot hide a failed check.
    """
    failed = any(check.passed is False for check in checks)
    passed = any(check.passed is True for check in checks)
    if selector is ConformanceSelector.REQUIREMENTS:
        if exit_code is None:
            return ConformanceOutcome.HARNESS_FAILURE, "harness"
        if exit_code != 0:
            return ConformanceOutcome.OFFICIAL_CONFORMANCE_FAILURE, "official_exit_code"
        if not passed and not failed:
            return ConformanceOutcome.OFFICIAL_CONFORMANCE_SKIPPED, "official_exit_code"
        return ConformanceOutcome.OFFICIAL_CONFORMANCE_PASS, "official_exit_code"
    if failed:
        return ConformanceOutcome.OFFICIAL_CONFORMANCE_FAILURE, "check_counts"
    if passed:
        return ConformanceOutcome.OFFICIAL_CONFORMANCE_PASS, "check_counts"
    return ConformanceOutcome.OFFICIAL_CONFORMANCE_SKIPPED, "check_counts"


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

        command, selector = build_server_command(
            cli=CLI_ENTRY,
            url=url,
            spec_version=profile.spec_version,
            output_dir=output_dir,
            scenario=scenario,
        )

        base = ConformanceRun(
            requirements=profile.spec_version,
            selector=selector,
            verdict_source="harness",
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
        skipped, informational = count_non_verdicts(checks)
        outcome, verdict_source = official_outcome(
            selector=selector,
            exit_code=exit_code,
            checks=checks,
        )
        base.checks = checks
        base.scenarios_total = total
        base.scenarios_passed = passed
        base.scenarios_failed = failed
        base.checks_skipped = skipped
        base.checks_informational = informational
        base.outcome = outcome
        base.verdict_source = verdict_source
        return base
