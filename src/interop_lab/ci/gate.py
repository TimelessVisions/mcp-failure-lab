"""Regression evaluation against an approved baseline."""

from __future__ import annotations

import json
from pathlib import Path

from interop_lab.artifacts.portability import resolve_artifact_uri
from interop_lab.capabilities.model import describe_capability_drift
from interop_lab.ci.baseline import (
    EXIT_CODES,
    TARGET_FINGERPRINT_VERSION,
    BaselineDocument,
    BaselineEntry,
    DivergencePolicy,
    DriftPolicy,
    GateOutcome,
    GateResult,
    Improvement,
    InconclusivePolicy,
    Regression,
    RegressionKind,
    ReproductionExpectation,
    UnknownResultPolicy,
    scenario_fingerprint,
    target_fingerprint,
)
from interop_lab.models.core import (
    FAILING,
    AssertionStatus,
    Classification,
    RunResult,
    TargetDefinition,
)
from interop_lab.naming import CLI_COMMAND
from interop_lab.replay.repro import load_repro
from interop_lab.scenarios.model import Scenario


def _reproduction(run: RunResult) -> str:
    return f"{CLI_COMMAND} run -t <target.yaml> --scenario {run.scenario_id}"


def _failed_assertions(run: RunResult) -> list[str]:
    return [a.assertion_id for a in run.assertions if a.status is AssertionStatus.FAIL]


def _missing_divergence_approval(
    entry: BaselineEntry,
    *,
    run: RunResult,
    observed_target: str,
    observed_scenario: str,
) -> list[str]:
    """List the approval evidence a divergence is missing.

    ``ALLOW_IF_APPROVED`` has to mean approved. Without these checks the enum
    name claims more than the code enforces, and every divergence sails through
    on a policy nobody actually satisfied.
    """
    missing: list[str] = []
    if not entry.approved_differential_verdict:
        missing.append("baseline entry records no approved_differential_verdict")
    elif entry.approved_differential_verdict != Classification.BEHAVIORAL_DIVERGENCE.value:
        missing.append(
            f"approved verdict is {entry.approved_differential_verdict!r}, "
            f"not {Classification.BEHAVIORAL_DIVERGENCE.value}"
        )
    if not entry.approval_reason:
        missing.append("no approval_reason recorded")
    if not entry.evidence:
        missing.append("no evidence reference recorded")
    if entry.target_fingerprint != observed_target:
        missing.append("target fingerprint changed since the divergence was approved")
    if entry.scenario_fingerprint != observed_scenario:
        missing.append("scenario fingerprint changed since the divergence was approved")
    # An approved divergence must not be carrying a normative failure with it.
    if any(
        a.classification is Classification.SPEC_VIOLATION
        for a in run.assertions
        if a.status is AssertionStatus.FAIL
    ):
        missing.append("a normative failure accompanies the divergence")
    return missing


def _check_capability_identity(
    entry: BaselineEntry, run: RunResult, *, policy_blocks: bool
) -> list[Regression]:
    """Hold this run's observed surface to the one the entry was approved against.

    Four outcomes, and the distinctions between them are the point:

    * The entry never recorded a surface, and does not require one. Nothing to
      compare, so nothing is claimed.
    * The entry's applicability rested on observing the target and this run did
      not observe it. That is ``CAPABILITY_UNRESOLVED`` and it always blocks. It
      is emphatically not ``NOT_APPLICABLE``: reporting the scenario as skipped
      would state a reason — that the target lacks the tool — which nobody
      established, and reusing the previously approved record would cite an
      observation this run did not make.
    * The record and its own digest disagree. That is a harness integrity
      failure, not a statement about anyone's server.
    * The surface moved. Reported as ``CAPABILITY_DRIFT``, described rather than
      merely detected, and never converted into a verdict — a capability change
      is not a defect, and the gate does not have the standing to decide whether
      this one is acceptable. Blocking by policy, so the decision lands on a
      person.
    """
    problems: list[Regression] = []
    observed = run.resolved_capabilities

    # An approved skip has no capability precondition to satisfy. The executor
    # decides transport, profile and role before touching the target — a scenario
    # restricted to another transport cannot become applicable no matter what the
    # target advertises — so those runs carry no capability record by design, and
    # demanding one would report a missing observation nobody needed to make.
    #
    # A scenario that *was* applicable and is now skipped is a different thing
    # entirely, and SILENTLY_SKIPPED already reports it.
    if run.classification is Classification.NOT_APPLICABLE and not entry.applicable:
        return problems

    def regression(
        kind: RegressionKind, detail: str, *, blocking: bool, **kwargs: str | None
    ) -> None:
        problems.append(
            Regression(
                kind=kind,
                target_id=entry.target_id,
                scenario_id=entry.scenario_id,
                detail=detail,
                evidence=run.trace_path,
                blocking=blocking,
                **kwargs,
            )
        )

    usable = observed is not None and observed.is_usable
    if not usable:
        if entry.capability_required:
            status = observed.status.value if observed is not None else "no record at all"
            regression(
                RegressionKind.CAPABILITY_UNRESOLVED,
                (
                    f"this entry's applicability was approved on an observed capability "
                    f"surface, and this run resolved capabilities to {status}. The "
                    "approved record is not reused and the scenario is not reported as "
                    "skipped: both would cite an observation this run did not make."
                ),
                baseline_value=entry.capability_fingerprint,
                blocking=True,
            )
        return problems

    assert observed is not None  # narrowed by `usable`
    surface = observed.fingerprint()

    # The stored digest must describe the record it travels with. A live run
    # cannot normally violate this — the model computes it — so a mismatch means
    # something rewrote the record in flight.
    if observed.capability_fingerprint not in (None, surface.digest):
        regression(
            RegressionKind.HARNESS_ERROR,
            (
                f"the run's capability record states fingerprint "
                f"{observed.capability_fingerprint} but its contents hash to "
                f"{surface.digest}. This is a harness integrity failure, not an "
                "observation about the target."
            ),
            blocking=True,
        )
        return problems

    if entry.capability_fingerprint is None or entry.capability_fingerprint == surface.digest:
        return problems

    approved = entry.approved_capability_surface
    changes = describe_capability_drift(approved, surface) if approved is not None else []
    described = (
        "; ".join(changes)
        if changes
        else "the approved surface was not recorded in enough detail to say what moved"
    )
    regression(
        RegressionKind.CAPABILITY_DRIFT,
        (
            f"the target advertises a different capability surface than the one this "
            f"entry was approved against: {described}. A capability change is not by "
            "itself a defect and is not reported as one. The approved result was "
            "decided against a surface that no longer exists, so it needs review and "
            "an explicit re-approval rather than an automatic one."
        ),
        baseline_value=entry.capability_fingerprint,
        observed_value=surface.digest,
        blocking=policy_blocks,
    )
    return problems


def _check_security_dimension(
    entry: BaselineEntry, run: RunResult, *, policy_blocks: bool
) -> list[Regression]:
    """Report a security dimension the baseline never approved.

    Deliberately independent of the classification comparison, and evaluated even
    when the classification is unchanged. That is the whole point: the case this
    catches is a result that keeps its approved outcome — or moves to one policy
    tolerates, such as an allowed BEHAVIORAL_DIVERGENCE — while acquiring a
    security dimension it did not have. A gate comparing only classifications
    reports nothing there and stays green.

    Never evaluative. Whether a newly security-relevant result is acceptable is a
    judgement, and the gate's job is to stop until someone has made it. The
    category is quoted so the reviewer knows what property is involved without
    opening the artifact.
    """
    observed = run.dimensions
    approved = entry.allowed_dimensions
    if not observed.security_relevant or approved.security_relevant:
        return []

    category = (
        observed.security_category.value
        if observed.security_category is not None
        else "uncategorised"
    )
    return [
        Regression(
            kind=RegressionKind.NEW_SECURITY_RELEVANT,
            target_id=entry.target_id,
            scenario_id=entry.scenario_id,
            baseline_value="not security-relevant",
            observed_value=f"security-relevant ({category})",
            detail=(
                f"this result is now security-relevant under {category}, and the "
                f"baseline approved it as not security-relevant. Its primary "
                f"classification is {run.classification.value}. A security-relevant "
                "observation is not a vulnerability claim and this gate makes none: "
                f"vulnerability_status is {observed.vulnerability_status.value}. It "
                "blocks so the change reaches a person rather than a summary count."
            ),
            evidence=run.trace_path,
            reproduction=_reproduction(run),
            normative_source=next(
                (a.normative_source for a in run.assertions if a.normative_source), None
            ),
            blocking=policy_blocks,
        )
    ]


def _check_reproduction(
    entry: BaselineEntry, expectation: ReproductionExpectation, *, artifacts_root: Path
) -> list[Regression]:
    """Verify a declared reproduction package still demonstrates the finding."""
    problems: list[Regression] = []

    def fail(kind: RegressionKind, detail: str, *, blocking: bool = True) -> None:
        problems.append(
            Regression(
                kind=kind,
                target_id=entry.target_id,
                scenario_id=entry.scenario_id,
                baseline_value=expectation.expected_classification.value,
                detail=detail,
                evidence=expectation.package_uri,
                blocking=blocking,
            )
        )

    package = resolve_artifact_uri(expectation.package_uri, root=artifacts_root)
    repro_yaml = package / "repro.yaml"
    minimization = package / "minimization.json"

    if not repro_yaml.is_file():
        fail(RegressionKind.REPRODUCTION_MISSING, f"no repro.yaml at {expectation.package_uri}")
        return problems

    try:
        scenario, target, expected_classification, _ = load_repro(repro_yaml)
    except Exception as exc:
        fail(RegressionKind.REPRODUCTION_MISMATCH, f"reproduction does not load: {exc}")
        return problems

    if expected_classification is not expectation.expected_classification:
        fail(
            RegressionKind.REPRODUCTION_MISMATCH,
            f"package expects {expected_classification.value}, baseline expects "
            f"{expectation.expected_classification.value}",
        )

    if expectation.expected_target_fingerprint:
        observed = target_fingerprint(target)
        if observed != expectation.expected_target_fingerprint:
            fail(
                RegressionKind.REPRODUCTION_MISMATCH,
                f"package target fingerprint {observed} does not match approved "
                f"{expectation.expected_target_fingerprint}",
            )
    if expectation.expected_scenario_fingerprint:
        observed = scenario_fingerprint(scenario)
        if observed != expectation.expected_scenario_fingerprint:
            fail(
                RegressionKind.REPRODUCTION_MISMATCH,
                f"package scenario fingerprint {observed} does not match approved "
                f"{expectation.expected_scenario_fingerprint}",
            )

    if not minimization.is_file():
        fail(RegressionKind.REPRODUCTION_MISSING, "no minimization.json in the package")
        return problems

    try:
        record = json.loads(minimization.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(RegressionKind.REPRODUCTION_MISMATCH, f"minimization.json unreadable: {exc}")
        return problems

    if not record.get("stable"):
        fail(RegressionKind.REPRODUCTION_UNSTABLE, "the reproduction is not marked stable")
    runs = int(record.get("stability_runs", 0))
    if runs < expectation.minimum_stability_runs:
        fail(
            RegressionKind.REPRODUCTION_UNSTABLE,
            f"reproduced {runs} consecutive times, baseline requires "
            f"{expectation.minimum_stability_runs}",
        )

    preserved = sorted(record.get("preserved_failing_assertions", []))
    if expectation.expected_failed_assertions and preserved != sorted(
        expectation.expected_failed_assertions
    ):
        fail(
            RegressionKind.REPRODUCTION_MISMATCH,
            f"failure signature changed: package preserves {preserved}, baseline "
            f"approved {sorted(expectation.expected_failed_assertions)}",
        )

    return problems


def evaluate(
    *,
    baseline: BaselineDocument,
    runs: list[tuple[RunResult, Scenario, TargetDefinition]],
    artifacts_root: Path | None = None,
    conformance_outcomes: dict[str, str] | None = None,
) -> GateResult:
    """Compare a fresh matrix run against an approved baseline.

    ``conformance_outcomes`` maps target id to the official framework's outcome
    for this run. It is compared separately and never merged into the harness's
    own classification vocabulary.
    """
    root = artifacts_root or Path("artifacts")
    observed_conformance = conformance_outcomes or {}
    policy = baseline.policy

    # A baseline recorded under an older fingerprint scheme cannot be compared
    # against fingerprints computed under a newer one. Every entry would report
    # drift, which is technically true and useless: it hides any real regression
    # in a wall of noise and invites someone to re-approve the baseline blind.
    if baseline.target_fingerprint_version != TARGET_FINGERPRINT_VERSION:
        return GateResult(
            baseline_id=baseline.baseline_id,
            suite=baseline.suite,
            outcome=GateOutcome.CONFIGURATION_ERROR,
            exit_code=EXIT_CODES[GateOutcome.CONFIGURATION_ERROR],
            harness_failures=[
                f"baseline {baseline.baseline_id!r} was recorded with target "
                f"fingerprint v{baseline.target_fingerprint_version}, but this harness "
                f"computes v{TARGET_FINGERPRINT_VERSION}. Comparing across schemes "
                "would report drift for every entry and hide any real regression. "
                "Re-create the baseline deliberately with `baseline create`."
            ],
        )
    regressions: list[Regression] = []
    improvements: list[Improvement] = []
    environment_mismatches: list[str] = []
    harness_failures: list[str] = []
    unchanged = 0

    observed_keys: set[tuple[str, str]] = set()

    for run, scenario, target in runs:
        key = (run.target_id, run.scenario_id)
        observed_keys.add(key)
        entry = baseline.lookup(run.target_id, run.scenario_id)

        if run.environment_warnings:
            environment_mismatches.extend(
                f"{run.target_id}/{run.scenario_id}: {w}" for w in run.environment_warnings
            )
            if policy.runtime_version_drift is DriftPolicy.FAIL:
                regressions.append(
                    Regression(
                        kind=RegressionKind.TARGET_DRIFT,
                        target_id=run.target_id,
                        scenario_id=run.scenario_id,
                        detail="; ".join(run.environment_warnings),
                        blocking=True,
                    )
                )

        # A harness error is never a target verdict, and never a pass.
        if run.classification is Classification.HARNESS_ERROR:
            harness_failures.append(f"{run.target_id}/{run.scenario_id}: {run.harness_error}")
            if policy.fail_on_harness_error:
                regressions.append(
                    Regression(
                        kind=RegressionKind.HARNESS_ERROR,
                        target_id=run.target_id,
                        scenario_id=run.scenario_id,
                        observed_value=run.classification.value,
                        detail=run.harness_error or "harness error",
                        evidence=run.trace_path,
                        blocking=True,
                    )
                )
            continue

        if entry is None:
            # An unbaselined result is not a pass — not even when it *is* a PASS.
            # A new passing scenario still changes the approved measurement
            # surface, and approving that is a human decision.
            regressions.append(
                Regression(
                    kind=RegressionKind.UNKNOWN_RESULT,
                    target_id=run.target_id,
                    scenario_id=run.scenario_id,
                    observed_value=run.classification.value,
                    detail=(
                        f"result {run.classification.value} is not present in the baseline. "
                        "Approve it explicitly with `baseline create` rather than letting "
                        "the gate infer intent; a new PASS still widens what the gate covers."
                    ),
                    evidence=run.trace_path,
                    reproduction=_reproduction(run),
                    blocking=policy.unknown_result is UnknownResultPolicy.FAIL,
                )
            )
            continue

        # Fingerprints: is the baseline still describing this thing?
        observed_target = target_fingerprint(target)
        if observed_target != entry.target_fingerprint:
            regressions.append(
                Regression(
                    kind=RegressionKind.TARGET_DRIFT,
                    target_id=run.target_id,
                    scenario_id=run.scenario_id,
                    baseline_value=entry.target_fingerprint,
                    observed_value=observed_target,
                    detail=(
                        "the target definition changed since the baseline was approved, "
                        "so previous results no longer describe it"
                    ),
                    blocking=policy.fail_on_target_fingerprint_drift,
                )
            )

        observed_scenario = scenario_fingerprint(scenario)
        if observed_scenario != entry.scenario_fingerprint:
            regressions.append(
                Regression(
                    kind=RegressionKind.SCENARIO_DRIFT,
                    target_id=run.target_id,
                    scenario_id=run.scenario_id,
                    baseline_value=entry.scenario_fingerprint,
                    observed_value=observed_scenario,
                    detail="the scenario definition changed since the baseline was approved",
                    blocking=policy.fail_on_scenario_fingerprint_drift,
                )
            )

        # The third identity, and the only one that describes the *target's*
        # behaviour rather than a file in this repository. Recomputed from this
        # run's own observation, under the execution mode this run actually used.
        capability_problems = _check_capability_identity(
            entry, run, policy_blocks=policy.capability_drift is DriftPolicy.FAIL
        )
        regressions.extend(capability_problems)
        # A capability record that disagrees with its own digest is the harness
        # failing, not a regression in anything. Reported through the same
        # channel as any other harness failure so the gate exits 3 rather than
        # 1: an exit code that says "something regressed" would be a lie.
        harness_failures.extend(
            f"{run.target_id}/{run.scenario_id}: {problem.detail}"
            for problem in capability_problems
            if problem.kind is RegressionKind.HARNESS_ERROR
        )
        if any(
            problem.kind in (RegressionKind.CAPABILITY_UNRESOLVED, RegressionKind.HARNESS_ERROR)
            for problem in capability_problems
        ):
            # The approved conditions were not reproduced, so comparing this
            # run's classification against the entry would compare two results
            # that were not measured against the same target surface.
            continue

        # Evaluated before the classification comparison and never inside it: a
        # security dimension can appear on a result whose classification is
        # entirely unchanged, and every branch below either continues or returns.
        regressions.extend(
            _check_security_dimension(
                entry, run, policy_blocks=policy.fail_on_new_security_relevant_result
            )
        )

        allowed = entry.allowed_classification
        observed = run.classification

        # An approved result the harness could not decide. Reported before the
        # classification comparison so it is never described as the target having
        # behaved differently — nobody established that it did.
        #
        # PASS -> INCONCLUSIVE blocks by default. So does NOT_APPLICABLE ->
        # INCONCLUSIVE when the original skip rested on an observation: the entry
        # recorded "the target does not advertise this", and an unresolved run has
        # not re-established that. A declarative skip — wrong transport, wrong
        # profile — needs no observation and cannot become inconclusive this way,
        # which is why `capability_required` gates the second case.
        if observed is Classification.INCONCLUSIVE and allowed is not Classification.INCONCLUSIVE:
            observation_was_required = (
                allowed is not Classification.NOT_APPLICABLE or entry.capability_required
            )
            if observation_was_required:
                regressions.append(
                    Regression(
                        kind=RegressionKind.INCONCLUSIVE_RESULT,
                        target_id=run.target_id,
                        scenario_id=run.scenario_id,
                        baseline_value=allowed.value,
                        observed_value=observed.value,
                        detail=(
                            f"the baseline approved {allowed.value} and this run could "
                            "not reach a verdict: "
                            f"{run.harness_error or run.not_applicable_reason or 'no detail'}. "
                            "An undecided result is not a pass and is not reported as a "
                            "skip; reporting it as either would state something nobody "
                            "observed."
                        ),
                        evidence=run.trace_path,
                        reproduction=_reproduction(run),
                        blocking=policy.inconclusive_result is InconclusivePolicy.FAIL,
                    )
                )
                continue

        # An applicable scenario that quietly became skipped is a coverage loss.
        if observed is Classification.NOT_APPLICABLE and entry.applicable:
            regressions.append(
                Regression(
                    kind=RegressionKind.SILENTLY_SKIPPED,
                    target_id=run.target_id,
                    scenario_id=run.scenario_id,
                    baseline_value=allowed.value,
                    observed_value=observed.value,
                    detail=(
                        f"scenario was applicable at baseline time but is now skipped: "
                        f"{run.not_applicable_reason}"
                    ),
                    blocking=True,
                )
            )
            continue

        if observed is allowed:
            # Same classification: the failure signature must also match, or the
            # run is failing for a different reason under the same label.
            observed_failed = _failed_assertions(run)
            if sorted(observed_failed) != sorted(entry.expected_failed_assertions):
                regressions.append(
                    Regression(
                        kind=RegressionKind.SIGNATURE_CHANGED,
                        target_id=run.target_id,
                        scenario_id=run.scenario_id,
                        baseline_value=str(entry.expected_failed_assertions),
                        observed_value=str(observed_failed),
                        detail=(
                            "classification is unchanged but a different set of assertions "
                            "failed, so this is not the same finding"
                        ),
                        evidence=run.trace_path,
                        reproduction=_reproduction(run),
                        blocking=True,
                    )
                )
            else:
                unchanged += 1
            continue

        # Improvement: previously failing, now passing.
        if allowed in FAILING and observed is Classification.PASS:
            improvements.append(
                Improvement(
                    target_id=run.target_id,
                    scenario_id=run.scenario_id,
                    baseline_value=allowed.value,
                    observed_value=observed.value,
                    detail=(
                        "previously-approved failure now passes; review and re-approve "
                        "the baseline rather than letting it update itself"
                    ),
                )
            )
            continue

        # New behavioural divergence is configurable — but ALLOW_IF_APPROVED
        # means approved, and the evidence of approval has to be present.
        if observed is Classification.BEHAVIORAL_DIVERGENCE:
            if policy.new_behavioral_divergence is DivergencePolicy.ALLOW_IF_APPROVED:
                missing = _missing_divergence_approval(
                    entry,
                    run=run,
                    observed_target=observed_target,
                    observed_scenario=observed_scenario,
                )
                if not missing:
                    unchanged += 1
                    continue
                regressions.append(
                    Regression(
                        kind=RegressionKind.NEW_DIVERGENCE,
                        target_id=run.target_id,
                        scenario_id=run.scenario_id,
                        baseline_value=entry.approved_differential_verdict,
                        observed_value=observed.value,
                        detail=("divergence is not approved: " + "; ".join(missing)),
                        evidence=run.trace_path,
                        reproduction=_reproduction(run),
                        blocking=True,
                    )
                )
                continue
            regressions.append(
                Regression(
                    kind=RegressionKind.NEW_DIVERGENCE,
                    target_id=run.target_id,
                    scenario_id=run.scenario_id,
                    baseline_value=allowed.value,
                    observed_value=observed.value,
                    detail="a new behavioural divergence appeared",
                    evidence=run.trace_path,
                    reproduction=_reproduction(run),
                    blocking=policy.new_behavioral_divergence is DivergencePolicy.FAIL,
                )
            )
            continue

        blocking = observed in FAILING and (
            policy.fail_on_new_normative_failure
            if observed is Classification.SPEC_VIOLATION
            else policy.fail_on_crash
            if observed is Classification.CRASH
            else policy.fail_on_hang
            if observed in (Classification.HANG, Classification.TIMEOUT)
            else policy.fail_on_cleanup_failure
            if observed is Classification.CLEANUP_FAILURE
            else True
        )
        regressions.append(
            Regression(
                kind=RegressionKind.NEW_FAILURE
                if observed in FAILING
                else RegressionKind.CLASSIFICATION_CHANGED,
                target_id=run.target_id,
                scenario_id=run.scenario_id,
                baseline_value=allowed.value,
                observed_value=observed.value,
                detail=f"{allowed.value} -> {observed.value}",
                evidence=run.trace_path,
                reproduction=_reproduction(run),
                normative_source=next(
                    (a.normative_source for a in run.assertions if a.normative_source), None
                ),
                blocking=blocking,
            )
        )

    # Reproduction expectations, checked once per entry rather than per run.
    for entry in baseline.entries:
        expectation = entry.reproduction
        if expectation is None or not expectation.required:
            continue
        if policy.fail_on_reproduction_instability:
            regressions.extend(_check_reproduction(entry, expectation, artifacts_root=root))

    # Official conformance outcome, reported separately from Interop findings.
    if policy.fail_on_conformance_outcome_change:
        for entry in baseline.entries:
            expected_outcome = entry.official_conformance_outcome
            if not expected_outcome:
                continue
            actual = observed_conformance.get(entry.target_id)
            if not actual:
                # A recorded official outcome is a required check. Skipping the
                # comparison left the gate passed while the check did not run.
                regressions.append(
                    Regression(
                        kind=RegressionKind.CONFORMANCE_UNMEASURED,
                        target_id=entry.target_id,
                        scenario_id=entry.scenario_id,
                        baseline_value=expected_outcome,
                        observed_value="NOT_MEASURED",
                        detail=(
                            "the baseline records an official conformance outcome and "
                            "this run did not re-measure it. Missing execution is "
                            "NOT_MEASURED, never a pass. This stays in the official "
                            "framework's vocabulary and is not a Failure Lab finding."
                        ),
                        blocking=True,
                    )
                )
                continue
            if actual != expected_outcome:
                regressions.append(
                    Regression(
                        kind=RegressionKind.CONFORMANCE_OUTCOME_CHANGED,
                        target_id=entry.target_id,
                        scenario_id=entry.scenario_id,
                        baseline_value=expected_outcome,
                        observed_value=actual,
                        detail=(
                            "the official conformance outcome changed. This is a statement "
                            "by the official framework, not a Failure Lab finding."
                        ),
                        blocking=True,
                    )
                )

    # Anything the baseline expected but the run did not produce.
    for entry in baseline.entries:
        if entry.key in observed_keys:
            continue
        missing_target = entry.target_id not in {r.target_id for r, _, _ in runs}
        regressions.append(
            Regression(
                kind=RegressionKind.MISSING_TARGET
                if missing_target
                else RegressionKind.MISSING_SCENARIO,
                target_id=entry.target_id,
                scenario_id=entry.scenario_id,
                baseline_value=entry.allowed_classification.value,
                detail=(
                    "expected by the baseline but absent from this run; a missing result "
                    "is never treated as a pass"
                ),
                blocking=(
                    policy.fail_on_missing_target
                    if missing_target
                    else policy.fail_on_missing_scenario
                ),
            )
        )

    blocking = [r for r in regressions if r.blocking]
    if harness_failures and policy.fail_on_harness_error:
        outcome = GateOutcome.HARNESS_FAILURE
    elif blocking:
        outcome = GateOutcome.REGRESSION
    else:
        outcome = GateOutcome.PASSED

    return GateResult(
        baseline_id=baseline.baseline_id,
        suite=baseline.suite,
        outcome=outcome,
        exit_code=EXIT_CODES[outcome],
        regressions=regressions,
        improvements=improvements,
        unchanged=unchanged,
        environment_mismatches=environment_mismatches,
        harness_failures=harness_failures,
    )
