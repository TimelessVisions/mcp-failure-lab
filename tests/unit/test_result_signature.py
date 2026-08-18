"""A repeated classification is not a repeated result.

The external matrix used to compare classifications across repetitions and call
a repeated value stable. Two runs can both report ``SPEC_VIOLATION`` while
different assertions failed, while one was killed by the harness and the other
crashed on its own, or against two different capability surfaces. Each of those
is a different result wearing one label, and the old check said nothing about any
of them.

These tests pin both halves: the signature moves when the meaning of the result
moves, and does *not* move on durations, timestamps, run identifiers or anything
else that legitimately differs between two correct executions of the same thing.
A signature that churned on those would report instability every run and mean
nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime

from interop_lab.containment.model import ContainmentOutcome, ContainmentRecord, ExecutionMode
from interop_lab.containment.model import Provenance as ContainmentProvenance
from interop_lab.external.matrix import ExternalRunRecord, TargetEvidence, check_stability
from interop_lab.models.core import (
    AssertionResult,
    AssertionStatus,
    Classification,
    ProcessOutcome,
    ResultDimensions,
    RunResult,
    SecurityCategory,
    TimeoutCause,
)
from interop_lab.profiles.registry import Transport
from interop_lab.results.signature import (
    RESULT_SIGNATURE_VERSION,
    ResultSignature,
    recompute_digest,
    signature_for,
)


def assertion(
    assertion_id: str,
    *,
    status: AssertionStatus = AssertionStatus.FAIL,
    classification: Classification | None = Classification.SPEC_VIOLATION,
) -> AssertionResult:
    return AssertionResult(
        assertion_id=assertion_id,
        status=status,
        expected="e",
        actual="a",
        classification=classification if status is AssertionStatus.FAIL else None,
    )


def make_run(**overrides: object) -> RunResult:
    now = datetime.now(UTC).isoformat()
    base: dict[str, object] = {
        "run_id": "s__t__deadbeef",
        "scenario_id": "version.http_header_body_mismatch",
        "scenario_version": "1",
        "target_id": "t",
        "protocol_profile": "mcp-2026-07-28",
        "spec_version": "2026-07-28",
        "transport": Transport.STREAMABLE_HTTP,
        "seed": 0,
        "started_at": now,
        "finished_at": now,
        "duration_ms": 12.5,
        "classification": Classification.SPEC_VIOLATION,
        "assertions": [assertion("is_error"), assertion("code_32020")],
        "containment": ContainmentRecord(
            mode=ExecutionMode.CONTAINER,
            provenance=ContainmentProvenance.EXTERNAL,
            outcome=ContainmentOutcome.CONTAINED,
        ),
    }
    base.update(overrides)
    return RunResult.model_validate(base)


# ------------------------------------------------------ what must not move it


def test_durations_timestamps_and_run_ids_do_not_change_the_signature() -> None:
    """Two correct executions of the same thing differ in all three."""
    first = signature_for(make_run())
    second = signature_for(
        make_run(
            run_id="s__t__cafebabe",
            duration_ms=987.6,
            started_at="2020-01-01T00:00:00+00:00",
            finished_at="2020-01-01T00:00:01+00:00",
        )
    )

    assert first.digest == second.digest


def test_assertion_order_does_not_change_the_signature() -> None:
    forwards = signature_for(make_run())
    backwards = signature_for(make_run(assertions=[assertion("code_32020"), assertion("is_error")]))

    assert forwards.digest == backwards.digest


# ---------------------------------------------------------- what must move it


def test_the_same_classification_with_different_failing_assertions_is_a_different_result() -> None:
    """The case classification-only comparison calls stable."""
    before = signature_for(make_run())
    after = signature_for(make_run(assertions=[assertion("is_error"), assertion("id_preserved")]))

    assert before.classification is after.classification
    assert before.digest != after.digest
    assert "assertion no longer evaluated: code_32020" in before.describe_difference(after)


def test_an_assertion_failing_for_a_different_reason_is_a_different_result() -> None:
    before = signature_for(make_run())
    after = signature_for(
        make_run(
            assertions=[
                assertion("is_error"),
                assertion("code_32020", classification=Classification.INVALID_WIRE_MESSAGE),
            ]
        )
    )

    assert before.failed_assertion_ids == after.failed_assertion_ids
    assert before.digest != after.digest
    assert any("classification" in change for change in before.describe_difference(after))


def test_a_harness_kill_is_not_the_same_evidence_as_a_crash() -> None:
    killed = signature_for(make_run(process=ProcessOutcome(exit_code=-9, killed_by_harness=True)))
    crashed = signature_for(make_run(process=ProcessOutcome(exit_code=-9, killed_by_harness=False)))

    assert killed.digest != crashed.digest


def test_a_different_timeout_cause_is_a_different_result() -> None:
    harness = signature_for(make_run(timeout_cause=TimeoutCause.HARNESS_DEADLINE))
    target = signature_for(make_run(timeout_cause=TimeoutCause.TARGET_NO_RESPONSE))

    assert harness.digest != target.digest


def test_containment_that_was_not_obtained_is_a_different_result() -> None:
    contained = signature_for(make_run())
    unavailable = signature_for(
        make_run(
            containment=ContainmentRecord(
                mode=ExecutionMode.CONTAINER,
                provenance=ContainmentProvenance.EXTERNAL,
                outcome=ContainmentOutcome.CONTAINMENT_UNAVAILABLE,
                unavailable_reason="no docker",
            )
        )
    )

    assert contained.containment_usable is True
    assert unavailable.containment_usable is False
    assert contained.digest != unavailable.digest


def test_a_result_that_became_security_relevant_is_a_different_result() -> None:
    """Even with the classification and the failing assertions unchanged."""
    plain = signature_for(make_run())
    security = signature_for(
        make_run(
            dimensions=ResultDimensions(
                security_relevant=True,
                security_category=SecurityCategory.ORIGIN_VALIDATION,
                security_limitations=["one exchange"],
            )
        )
    )

    assert plain.classification is security.classification
    assert plain.failed_assertion_ids == security.failed_assertion_ids
    assert plain.digest != security.digest


def test_a_moved_capability_surface_is_a_different_result() -> None:
    before = signature_for(make_run()).model_copy(update={"capability_fingerprint": "a" * 64})
    after = before.model_copy(update={"capability_fingerprint": "b" * 64})

    assert before.digest != after.digest


def test_two_signature_versions_are_not_comparable() -> None:
    """A scheme change must read as "cannot compare", not as target instability."""
    current = signature_for(make_run())
    future = current.model_copy(update={"signature_version": RESULT_SIGNATURE_VERSION + 1})

    assert current.digest != future.digest


# ------------------------------------------------------------- recomputation


def test_a_stored_signature_recomputes_to_its_own_digest() -> None:
    signature = signature_for(make_run())
    digest, error = recompute_digest(signature.model_dump(mode="json"))

    assert error is None
    assert digest == signature.digest


def test_a_signature_that_cannot_be_loaded_reports_why() -> None:
    digest, error = recompute_digest({"scenario_id": "x", "classification": "NOT_A_CLASSIFICATION"})

    assert digest is None
    assert error is not None


# --------------------------------------------- stability across repetitions


def evidence_with(*records: ExternalRunRecord) -> TargetEvidence:
    return TargetEvidence(
        target_key="example",
        target_file="targets/external/example.yaml",
        image_name="interop-lab/example",
        runs=list(records),
    )


def repetition(
    signature: ResultSignature,
    *,
    number: int,
    digest: str | None = None,
    scenario: str = "a",
) -> ExternalRunRecord:
    return ExternalRunRecord(
        target_key="example",
        target_file="targets/external/example.yaml",
        repetition=number,
        report_parsed=True,
        per_scenario_classification={scenario: signature.classification.value},
        per_scenario_result_signature={scenario: signature},
        per_scenario_result_signature_digest={scenario: digest or signature.digest},
    )


def test_a_stable_classification_with_a_changed_signature_is_instability() -> None:
    """The claim this whole phase exists to stop the job from making."""
    first = signature_for(make_run())
    second = signature_for(make_run(assertions=[assertion("is_error"), assertion("id_preserved")]))
    assert first.classification is second.classification

    evidence = evidence_with(repetition(first, number=1), repetition(second, number=2))
    check_stability(evidence)

    assert evidence.stable_across_repetitions is False
    assert any("different result signature" in entry for entry in evidence.instability)
    # And it names what moved, rather than printing two hex strings.
    assert any("code_32020" in entry for entry in evidence.instability)


def test_a_missing_signature_is_instability_not_silent_agreement() -> None:
    signature = signature_for(make_run())
    without = ExternalRunRecord(
        target_key="example",
        target_file="targets/external/example.yaml",
        repetition=2,
        report_parsed=True,
        per_scenario_classification={"a": signature.classification.value},
    )

    evidence = evidence_with(repetition(signature, number=1), without)
    check_stability(evidence)

    assert evidence.stable_across_repetitions is False
    assert any("carries no result signature" in entry for entry in evidence.instability)


def test_a_stored_digest_that_disagrees_with_its_contents_is_instability() -> None:
    signature = signature_for(make_run())

    evidence = evidence_with(
        repetition(signature, number=1),
        repetition(signature, number=2, digest="0" * 64),
    )
    check_stability(evidence)

    assert evidence.stable_across_repetitions is False
    assert any("was edited" in entry for entry in evidence.instability)


def test_a_scenario_measured_once_and_not_again_is_instability() -> None:
    signature = signature_for(make_run())
    both = repetition(signature, number=1)
    both.per_scenario_classification["b"] = "PASS"
    both.per_scenario_result_signature["b"] = signature
    both.per_scenario_result_signature_digest["b"] = signature.digest

    evidence = evidence_with(both, repetition(signature, number=2))
    check_stability(evidence)

    assert evidence.stable_across_repetitions is False
    assert any("change in what was measured" in entry for entry in evidence.instability)


def test_three_identical_repetitions_are_stable() -> None:
    signature = signature_for(make_run())
    evidence = evidence_with(*(repetition(signature, number=n) for n in (1, 2, 3)))
    check_stability(evidence)

    assert evidence.stable_across_repetitions is True
    assert evidence.instability == []
    assert evidence.classifications == {"SPEC_VIOLATION": 1}
