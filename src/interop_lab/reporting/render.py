"""Report rendering: terminal and JSON.

Reports must always carry the provenance that makes a result checkable — exact
target, exact version, profile, seed, evidence path and reproduction command.
JUnit, SARIF and HTML build on the same ``RunReport`` structure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from rich.console import Console
from rich.table import Table

from interop_lab.models.core import Classification, DifferentialResult, RunResult
from interop_lab.naming import ARTIFACT_SCHEMA_VERSION, CLI_COMMAND, HARNESS_VERSION, PRODUCT_NAME

_STYLES: dict[Classification, str] = {
    Classification.PASS: "green",
    Classification.NOT_APPLICABLE: "dim",
    Classification.EXPECTED_DIFFERENCE: "cyan",
    Classification.INCONCLUSIVE: "yellow",
    Classification.HARNESS_ERROR: "magenta",
}


def style_for(classification: Classification) -> str:
    return _STYLES.get(classification, "red")


class RunReport(BaseModel):
    """The serialisable root of every report format."""

    model_config = ConfigDict(extra="forbid")

    artifact_schema_version: str = ARTIFACT_SCHEMA_VERSION
    harness_version: str = HARNESS_VERSION
    product: str = PRODUCT_NAME
    generated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    suite: str
    runs: list[RunResult] = Field(default_factory=list)
    differentials: list[DifferentialResult] = Field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for run in self.runs:
            counts[run.classification.value] = counts.get(run.classification.value, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def failures(self) -> list[RunResult]:
        return [r for r in self.runs if r.failed]

    def summary(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "total_runs": len(self.runs),
            "counts": self.counts,
            "failures": len(self.failures),
            "differentials": len(self.differentials),
        }


def render_terminal(report: RunReport, console: Console | None = None) -> None:
    console = console or Console()

    table = Table(title=f"{PRODUCT_NAME} — suite '{report.suite}'", header_style="bold")
    table.add_column("Scenario", overflow="fold")
    table.add_column("Target", overflow="fold")
    table.add_column("Profile")
    table.add_column("Result")
    table.add_column("ms", justify="right")

    for run in report.runs:
        table.add_row(
            run.scenario_id,
            run.target_id,
            run.spec_version,
            f"[{style_for(run.classification)}]{run.classification.value}[/]",
            f"{run.duration_ms:.0f}",
        )
    console.print(table)

    for run in report.failures:
        console.print(
            f"\n[bold red]{run.classification.value}[/] {run.scenario_id} on {run.target_id}"
        )
        for assertion in run.assertions:
            if assertion.status.value != "fail":
                continue
            console.print(f"  expected: {assertion.expected}")
            console.print(f"  observed: {assertion.actual}")
            if assertion.normative_source:
                console.print(f"  basis:    {assertion.normative_source}")
        if run.timeout_cause:
            console.print(f"  timeout:  {run.timeout_cause.value}")
        if run.trace_path:
            console.print(f"  evidence: {run.trace_path}")

    if report.differentials:
        diff_table = Table(title="Differential comparison", header_style="bold")
        diff_table.add_column("Scenario", overflow="fold")
        diff_table.add_column("Left")
        diff_table.add_column("Right")
        diff_table.add_column("Verdict")
        diff_table.add_column("First diff", justify="right")
        for diff in report.differentials:
            first = (
                str(diff.first_normalized_difference_seq)
                if diff.first_normalized_difference_seq is not None
                else "-"
            )
            diff_table.add_row(
                diff.scenario_id,
                f"{diff.left_target} ({diff.left_classification.value})",
                f"{diff.right_target} ({diff.right_classification.value})",
                diff.verdict.value,
                first,
            )
        console.print(diff_table)

        for diff in report.differentials:
            if diff.verdict.value in ("equivalent", "incomparable"):
                continue
            console.print(f"\n[bold]{diff.scenario_id}[/]: {diff.normative_interpretation}")
            for difference in diff.meaningful_differences[:5]:
                console.print(
                    f"  seq {difference.seq} {difference.field_path}: "
                    f"{difference.left!r} vs {difference.right!r} — {difference.explanation}"
                )

    counts = report.counts
    console.print(f"\n[bold]Summary[/]: {counts}")
    if report.failures:
        console.print(
            f"[dim]Reproduce a failure with: {CLI_COMMAND} replay "
            f"artifacts/repros/<finding-id>/repro.yaml[/]"
        )
