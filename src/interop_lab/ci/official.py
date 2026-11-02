"""Feed official conformance results into the regression gate.

``mcp-failure-lab ci`` used to call ``evaluate`` with no conformance outcomes.
A baseline that recorded an official outcome then either passed without a
remeasurement or, after the unmeasured check, failed without ever invoking
the official CLI. This module runs that CLI for each HTTP target the baseline
marks as required and returns the official outcome strings.
"""

from __future__ import annotations

from pathlib import Path

from interop_lab.ci.baseline import BaselineDocument
from interop_lab.conformance.adapter import ConformanceAdapter, is_available
from interop_lab.models.core import RunResult, TargetDefinition
from interop_lab.profiles.registry import Transport
from interop_lab.scenarios.model import Scenario
from interop_lab.transports.http.session import StreamableHttpSession


async def official_outcomes_for_baseline(
    baseline: BaselineDocument,
    runs: list[tuple[RunResult, Scenario, TargetDefinition]],
    *,
    artifacts: Path,
) -> dict[str, str]:
    """Map target id to the official outcome value, for required entries only.

    Targets that are not Streamable HTTP, and a missing pinned CLI, are omitted.
    The gate then reports ``NOT_MEASURED`` rather than a pass.
    """
    required = {entry.target_id for entry in baseline.entries if entry.official_conformance_outcome}
    if not required or not is_available():
        return {}

    outcomes: dict[str, str] = {}
    for _run, _scenario, target in runs:
        if target.target_id not in required or target.target_id in outcomes:
            continue
        if target.transport is not Transport.STREAMABLE_HTTP:
            continue
        session = StreamableHttpSession(target, run_id="ci-conformance")
        async with session:
            if session.base_url is None:
                continue
            result = await ConformanceAdapter(output_root=artifacts).run_server(
                target=target,
                url=session.base_url,
            )
        outcomes[target.target_id] = result.outcome.value
    return outcomes
