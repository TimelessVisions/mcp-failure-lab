"""Baseline documents and the regression gate.

A baseline is an *approved* record of what a matrix currently does, including
failures that are known and accepted. The gate compares a fresh run against it.

Three rules carry the design:

* A baseline is never updated automatically. Absorbing a new failure because it
  happened twice is how a regression gate stops detecting regressions.
* A missing result is never a pass. Neither is ``NOT_APPLICABLE``.
* Fingerprints are compared. If the target version or the scenario definition
  changed, the baseline no longer describes the thing being measured, and that
  needs approval rather than silent acceptance.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from interop_lab.capabilities.model import CapabilityFingerprint
from interop_lab.containment.imagesource import ImageSourceKind
from interop_lab.containment.resolution import is_local_image_id
from interop_lab.models.core import (
    FAILING,
    Applicability,
    Classification,
    ResultDimensions,
    RunResult,
    TargetDefinition,
)
from interop_lab.naming import ARTIFACT_SCHEMA_VERSION, HARNESS_VERSION
from interop_lab.scenarios.model import Scenario

#: Layout of a baseline document.
#:
#: v4 records each entry's approved *dimensions* beside its classification, and
#: widens ``capability_required`` to cover safe invocations and observed transport
#: features. Both changes are the kind that must be refused by name rather than
#: absorbed: a v3 document loads perfectly well under this code, and would then
#: claim that every entry was approved with no security dimension and that a
#: session-lifecycle entry needed no observation — two statements nobody made.
#: The gate would enforce a weaker rule while looking exactly as approved.
#:
#: v3 binds each entry to the capability surface that justified it. The bump is
#: deliberate rather than a compatible field addition: a v2 document loads
#: perfectly well under this code and simply records no surface, so every
#: capability comparison would be skipped and the gate would enforce nothing
#: while looking exactly as approved as before. Refusing it by name is the
#: difference between a binding that exists and one that is merely available.
#:
#: v2 accompanied target fingerprint v3: the fingerprints a v1 baseline holds
#: were computed under a scheme that no longer exists, so every entry would
#: report drift rather than being compared.
BASELINE_SCHEMA_VERSION = "4"


class GateOutcome(StrEnum):
    PASSED = "passed"
    REGRESSION = "regression"
    CONFIGURATION_ERROR = "configuration_error"
    HARNESS_FAILURE = "harness_failure"


#: Documented exit codes. Anything else is a bug in the gate.
EXIT_PASSED = 0
EXIT_REGRESSION = 1
EXIT_CONFIGURATION_ERROR = 2
EXIT_HARNESS_FAILURE = 3

EXIT_CODES: dict[GateOutcome, int] = {
    GateOutcome.PASSED: EXIT_PASSED,
    GateOutcome.REGRESSION: EXIT_REGRESSION,
    GateOutcome.CONFIGURATION_ERROR: EXIT_CONFIGURATION_ERROR,
    GateOutcome.HARNESS_FAILURE: EXIT_HARNESS_FAILURE,
}


class DivergencePolicy(StrEnum):
    FAIL = "fail"
    WARN = "warn"
    ALLOW_IF_APPROVED = "allow_if_approved"


class DriftPolicy(StrEnum):
    FAIL = "fail"
    WARN = "warn"


class InconclusivePolicy(StrEnum):
    """What an undecided result does to the gate.

    Blocks by default. An INCONCLUSIVE is the harness saying it could not reach a
    verdict, and the entry it replaces was an approved statement about the
    target — so letting it through silently converts approved coverage into no
    coverage while the gate stays green.

    ``WARN`` exists for a matrix where a target is expected to be intermittently
    unreachable and the operator has decided that is acceptable. It is a
    deliberate choice with a name, not a default anyone drifts into.
    """

    FAIL = "fail"
    WARN = "warn"


class UnknownResultPolicy(StrEnum):
    """What to do with a result the baseline has never seen.

    Defaults to FAIL. A new PASS is encouraging, but it still changes the
    approved measurement surface: a scenario nobody approved is now contributing
    to the verdict. Letting it through silently means the gate's coverage can
    grow without anyone agreeing to it.
    """

    FAIL = "fail"
    WARN = "warn"


#: Fingerprint schema version. v1 covered only the program; v2 added how it is
#: executed; v3 replaces the collapsed image pin with an explicit image source,
#: so a locally built target is identified by what it is built *from* rather
#: than by an image ID only one machine could ever produce.
TARGET_FINGERPRINT_VERSION = 3


def target_fingerprint(target: TargetDefinition) -> str:
    """Identity of the thing being measured, not of the file describing it.

    **Version 3** carries the image *source*: a registry repository digest, or a
    local build's portable inputs — build key, committed context, Dockerfile,
    base images, toolchain pins. It deliberately does **not** carry the current
    runner's local image ID, its storage driver, the build timestamp, the
    machine, or the path of a resolution manifest. Those describe one execution
    of a build, not the target, and putting them here would mean the same
    approved target fingerprinted differently on every machine — so no baseline
    could ever be shared, and every CI run would report drift that is not drift.

    They are not discarded: they are execution evidence, recorded on the run's
    containment record where a reader can see which builder produced them.

    v2 hashed ``container`` wholesale, which included the pinned image digest.
    That field held a registry digest for a pulled image and a builder-local ID
    for a built one, with nothing recording which — so a fingerprint computed
    from it meant two different things depending on the target, and a rebuild
    on another machine drifted it for no reason anyone could act on.

    v1 hashed the program alone. That was wrong in a way the containment work
    made obvious: the same server, run on the host and run in a container with
    256 MB and no network, can legitimately produce different results — a
    timeout, a crash, a refusal to reach something. A baseline recorded under one
    and compared against the other looked comparable and was not.
    """
    material = json.dumps(
        {
            "fingerprint_version": TARGET_FINGERPRINT_VERSION,
            "target_id": target.target_id,
            "implementation": target.implementation,
            "version": target.version,
            "commit_sha": target.commit_sha,
            "transport": target.transport.value,
            "protocol_profile": target.protocol_profile,
            "launch_command": target.launch_command,
            "env": target.env,
            # `provenance` is included because it decides whether host execution
            # is permitted at all.
            "provenance": target.provenance.value,
            "execution_mode": target.execution_mode.value,
            "containment": _containment_material(target),
            # The approved safe invocation decides what a tool-call scenario
            # actually sends, so changing the tool or its arguments changes what
            # was measured — as surely as changing the launch command would.
            "safe_tool_invocation": (
                target.safe_tool_invocation.model_dump(mode="json")
                if target.safe_tool_invocation is not None
                else None
            ),
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _containment_material(target: TargetDefinition) -> dict[str, Any] | None:
    """The containment half of the identity, with the image source made explicit.

    The container spec is dumped whole — every limit is part of the measurement
    — and that dump already contains the image source, which for a local build
    is entirely portable by construction. This function exists to assert that
    rather than to trust it: if a builder-local identifier ever reappears in a
    container spec, the check below fails loudly here instead of silently
    poisoning every fingerprint downstream.
    """
    spec = target.container
    if spec is None:
        return None

    dumped: dict[str, Any] = spec.model_dump(mode="json")
    source = dumped.get("image_source")
    if isinstance(source, dict) and source.get("kind") == ImageSourceKind.LOCAL_BUILD.value:
        for key, value in source.items():
            if key == "base_image_digests":
                # Registry digests of images the build pulls. Portable: any
                # machine can fetch exactly these bits.
                continue
            if isinstance(value, str) and is_local_image_id(value):
                raise ValueError(
                    f"target {target.target_id!r} carries what looks like a builder-local "
                    f"image ID in image_source.{key}. That value differs between storage "
                    "drivers, so including it would make this target fingerprint "
                    "differently on every machine and no baseline could be shared."
                )
    return dumped


def scenario_fingerprint(scenario: Scenario) -> str:
    """Identity of the scenario's executable content.

    Covers the steps, faults and assertions — the things that decide what a
    result means. A prose edit to the title does not invalidate a baseline.
    """
    payload = scenario.model_dump(mode="json", exclude_none=True)
    material = json.dumps(
        {
            "scenario_id": scenario.scenario_id,
            "version": scenario.definition.version,
            "steps": payload.get("steps"),
            "faults": payload.get("faults"),
            "applicability": payload["definition"].get("applicability"),
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


class GatePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fail_on_new_normative_failure: bool = True
    fail_on_crash: bool = True
    fail_on_hang: bool = True
    fail_on_harness_error: bool = True
    fail_on_cleanup_failure: bool = True
    fail_on_missing_target: bool = True
    fail_on_missing_scenario: bool = True
    fail_on_target_fingerprint_drift: bool = True
    fail_on_scenario_fingerprint_drift: bool = True
    fail_on_reproduction_instability: bool = True
    new_behavioral_divergence: DivergencePolicy = DivergencePolicy.WARN
    runtime_version_drift: DriftPolicy = DriftPolicy.WARN
    #: What a changed capability surface does to the gate. Blocks by default.
    #:
    #: Not because a capability change is a defect — it usually is not, and the
    #: gate never says it is. It blocks because the approved result was decided
    #: against a surface that has moved, so nobody yet knows what the result
    #: means. `WARN` is available for a matrix where the surface is expected to
    #: drift; the drift is still reported either way, because a warning that is
    #: not printed is not a warning.
    capability_drift: DriftPolicy = DriftPolicy.FAIL
    #: Any target x scenario absent from the baseline, whatever its result.
    unknown_result: UnknownResultPolicy = UnknownResultPolicy.FAIL
    #: Compare official conformance outcomes where an entry declares one.
    fail_on_conformance_outcome_change: bool = True
    #: A security-relevant result appearing where the baseline approved none.
    #:
    #: Blocks by default, and deliberately independent of the primary
    #: classification. The case this exists for is the one the old design could
    #: not see: a result whose classification is unchanged, or is merely a
    #: BEHAVIORAL_DIVERGENCE that policy allows, while having become
    #: security-relevant. Under classification-only comparison that passes
    #: silently. A security dimension appearing for the first time is something a
    #: person should look at, whatever the outcome next to it says.
    fail_on_new_security_relevant_result: bool = True
    #: What an undecided result does. Blocks by default; see
    #: :class:`InconclusivePolicy`.
    inconclusive_result: InconclusivePolicy = InconclusivePolicy.FAIL


class ReproductionExpectation(BaseModel):
    """What a reproduction package must still demonstrate.

    Declared only for entries where a reproduction is the point: approved
    synthetic failures, confirmed external findings, and any failure used as a
    release-gate invariant. A passing entry does not need one.
    """

    model_config = ConfigDict(extra="forbid")

    required: bool = True
    package_uri: str
    expected_classification: Classification
    expected_failed_assertions: list[str] = Field(default_factory=list)
    minimum_stability_runs: int = 3
    expected_target_fingerprint: str | None = None
    expected_scenario_fingerprint: str | None = None
    #: Actually re-run the package. Off by default: it multiplies gate runtime.
    verify_replay: bool = False


class BaselineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: str
    target_fingerprint: str
    scenario_id: str
    scenario_version: str
    scenario_fingerprint: str

    #: The capability surface that justified this entry's applicability, as it
    #: was when the entry was approved.
    #:
    #: The target and scenario fingerprints together do not cover it. Both are
    #: computed from files in this repository, and neither moves when the *target*
    #: changes what it advertises — so an upstream release that added, removed or
    #: re-specified a tool left an approved entry looking untouched, while the
    #: decision the entry records was made against a surface that no longer
    #: exists.
    capability_fingerprint: str | None = None
    #: The approved surface itself, kept beside its digest so drift can be
    #: described rather than merely detected. Two hex strings that differ tell a
    #: reviewer that something changed and give them nothing to decide with.
    approved_capability_surface: CapabilityFingerprint | None = None
    #: Whether this entry's applicability rested on observing the target, rather
    #: than on facts a target file states. When it did, a run that could not
    #: resolve capabilities has not reproduced the approved conditions and cannot
    #: be compared against this entry at all.
    capability_required: bool = False

    applicable: bool
    allowed_classification: Classification
    #: The secondary dimensions this entry was approved with.
    #:
    #: Recorded because ``allowed_classification`` cannot answer the question the
    #: security gate asks. A run can keep its approved classification and acquire
    #: a security dimension — a different assertion inside the same scenario
    #: begins failing, or a scenario's security declaration is widened — and a
    #: comparison that looked only at the classification would report nothing.
    allowed_dimensions: ResultDimensions = Field(default_factory=ResultDimensions)
    expected_failed_assertions: list[str] = Field(default_factory=list)
    approved_differential_verdict: str | None = None
    official_conformance_outcome: str | None = None
    evidence: str | None = None
    approval_reason: str | None = None
    reproduction: ReproductionExpectation | None = None

    @property
    def is_approved_failure(self) -> bool:
        return self.allowed_classification in FAILING

    @property
    def key(self) -> tuple[str, str]:
        return (self.target_id, self.scenario_id)


class BaselineDocument(BaseModel):
    """An approved snapshot. Extra fields are rejected, never reinterpreted."""

    model_config = ConfigDict(extra="forbid")

    artifact_schema_version: str = ARTIFACT_SCHEMA_VERSION
    baseline_schema_version: str = BASELINE_SCHEMA_VERSION
    harness_version: str = HARNESS_VERSION

    baseline_id: str
    created_at: str
    created_reason: str
    created_by: str

    #: Which fingerprint scheme the recorded fingerprints were produced with.
    #: A baseline written under v1 cannot be compared against v2 fingerprints,
    #: and saying so explicitly beats every entry drifting with no explanation.
    target_fingerprint_version: int = TARGET_FINGERPRINT_VERSION

    protocol_profiles: list[str] = Field(default_factory=list)
    suite: str
    suite_fingerprint: str
    target_fingerprints: dict[str, str] = Field(default_factory=dict)
    policy: GatePolicy = Field(default_factory=GatePolicy)
    entries: list[BaselineEntry] = Field(default_factory=list)

    def lookup(self, target_id: str, scenario_id: str) -> BaselineEntry | None:
        for entry in self.entries:
            if entry.target_id == target_id and entry.scenario_id == scenario_id:
                return entry
        return None


class BaselineSchemaError(Exception):
    """A baseline this harness will not reinterpret."""


def load_baseline(path: Path) -> BaselineDocument:
    """Read a baseline, refusing an older schema by name rather than by symptom.

    A baseline written under an earlier scheme is not upgraded in place, and it
    is deliberately not *repaired*. What a baseline records is human approval:
    which failures are known, why each one was accepted, and which reproduction
    is expected to still demonstrate it. Rewriting the fingerprints under a new
    scheme would carry that approval forward to measurements nobody approved,
    which is precisely the property a regression gate exists to prevent.

    So the old document is left on disk, untouched, and a new one is created
    deliberately with ``baseline create`` — preserving the approval reasons the
    old one carries, which is a decision for a person and not for this function.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineSchemaError(f"{path}: unreadable baseline: {exc}") from exc
    if not isinstance(payload, dict):
        raise BaselineSchemaError(f"{path}: expected a JSON object")

    schema = payload.get("baseline_schema_version")
    fingerprints = payload.get("target_fingerprint_version")
    if schema != BASELINE_SCHEMA_VERSION or fingerprints != TARGET_FINGERPRINT_VERSION:
        raise BaselineSchemaError(
            f"{path} is baseline schema {schema!r} with target fingerprints "
            f"v{fingerprints}; this harness reads schema "
            f"{BASELINE_SCHEMA_VERSION!r} with fingerprints v{TARGET_FINGERPRINT_VERSION}. "
            "An older baseline either holds fingerprints computed under a scheme that "
            "no longer exists — so every entry would report drift instead of being "
            "compared — or predates the capability binding or the result dimensions, "
            "in which case it would load cleanly and silently enforce nothing about "
            "the surface each entry was approved against, or record every entry as "
            "having been approved with no security dimension. "
            "Nothing is migrated automatically: a baseline is an approval, and "
            "re-approving it is a decision for a person. Re-create it deliberately "
            "with `mcp-failure-lab baseline create --reason ...`, carrying the approval "
            "reasons and reproduction expectations the existing file records."
        )

    try:
        return BaselineDocument.model_validate(payload)
    except Exception as exc:
        raise BaselineSchemaError(f"{path}: not a valid baseline: {exc}") from exc


class RegressionKind(StrEnum):
    NEW_FAILURE = "new_failure"
    CLASSIFICATION_CHANGED = "classification_changed"
    HARNESS_ERROR = "harness_error"
    MISSING_TARGET = "missing_target"
    MISSING_SCENARIO = "missing_scenario"
    SILENTLY_SKIPPED = "silently_skipped"
    TARGET_DRIFT = "target_fingerprint_drift"
    SCENARIO_DRIFT = "scenario_fingerprint_drift"
    #: The target advertises a different surface than the one this entry was
    #: approved against. Deliberately its own kind rather than folded into
    #: TARGET_DRIFT: that one means a file in this repository changed, which is
    #: something the author did, and this one means someone else's server did.
    CAPABILITY_DRIFT = "capability_fingerprint_drift"
    #: The entry's applicability rested on observing the target, and this run did
    #: not manage to observe it. Never NOT_APPLICABLE — that would state a reason
    #: ("the target lacks the tool") nobody established.
    CAPABILITY_UNRESOLVED = "capability_unresolved"
    SIGNATURE_CHANGED = "failure_signature_changed"
    #: A security dimension appeared where the baseline approved none. Its own
    #: kind because it is orthogonal to the classification: the outcome beside it
    #: may be entirely unchanged, and that is exactly the case worth surfacing.
    NEW_SECURITY_RELEVANT = "new_security_relevant_result"
    #: An approved result became undecidable. Never folded into
    #: CLASSIFICATION_CHANGED: that reads as the target behaving differently, and
    #: this means the harness could not tell whether it did.
    INCONCLUSIVE_RESULT = "inconclusive_result"
    NEW_DIVERGENCE = "new_behavioral_divergence"
    UNKNOWN_RESULT = "result_not_in_baseline"
    REPRODUCTION_MISSING = "reproduction_missing"
    REPRODUCTION_UNSTABLE = "reproduction_unstable"
    REPRODUCTION_MISMATCH = "reproduction_mismatch"
    CONFORMANCE_OUTCOME_CHANGED = "official_conformance_outcome_changed"


class Regression(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: RegressionKind
    target_id: str
    scenario_id: str
    baseline_value: str | None = None
    observed_value: str | None = None
    detail: str
    evidence: str | None = None
    reproduction: str | None = None
    normative_source: str | None = None
    #: Whether this alone fails the gate, or is reported as a warning.
    blocking: bool = True


class Improvement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: str
    scenario_id: str
    baseline_value: str
    observed_value: str
    detail: str


class GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_schema_version: str = ARTIFACT_SCHEMA_VERSION
    harness_version: str = HARNESS_VERSION
    generated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    baseline_id: str
    suite: str
    outcome: GateOutcome
    exit_code: int

    regressions: list[Regression] = Field(default_factory=list)
    improvements: list[Improvement] = Field(default_factory=list)
    unchanged: int = 0
    environment_mismatches: list[str] = Field(default_factory=list)
    harness_failures: list[str] = Field(default_factory=list)
    artifact_paths: list[str] = Field(default_factory=list)

    @property
    def blocking_regressions(self) -> list[Regression]:
        return [r for r in self.regressions if r.blocking]

    def summary(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "exit_code": self.exit_code,
            "regressions": len(self.blocking_regressions),
            "warnings": len(self.regressions) - len(self.blocking_regressions),
            "improvements": len(self.improvements),
            "unchanged": self.unchanged,
        }


def requires_observed_surface(applicability: Applicability) -> bool:
    """Whether this applicability gate can only be answered by observing the target.

    Three declarations put a scenario in this class, and the previous version of
    this rule recognised one of them:

    * ``requires_tools`` — answered from what the target advertised in
      ``tools/list``.
    * ``requires_safe_tool_invocation`` — answered from the advertised surface
      *and* the approved contract's schema hash. A run that never observed the
      target cannot tell "the tool is gone" from "nobody looked", and the
      difference between those is the difference between NOT_APPLICABLE and
      INCONCLUSIVE.
    * ``requires_transport_features`` — answered from a handshake. Whether a
      server issues a session identifier is not in any file; it is on a response
      header, and 2025-11-25 permits either answer.

    Missing the last two meant a safe-tool-call baseline and a session-lifecycle
    baseline both recorded ``capability_required: false`` — so the gate skipped
    the capability comparison entirely for them, and a run that failed discovery
    could be compared against an entry whose approved conditions it had not
    reproduced.

    Everything else — profile, dated version, transport, role — is decided from
    the target file and the profile registry, which a run always has, and needs
    no traffic with the target at all.
    """
    return bool(
        applicability.requires_tools
        or applicability.requires_safe_tool_invocation
        or applicability.requires_transport_features
    )


def capability_dependent(scenario: Scenario) -> bool:
    """Whether this scenario's applicability was decided by observing the target."""
    return requires_observed_surface(scenario.definition.applicability)


def suite_fingerprint(scenarios: list[Scenario]) -> str:
    material = json.dumps(sorted(scenario_fingerprint(s) for s in scenarios), sort_keys=True)
    return hashlib.sha256(material.encode()).hexdigest()[:16]


class UnapprovedFailureError(Exception):
    """A baseline was asked to record a failure nobody approved."""


def approval_key(target_id: str, scenario_id: str) -> str:
    return f"{target_id}:{scenario_id}"


def build_baseline(
    *,
    baseline_id: str,
    reason: str,
    created_by: str,
    suite: str,
    scenarios: list[Scenario],
    targets: list[TargetDefinition],
    runs: list[tuple[RunResult, Scenario, TargetDefinition]],
    policy: GatePolicy | None = None,
    approved_failures: dict[str, str] | None = None,
    reproductions: dict[str, ReproductionExpectation] | None = None,
) -> BaselineDocument:
    """Record what the matrix currently does, as an approved snapshot.

    A failing result cannot enter a baseline on the strength of one generic
    reason covering the whole document. Each failure must be named explicitly
    with its own justification, because "the build was red when I made the
    baseline" is exactly what a regression gate exists to prevent.
    """
    approvals = approved_failures or {}
    expectations = reproductions or {}

    unapproved = sorted(
        approval_key(run.target_id, run.scenario_id)
        for run, _, _ in runs
        if run.classification in FAILING
        and approval_key(run.target_id, run.scenario_id) not in approvals
    )
    if unapproved:
        raise UnapprovedFailureError(
            "refusing to record failing results without per-entry approval:\n  "
            + "\n  ".join(unapproved)
            + "\n\nApprove each explicitly, for example:\n  --approve-failure "
            + f"{unapproved[0]} --approval-reason '<why this failure is expected>'"
        )

    entries: list[BaselineEntry] = []
    for run, scenario, target in runs:
        failed = [a.assertion_id for a in run.assertions if a.status.value == "fail"]
        key = approval_key(run.target_id, run.scenario_id)
        is_failure = run.classification in FAILING
        # The surface the decision was made against, recorded whenever the run
        # observed one. Recording it costs nothing and is what lets a later gate
        # run see that the target changed underneath an approval.
        capabilities = run.resolved_capabilities
        surface = (
            capabilities.fingerprint()
            if capabilities is not None and capabilities.is_usable
            else None
        )
        entries.append(
            BaselineEntry(
                target_id=run.target_id,
                target_fingerprint=target_fingerprint(target),
                scenario_id=run.scenario_id,
                scenario_version=run.scenario_version,
                scenario_fingerprint=scenario_fingerprint(scenario),
                capability_fingerprint=surface.digest if surface is not None else None,
                approved_capability_surface=surface,
                capability_required=capability_dependent(scenario),
                applicable=run.classification is not Classification.NOT_APPLICABLE,
                allowed_classification=run.classification,
                allowed_dimensions=run.dimensions,
                expected_failed_assertions=failed,
                evidence=run.trace_path,
                # A failure carries its own justification; everything else
                # inherits the document-level reason.
                approval_reason=approvals[key] if is_failure else reason,
                reproduction=expectations.get(key),
            )
        )

    return BaselineDocument(
        baseline_id=baseline_id,
        created_at=datetime.now(UTC).isoformat(),
        created_reason=reason,
        created_by=created_by,
        protocol_profiles=sorted({t.protocol_profile for t in targets}),
        suite=suite,
        suite_fingerprint=suite_fingerprint(scenarios),
        target_fingerprints={t.target_id: target_fingerprint(t) for t in targets},
        policy=policy or GatePolicy(),
        entries=entries,
    )
