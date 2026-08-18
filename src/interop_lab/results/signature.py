"""What has to be identical before two runs may be called the same result.

The external matrix compared *classifications* across repetitions and called a
repeated value stable. That is too weak in a specific and checkable way: two runs
can both report ``SPEC_VIOLATION`` while different assertions failed, or while
one was killed by the harness and the other exited on its own, or against two
different capability surfaces. Every one of those is a different result wearing
the same label, and "stable across repetitions" said nothing about any of them.

So stability is defined over a typed signature instead, and the signature carries
the things that decide what a result *means*:

* the primary classification and the failing assertion identities;
* each assertion's own classification, because two runs can fail the same
  assertion for different reasons;
* the timeout cause, the exit code, the signal, and whether the harness did the
  killing — a harness kill and a target crash are not the same evidence;
* cleanup and containment outcomes, because a run without usable containment is
  not evidence about the target at all;
* the target, scenario and capability fingerprints, so a signature cannot be
  compared across a changed target, a changed scenario, or a moved surface;
* the first meaningful differential field, where the result came from a
  comparison.

Deliberately **not** carried: wall-clock durations, timestamps, run identifiers,
raw wire bytes, or anything else that legitimately differs between two correct
executions of the same thing. A signature that moved on those would report
instability on every run and mean nothing.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from interop_lab.models.core import (
    AssertionStatus,
    Classification,
    ResultDimensions,
    RunResult,
    TimeoutCause,
)

#: Version of the signature scheme.
#:
#: Carried inside the digest material. Two signatures computed under different
#: schemes are not comparable, and a scheme change must therefore read as "cannot
#: be compared" rather than as instability in someone's server.
RESULT_SIGNATURE_VERSION = 1


def canonical_digest(material: Any) -> str:
    """Hash a JSON value in a form two machines agree on."""
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class AssertionSignature(BaseModel):
    """One assertion's contribution to the signature.

    The status *and* the classification. A run where ``code_32020`` failed as a
    SPEC_VIOLATION and a run where the same assertion failed as an
    INVALID_WIRE_MESSAGE are not the same finding, and the assertion id alone
    cannot tell them apart.
    """

    model_config = ConfigDict(extra="forbid")

    assertion_id: str
    status: AssertionStatus
    classification: Classification | None = None
    security_relevant: bool = False


class ResultSignature(BaseModel):
    """Everything that must match before two runs are the same result."""

    model_config = ConfigDict(extra="forbid")

    signature_version: int = RESULT_SIGNATURE_VERSION

    scenario_id: str
    classification: Classification
    #: Sorted, so a server that evaluates assertions in a different order does not
    #: read as a different result.
    failed_assertion_ids: list[str] = Field(default_factory=list)
    #: Every assertion, sorted by id — not only the failing ones. An assertion
    #: that stopped being evaluated at all is a change in what was measured.
    assertions: list[AssertionSignature] = Field(default_factory=list)

    timeout_cause: TimeoutCause | None = None
    exit_code: int | None = None
    signal: int | None = None
    killed_by_harness: bool = False
    cleanup_ok: bool = True
    #: ``None`` when the run requested no containment, which is a different fact
    #: from containment having been requested and not obtained.
    containment_outcome: str | None = None
    containment_usable: bool = True

    target_fingerprint: str | None = None
    scenario_fingerprint: str | None = None
    capability_fingerprint: str | None = None

    #: The secondary dimensions, so a result that became security-relevant is
    #: unstable even when its classification held still.
    dimensions: ResultDimensions = Field(default_factory=ResultDimensions)

    #: Where a differential comparison first diverged meaningfully, when this
    #: result came from one. The *field path*, never the values: two runs of a
    #: correct server legitimately differ in a timestamp's contents while
    #: diverging at the same place, and a signature over the values would report
    #: instability on every repetition.
    first_meaningful_difference: str | None = None

    def digest_material(self) -> dict[str, Any]:
        """Exactly what the digest covers, in a canonical order."""
        return {
            "signature_version": self.signature_version,
            "scenario_id": self.scenario_id,
            "classification": self.classification.value,
            "failed_assertion_ids": sorted(self.failed_assertion_ids),
            "assertions": [
                {
                    "assertion_id": a.assertion_id,
                    "status": a.status.value,
                    "classification": (
                        a.classification.value if a.classification is not None else None
                    ),
                    "security_relevant": a.security_relevant,
                }
                for a in sorted(self.assertions, key=lambda a: a.assertion_id)
            ],
            "timeout_cause": self.timeout_cause.value if self.timeout_cause else None,
            "exit_code": self.exit_code,
            "signal": self.signal,
            "killed_by_harness": self.killed_by_harness,
            "cleanup_ok": self.cleanup_ok,
            "containment_outcome": self.containment_outcome,
            "containment_usable": self.containment_usable,
            "target_fingerprint": self.target_fingerprint,
            "scenario_fingerprint": self.scenario_fingerprint,
            "capability_fingerprint": self.capability_fingerprint,
            "dimensions": self.dimensions.digest_material,
            "first_meaningful_difference": self.first_meaningful_difference,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.digest_material())

    def describe_difference(self, other: ResultSignature) -> list[str]:
        """Name what moved between two signatures, in reviewable terms.

        Two digests that differ is a true statement and a useless one. The whole
        value of a signature over a bare classification is that it can say *which*
        of the things that decide a result's meaning changed, so this returns that
        rather than leaving a reader to diff two hex strings.
        """
        mine, theirs = self.digest_material(), other.digest_material()
        changes: list[str] = []
        for key in mine:
            before, after = mine[key], theirs.get(key)
            if before == after:
                continue
            if key == "assertions":
                changes.extend(_describe_assertion_changes(self, other))
                continue
            changes.append(f"{key}: {before!r} -> {after!r}")
        return changes


def _describe_assertion_changes(left: ResultSignature, right: ResultSignature) -> list[str]:
    before = {a.assertion_id: a for a in left.assertions}
    after = {a.assertion_id: a for a in right.assertions}
    changes = [
        f"assertion no longer evaluated: {name}" for name in sorted(set(before) - set(after))
    ]
    changes += [f"assertion newly evaluated: {name}" for name in sorted(set(after) - set(before))]
    for name in sorted(set(before) & set(after)):
        old, new = before[name], after[name]
        if old.status is not new.status:
            changes.append(f"assertion {name}: {old.status.value} -> {new.status.value}")
        elif old.classification is not new.classification:
            changes.append(
                f"assertion {name} classification: "
                f"{old.classification.value if old.classification else None} -> "
                f"{new.classification.value if new.classification else None}"
            )
    return changes


def signature_for(
    run: RunResult,
    *,
    target_fingerprint: str | None = None,
    scenario_fingerprint: str | None = None,
    first_meaningful_difference: str | None = None,
) -> ResultSignature:
    """Build the signature of one executed run.

    The three fingerprints are supplied rather than computed here. Two of them
    need the target definition and the scenario object, which a stored report does
    not carry, and computing them from a partial view would produce a value that
    looks authoritative and describes something else.
    """
    containment = run.containment
    capabilities = run.resolved_capabilities
    return ResultSignature(
        scenario_id=run.scenario_id,
        classification=run.classification,
        failed_assertion_ids=sorted(
            a.assertion_id for a in run.assertions if a.status is AssertionStatus.FAIL
        ),
        assertions=[
            AssertionSignature(
                assertion_id=a.assertion_id,
                status=a.status,
                classification=a.classification,
                security_relevant=a.security_relevant,
            )
            for a in run.assertions
        ],
        timeout_cause=run.timeout_cause,
        exit_code=run.process.exit_code,
        signal=run.process.signal,
        killed_by_harness=run.process.killed_by_harness,
        cleanup_ok=run.process.cleanup_ok,
        containment_outcome=containment.outcome.value if containment is not None else None,
        containment_usable=run.is_usable_evidence,
        target_fingerprint=target_fingerprint,
        scenario_fingerprint=scenario_fingerprint,
        capability_fingerprint=(
            capabilities.capability_fingerprint if capabilities is not None else None
        ),
        dimensions=run.dimensions,
        first_meaningful_difference=first_meaningful_difference,
    )


class SignatureRecomputationError(Exception):
    """A stored signature could not be rebuilt from its own contents."""


def recompute_digest(stored: dict[str, Any]) -> tuple[str | None, str | None]:
    """Re-derive a serialized signature's digest. Returns ``(digest, error)``.

    Routed through the model rather than re-implementing the hash. A second
    implementation is a second definition of what a signature *is*, and the two
    would agree right up until the real one changed — at which point this would
    certify agreement with a rule nobody else follows.
    """
    try:
        signature = ResultSignature.model_validate(stored)
    except Exception as exc:  # a truncated or hand-edited record, not a target fault
        return None, f"{type(exc).__name__}: {exc}"
    return signature.digest, None
