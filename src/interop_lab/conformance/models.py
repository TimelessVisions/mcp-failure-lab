"""Typed representation of official MCP conformance results.

These models describe *the official framework's* output. They are deliberately
kept apart from ``interop_lab.models.core`` so an official conformance failure
can never be silently reported as a Failure Lab finding, or the reverse.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class ConformanceSelector(StrEnum):
    """Which official question this invocation answered.

    ``requirements`` is ``--requirements <revision>``: the frozen set, at that
    revision's wire. ``scenario`` is one ``--scenario`` at ``--spec-version``.
    The official CLI rejects combining the two, and a scenario pass is not a
    requirement-set pass.
    """

    REQUIREMENTS = "requirements"
    SCENARIO = "scenario"


class ConformanceCheck(BaseModel):
    model_config = ConfigDict(extra="allow")

    check_id: str
    scenario: str | None = None
    passed: bool | None = None
    #: Official status token, preserved so SKIPPED is not counted as a pass.
    status: str | None = None
    message: str | None = None


class ConformanceRun(BaseModel):
    """One invocation of the official CLI, with provenance."""

    model_config = ConfigDict(extra="forbid")

    artifact_schema_version: str = ARTIFACT_SCHEMA_VERSION

    package: str = CONFORMANCE_PACKAGE
    version: str = CONFORMANCE_VERSION
    source_sha: str = CONFORMANCE_SOURCE_SHA
    repository: str = CONFORMANCE_REPOSITORY

    #: Dated revision this invocation was pinned to. Always set.
    #: A scenario probe stores the ``--spec-version`` wire pin here and does not
    #: emit a ``requirements`` field: that name is only the frozen set.
    revision: str = Field(description="Dated revision this invocation was pinned to.")
    #: Required. A default of ``requirements`` would label a forgotten scenario
    #: probe as a frozen requirement-set run.
    selector: ConformanceSelector
    #: Set only for ``selector=requirements``. Omitted from serialized scenario
    #: probes so the field name cannot be read as a requirement-set claim.
    requirements: str | None = None
    #: ``official_exit_code`` for a requirements run (the pinned CLI's scored
    #: verdict). ``check_counts`` for a single-scenario probe. ``harness`` when
    #: the official CLI did not produce a verdict.
    verdict_source: str = "harness"
    target_id: str
    url: str
    command: list[str]

    outcome: ConformanceOutcome
    exit_code: int | None = None
    duration_ms: float = 0.0

    scenarios_total: int = 0
    scenarios_passed: int = 0
    scenarios_failed: int = 0
    checks_skipped: int = 0
    checks_informational: int = 0
    checks: list[ConformanceCheck] = Field(default_factory=list)

    #: Official output, truncated, with absolute filesystem paths rewritten to
    #: ``<abs-path>``. URIs such as ``test://`` are kept. Verbatim machine paths
    #: are not portable evidence.
    raw_stdout: str = ""
    raw_stderr: str = ""
    parse_note: str | None = None

    @model_validator(mode="after")
    def _requirements_only_for_the_frozen_set(self) -> Self:
        if self.selector is ConformanceSelector.REQUIREMENTS:
            if not self.requirements:
                self.requirements = self.revision
        else:
            self.requirements = None
        return self

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        data = super().model_dump(**kwargs)
        if self.selector is not ConformanceSelector.REQUIREMENTS:
            data.pop("requirements", None)
        return data

    @property
    def failed(self) -> bool:
        return self.outcome is ConformanceOutcome.OFFICIAL_CONFORMANCE_FAILURE

    def provenance(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "package": self.package,
            "version": self.version,
            "source_sha": self.source_sha,
            "repository": self.repository,
            "revision": self.revision,
            "selector": self.selector.value,
            "verdict_source": self.verdict_source,
        }
        if self.selector is ConformanceSelector.REQUIREMENTS:
            payload["requirements"] = self.requirements
        return payload
