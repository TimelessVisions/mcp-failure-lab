"""JUnit XML output.

One test case per target x scenario. The mapping is deliberate:

* ``failure`` — the target did something it should not have.
* ``error``   — the harness or environment could not produce a verdict.
* ``skipped`` — genuinely not applicable, with the reason recorded.

``NOT_APPLICABLE`` is never reported as success. A CI dashboard that shows a
skipped check as green is worse than no check at all.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from interop_lab.models.core import (
    FAILING,
    AssertionStatus,
    Classification,
    RunResult,
)
from interop_lab.naming import CLI_COMMAND

#: Classifications that could not yield a verdict about the target.
ERROR_CLASSIFICATIONS = frozenset({Classification.HARNESS_ERROR})

SKIPPED_CLASSIFICATIONS = frozenset({Classification.NOT_APPLICABLE})


def _properties(run: RunResult) -> ET.Element:
    element = ET.Element("properties")
    values = {
        "target": run.target_id,
        "protocol_profile": run.protocol_profile,
        "spec_version": run.spec_version,
        "transport": run.transport.value,
        "scenario_version": run.scenario_version,
        "classification": run.classification.value,
        "seed": str(run.seed),
        "replay_command": f"{CLI_COMMAND} replay <package>/repro.yaml",
    }
    if run.trace_path:
        values["evidence"] = run.trace_path
    if run.timeout_cause:
        values["timeout_cause"] = run.timeout_cause.value
    failed = [a.assertion_id for a in run.assertions if a.status is AssertionStatus.FAIL]
    if failed:
        values["failed_assertions"] = ",".join(failed)
    if run.resolved_target:
        values["target_version"] = run.resolved_target.version

    # The second dimension, alongside the classification rather than folded into
    # it. A dashboard reading only `classification` sees the outcome; one reading
    # these sees whether the outcome touches a security property and what anyone
    # has established about exploiting it. Neither substitutes for the other, so
    # both are emitted and the failure text below repeats the distinction in
    # words for a human skimming a failing case.
    dimensions = run.dimensions
    values["normative"] = "true" if dimensions.normative else "false"
    values["security_relevant"] = "true" if dimensions.security_relevant else "false"
    values["vulnerability_status"] = dimensions.vulnerability_status.value
    if dimensions.security_category is not None:
        values["security_category"] = dimensions.security_category.value
    if dimensions.resilience_relevant:
        values["resilience_relevant"] = "true"

    for name, value in values.items():
        ET.SubElement(element, "property", {"name": name, "value": value})
    return element


def _message(run: RunResult) -> str:
    failed = [a for a in run.assertions if a.status is AssertionStatus.FAIL]
    if not failed:
        return run.classification.value
    first = failed[0]
    return f"{run.classification.value}: expected {first.expected}, observed {first.actual}"


def _detail(run: RunResult) -> str:
    lines = [f"classification: {run.classification.value}"]
    for assertion in run.assertions:
        if assertion.status is not AssertionStatus.FAIL:
            continue
        lines.append(f"  assertion {assertion.assertion_id}")
        lines.append(f"    expected: {assertion.expected}")
        lines.append(f"    observed: {assertion.actual}")
        if assertion.normative_source:
            lines.append(f"    basis:    {assertion.normative_source}")
    dimensions = run.dimensions
    if dimensions.security_relevant:
        category = (
            dimensions.security_category.value
            if dimensions.security_category is not None
            else "uncategorised"
        )
        lines.append(
            f"security-relevant: {category} "
            f"(vulnerability status: {dimensions.vulnerability_status.value})"
        )
        lines.append(
            "  This is a security-relevant observation, not a vulnerability claim. "
            "The classification above is the conformance outcome; the two are "
            "separate and neither implies the other."
        )
        lines.extend(f"  limitation: {text}" for text in dimensions.security_limitations)
    if run.trace_path:
        lines.append(f"evidence: {run.trace_path}")
    return "\n".join(lines)


def build(runs: list[RunResult], *, suite_name: str) -> ET.ElementTree:
    failures = sum(1 for r in runs if r.classification in FAILING)
    errors = sum(1 for r in runs if r.classification in ERROR_CLASSIFICATIONS)
    skipped = sum(1 for r in runs if r.classification in SKIPPED_CLASSIFICATIONS)
    total_time = sum(r.duration_ms for r in runs) / 1000

    suites = ET.Element(
        "testsuites",
        {
            "name": suite_name,
            "tests": str(len(runs)),
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped),
            "time": f"{total_time:.3f}",
        },
    )
    suite = ET.SubElement(
        suites,
        "testsuite",
        {
            "name": suite_name,
            "tests": str(len(runs)),
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped),
            "time": f"{total_time:.3f}",
        },
    )

    for run in runs:
        case = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": run.target_id,
                "name": run.scenario_id,
                "time": f"{run.duration_ms / 1000:.3f}",
            },
        )
        case.append(_properties(run))

        if run.classification in ERROR_CLASSIFICATIONS:
            error = ET.SubElement(
                case,
                "error",
                {"type": run.classification.value, "message": run.harness_error or "harness error"},
            )
            error.text = run.harness_error or ""
        elif run.classification in SKIPPED_CLASSIFICATIONS:
            ET.SubElement(
                case,
                "skipped",
                {"message": run.not_applicable_reason or "not applicable"},
            )
        elif run.classification in FAILING:
            failure = ET.SubElement(
                case,
                "failure",
                {"type": run.classification.value, "message": _message(run)},
            )
            failure.text = _detail(run)

    ET.indent(suites, space="  ")
    return ET.ElementTree(suites)


def to_string(runs: list[RunResult], *, suite_name: str) -> str:
    from io import StringIO

    buffer = StringIO()
    build(runs, suite_name=suite_name).write(buffer, encoding="unicode", xml_declaration=True)
    return buffer.getvalue()
