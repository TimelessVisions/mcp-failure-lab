"""What makes a benchmark sample valid, and what makes one rejected.

The distinction carries the whole benchmark. A rejected sample is not a slower or
worse measurement — it is a measurement of something other than what the
definition describes, and letting it into a median moves that median for a reason
the number does not disclose. So rejection is explicit, typed, and total: a
rejected sample contributes to no timing statistic and no classification summary,
and the count of rejections is itself published.

The checks are deliberately mechanical. Every one of them compares a sample
against something the definition *pinned* in advance, so none of them requires a
judgement at analysis time about whether a run "looks right".
"""

from __future__ import annotations

from pathlib import Path

from interop_lab.benchmark.model import (
    BenchmarkDefinition,
    BenchmarkSample,
    MeasurementState,
    SampleRejection,
    TimingSummary,
)
from interop_lab.models.core import Classification


def _reject(sample: BenchmarkSample, kind: SampleRejection, detail: str) -> None:
    sample.rejections.append(kind)
    sample.rejection_detail.append(detail)


def validate_sample(
    sample: BenchmarkSample,
    definition: BenchmarkDefinition,
    *,
    expected_target_fingerprint: str | None = None,
    expected_scenario_fingerprint: str | None = None,
    expected_capability_fingerprint: str | None = None,
    artifacts_root: Path | None = None,
) -> BenchmarkSample:
    """Apply every rejection rule, recording all of them rather than the first.

    All of them, because a sample that failed for four reasons and reports one
    invites someone to fix that one and expect the sample to count.
    """
    if sample.exit_code is not None and sample.exit_code not in definition.allowed_exit_codes:
        _reject(
            sample,
            SampleRejection.UNEXPECTED_EXIT_CODE,
            f"exit code {sample.exit_code} is outside the pinned set "
            f"{definition.allowed_exit_codes}; the command stopped before measuring",
        )

    root = artifacts_root or Path()
    for required in definition.expected_artifacts:
        if required in sample.observed_artifacts:
            continue
        if (root / required).is_file():
            continue
        _reject(
            sample,
            SampleRejection.MISSING_ARTIFACT,
            f"the definition requires {required!r} and this sample produced none. "
            "A run that wrote nothing is not a measurement.",
        )

    if expected_target_fingerprint and sample.target_fingerprint != expected_target_fingerprint:
        _reject(
            sample,
            SampleRejection.TARGET_FINGERPRINT_MISMATCH,
            f"target fingerprint {sample.target_fingerprint} does not match the pinned "
            f"{expected_target_fingerprint}; this sample measured a different target",
        )

    if (
        expected_scenario_fingerprint
        and sample.scenario_fingerprint != expected_scenario_fingerprint
    ):
        _reject(
            sample,
            SampleRejection.SCENARIO_FINGERPRINT_MISMATCH,
            f"scenario fingerprint {sample.scenario_fingerprint} does not match the "
            f"pinned {expected_scenario_fingerprint}; this sample ran a different scenario",
        )

    # Capability identity is only required where the scenario needed an
    # observation. A declarative skip — wrong transport, wrong profile — is decided
    # before any traffic, so demanding a surface there would reject a correct
    # sample for a measurement nobody needed to make.
    if sample.classification is not Classification.NOT_APPLICABLE:
        if sample.capability_fingerprint is None:
            _reject(
                sample,
                SampleRejection.CAPABILITY_FINGERPRINT_MISSING,
                "no capability fingerprint, so the applicability decisions in this "
                "sample cannot be re-checked against what the target advertised",
            )
        elif (
            expected_capability_fingerprint
            and sample.capability_fingerprint != expected_capability_fingerprint
        ):
            _reject(
                sample,
                SampleRejection.CAPABILITY_FINGERPRINT_CHANGED,
                f"capability fingerprint {sample.capability_fingerprint} differs from "
                f"{expected_capability_fingerprint} observed for the same target in this "
                "execution; the surface moved underneath the measurement",
            )

    if definition.require_complete_result_signature and sample.scenario_id is not None:
        signature = sample.result_signature
        if signature is None or sample.result_signature_digest is None:
            _reject(
                sample,
                SampleRejection.RESULT_SIGNATURE_MISSING,
                "no result signature, so a repeated classification could not be shown "
                "to be a repeated result",
            )
        elif signature.digest != sample.result_signature_digest:
            _reject(
                sample,
                SampleRejection.RESULT_SIGNATURE_UNSTABLE,
                f"stored signature digest {sample.result_signature_digest} does not "
                f"describe its own contents, which hash to {signature.digest}",
            )

    if not sample.containment_usable:
        _reject(
            sample,
            SampleRejection.CONTAINMENT_UNUSABLE,
            "containment was requested and not obtained, so this sample is not "
            "evidence about the target",
        )

    if sample.classification is Classification.HARNESS_ERROR:
        _reject(
            sample,
            SampleRejection.HARNESS_ERROR,
            "the harness or the environment failed. Never a verdict about the target, "
            "and never a valid timing sample either.",
        )

    if not sample.cleanup_ok:
        _reject(
            sample,
            SampleRejection.CLEANUP_FAILED,
            "cleanup failed, so a later sample may have run against leaked state",
        )

    if not sample.tracked_tree_clean:
        _reject(
            sample,
            SampleRejection.TRACKED_TREE_CHANGED,
            "the tracked tree changed during this sample, so it did not measure the "
            "commit it claims to have measured",
        )

    bound_ms = definition.timing_bounds.per_sample_timeout_s * 1000.0
    if sample.duration_ms > bound_ms:
        _reject(
            sample,
            SampleRejection.TIMEOUT_BOUND_EXCEEDED,
            f"took {sample.duration_ms:.0f} ms, beyond the pinned bound of "
            f"{bound_ms:.0f} ms. Past that this measures an environment problem "
            "rather than the operation, so it is rejected instead of widening the range.",
        )

    return sample


def check_signature_stability(samples: list[BenchmarkSample]) -> list[str]:
    """Hold every repetition of one scenario to one complete signature.

    Returns the instabilities found, described rather than merely detected.
    Classification equality is not consulted at all here: it is a strictly weaker
    condition, and a signature comparison that passed would have implied it.
    """
    valid = [s for s in samples if s.valid and s.result_signature is not None]
    if len(valid) < 2:
        return []

    problems: list[str] = []
    reference = valid[0]
    assert reference.result_signature is not None
    for sample in valid[1:]:
        signature = sample.result_signature
        assert signature is not None
        if signature.digest == reference.result_signature.digest:
            continue
        changes = reference.result_signature.describe_difference(signature)
        problems.append(
            f"{sample.scenario_id}: repetition {sample.repetition} differs from "
            f"repetition {reference.repetition} — "
            f"{'; '.join(changes) or 'no field-level difference found'}"
        )
    return problems


def summarise_timing(
    name: str,
    samples: list[BenchmarkSample],
    *,
    minimum_samples: int,
    limitations: list[str] | None = None,
) -> TimingSummary:
    """Median, minimum, maximum and every sample. Nothing inferential.

    When there are not enough valid samples the headline number is **absent**
    rather than computed from what survived. A median of two is a number, and
    publishing it beside medians of five would invite a comparison the evidence
    does not support.
    """
    base = limitations or []
    counted = [s for s in samples if s.counts_for_timing]
    rejected = sum(1 for s in samples if not s.valid)
    values = sorted(s.duration_ms for s in counted)

    if len(values) < minimum_samples:
        return TimingSummary(
            name=name,
            state=MeasurementState.NOT_MEASURED,
            samples_ms=values,
            rejected_samples=rejected,
            insufficient_reason=(
                f"{len(values)} valid measured sample(s), and this benchmark requires "
                f"{minimum_samples} before quoting a headline timing. The samples are "
                "recorded; no median is reported from them."
            ),
            limitations=base,
        )

    import statistics

    return TimingSummary(
        name=name,
        state=MeasurementState.MEASURED,
        samples_ms=values,
        median_ms=statistics.median(values),
        min_ms=values[0],
        max_ms=values[-1],
        rejected_samples=rejected,
        limitations=[
            *base,
            "Median of a small sample with its full range and every individual value. "
            "No confidence interval and no significance claim: five samples on a "
            "shared runner cannot support either.",
        ],
    )
