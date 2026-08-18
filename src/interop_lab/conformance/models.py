"""Typed representation of official MCP conformance results.

These models describe *the official framework's* output. They are deliberately
kept apart from ``interop_lab.models.core`` so an official conformance failure
can never be silently reported as a Failure Lab finding, or the reverse.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from interop_lab.naming import ARTIFACT_SCHEMA_VERSION

#: Pinned by tools/conformance/package.json. Recorded in every artifact.
CONFORMANCE_PACKAGE = "@modelcontextprotocol/conformance"
CONFORMANCE_VERSION = "0.2.0-alpha.11"
CONFORMANCE_SOURCE_SHA = "c321dd32035556e6769d3724a8ee97d87c3faaac"
CONFORMANCE_REPOSITORY = "modelcontextprotocol/conformance"


class ConformanceOutcome(StrEnum):
    """Deliberately separate from ``Classification``.

    An official conformance failure is a statement by the official framework. An
    Failure Lab resilience finding is a statement by this harness. Collapsing
    them into one vocabulary would make it impossible to say which tool observed
    what — and would let this project take credit for the official suite's work.
    """

    OFFICIAL_CONFORMANCE_PASS = "OFFICIAL_CONFORMANCE_PASS"  # noqa: S105 - an outcome name
    OFFICIAL_CONFORMANCE_FAILURE = "OFFICIAL_CONFORMANCE_FAILURE"
    OFFICIAL_CONFORMANCE_SKIPPED = "OFFICIAL_CONFORMANCE_SKIPPED"
    #: The official framework could not be run at all.
    HARNESS_FAILURE = "HARNESS_FAILURE"


class ConformanceCheck(BaseModel):
    model_config = ConfigDict(extra="allow")

    check_id: str
    scenario: str | None = None
    passed: bool | None = None
    message: str | None = None


class ConformanceRun(BaseModel):
    """One invocation of the official CLI, with provenance."""

    model_config = ConfigDict(extra="forbid")

    artifact_schema_version: str = ARTIFACT_SCHEMA_VERSION

    package: str = CONFORMANCE_PACKAGE
    version: str = CONFORMANCE_VERSION
    source_sha: str = CONFORMANCE_SOURCE_SHA
    repository: str = CONFORMANCE_REPOSITORY

    requirements: str = Field(description="Spec revision passed via --requirements.")
    target_id: str
    url: str
    command: list[str]

    outcome: ConformanceOutcome
    exit_code: int | None = None
    duration_ms: float = 0.0

    scenarios_total: int = 0
    scenarios_passed: int = 0
    scenarios_failed: int = 0
    checks: list[ConformanceCheck] = Field(default_factory=list)

    #: Raw official output, preserved verbatim. Never edited, only truncated.
    raw_stdout: str = ""
    raw_stderr: str = ""
    parse_note: str | None = None

    @property
    def failed(self) -> bool:
        return self.outcome is ConformanceOutcome.OFFICIAL_CONFORMANCE_FAILURE

    def provenance(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "version": self.version,
            "source_sha": self.source_sha,
            "repository": self.repository,
            "requirements": self.requirements,
        }
