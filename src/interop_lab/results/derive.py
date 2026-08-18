"""Deriving a run's secondary dimensions from what it actually observed.

The primary classification comes from :func:`interop_lab.runner.executor.classify`
and answers "what did the target do". This answers the orthogonal questions:
does the result speak to a dated requirement, does it touch a security property,
and what has anyone established about exploiting it.

Two rules carry the design, and both exist to stop the harness from manufacturing
security findings out of its own uncertainty:

* Only a **decided** result can be a security-relevant observation. A
  HARNESS_ERROR, an INCONCLUSIVE, or a run whose containment was not obtained
  says nothing about the target, so it cannot say anything about the target's
  security either.
* ``vulnerability_status`` never rises above ``unconfirmed`` here. Establishing
  practical exploitation means reasoning about deployment, defaults and reachable
  paths — work a person does and records in a finding package. No execution path
  writes ``confirmed_private``.
"""

from __future__ import annotations

from interop_lab.models.core import (
    DEFAULT_SECURITY_LIMITATIONS,
    AssertionResult,
    AssertionStatus,
    Classification,
    ResultDimensions,
    ScenarioNature,
    SecurityCategory,
    VulnerabilityStatus,
)
from interop_lab.scenarios.model import Scenario

#: Classifications that decided nothing about the target.
#:
#: ``NOT_APPLICABLE`` belongs here for a different reason than the rest: the
#: scenario never ran, so there is no observation to be relevant to anything.
#: ``INCONCLUSIVE`` and ``HARNESS_ERROR`` belong because the harness could not
#: reach a verdict, and a security dimension attached to "we do not know" would
#: be a finding invented out of a missing measurement.
UNDECIDED = frozenset(
    {
        Classification.NOT_APPLICABLE,
        Classification.INCONCLUSIVE,
        Classification.HARNESS_ERROR,
    }
)


def _security_category(
    scenario: Scenario, failed: list[AssertionResult]
) -> SecurityCategory | None:
    """The most specific category available: the assertion's, then the scenario's.

    An assertion-level category wins because a scenario can hold one assertion
    about a security property and several about message shape, and attributing
    the whole scenario's category to a shape failure would file the result under
    a property it does not touch.
    """
    for assertion in failed:
        if assertion.security_relevant and assertion.security_category is not None:
            return assertion.security_category
    return scenario.definition.security_category


def _security_limitations(scenario: Scenario, failed: list[AssertionResult]) -> list[str]:
    specs = {spec.assertion_id: spec for step in scenario.steps for spec in step.assertions}
    for assertion in failed:
        spec = specs.get(assertion.assertion_id)
        if spec is not None and spec.security_relevant and spec.security_limitations:
            return list(spec.security_limitations)
    if scenario.definition.security_limitations:
        return list(scenario.definition.security_limitations)
    return list(DEFAULT_SECURITY_LIMITATIONS)


def derive_dimensions(
    scenario: Scenario,
    *,
    classification: Classification,
    assertions: list[AssertionResult],
    usable_evidence: bool = True,
) -> ResultDimensions:
    """Compute the secondary dimensions of one executed run.

    ``usable_evidence`` is the containment question. A run whose requested
    isolation was not obtained did not execute what it claims to have executed,
    so it cannot contribute a security observation any more than it can
    contribute a conformance verdict.
    """
    failed = [a for a in assertions if a.status is AssertionStatus.FAIL]

    # "This result speaks to a dated requirement." True whether the assertion
    # passed or failed: a conformance pass is a normative result too, and the
    # benchmark's normative denominator needs both halves.
    normative = any(a.normative_source for a in assertions) or (
        scenario.definition.nature is ScenarioNature.NORMATIVE
        and scenario.definition.normative_source is not None
    )

    decided = classification not in UNDECIDED and usable_evidence
    non_pass = classification is not Classification.PASS

    declared_by_assertion = any(a.security_relevant for a in failed)
    declared_by_scenario = scenario.definition.security_category is not None
    security_relevant = decided and non_pass and (declared_by_assertion or declared_by_scenario)

    category = _security_category(scenario, failed) if security_relevant else None
    return ResultDimensions(
        normative=normative,
        security_relevant=security_relevant,
        security_category=category,
        resilience_relevant=scenario.definition.nature is ScenarioNature.RESILIENCE,
        severity=scenario.definition.severity,
        # Observed, never worked up. A person moves this forward in a finding
        # package; the execution path has no standing to.
        vulnerability_status=(
            VulnerabilityStatus.UNCONFIRMED
            if security_relevant
            else VulnerabilityStatus.NOT_ASSESSED
        ),
        security_limitations=(_security_limitations(scenario, failed) if security_relevant else []),
    )
