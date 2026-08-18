"""A normative outcome and a security reading are two facts, not one choice.

``security.http_invalid_origin`` is where the old single-enum design failed
visibly. Its assertion quotes a MUST, so a target that fails it has violated a
dated requirement — and the scenario reported ``SECURITY_RELEVANT``, a label that
says nothing about conformance. Whichever value was chosen, the other fact was
destroyed.

These tests pin the separation, and the boundaries that stop it becoming a
finding generator: an undecided result carries no security dimension, an
unexploited observation is never called a vulnerability, and neither dimension is
allowed to displace the other in a report.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from interop_lab.models.core import (
    AssertionResult,
    AssertionStatus,
    Classification,
    ResultDimensions,
    ScenarioCategory,
    ScenarioDefinition,
    ScenarioNature,
    SecurityCategory,
    Severity,
    VulnerabilityStatus,
)
from interop_lab.results.derive import derive_dimensions
from interop_lab.scenarios.library import get_scenario
from interop_lab.scenarios.model import AssertionKind, AssertionSpec, Scenario, Step, StepKind

ORIGIN = get_scenario("security.http_invalid_origin")


def failed(assertion_id: str = "forbidden", *, security: bool = True) -> AssertionResult:
    return AssertionResult(
        assertion_id=assertion_id,
        status=AssertionStatus.FAIL,
        expected="403",
        actual="200",
        normative_source="MCP transport binding: Servers MUST validate Origin",
        classification=Classification.SPEC_VIOLATION,
        security_relevant=security,
        security_category=SecurityCategory.ORIGIN_VALIDATION if security else None,
    )


def passed(assertion_id: str = "forbidden") -> AssertionResult:
    return AssertionResult(
        assertion_id=assertion_id,
        status=AssertionStatus.PASS,
        expected="403",
        actual="403",
        normative_source="MCP transport binding: Servers MUST validate Origin",
        security_relevant=True,
        security_category=SecurityCategory.ORIGIN_VALIDATION,
    )


# ------------------------------------------------- the invalid-Origin result


def test_a_failed_origin_must_is_a_spec_violation_that_is_also_security_relevant() -> None:
    """The case the previous vocabulary could not express."""
    dimensions = derive_dimensions(
        ORIGIN,
        classification=Classification.SPEC_VIOLATION,
        assertions=[failed()],
    )

    # The primary outcome is the conformance fact and is not replaced.
    assert dimensions.normative is True
    # The security reading survives alongside it.
    assert dimensions.security_relevant is True
    assert dimensions.security_category is SecurityCategory.ORIGIN_VALIDATION
    # And it is emphatically not a vulnerability claim.
    assert dimensions.vulnerability_status is VulnerabilityStatus.UNCONFIRMED
    assert dimensions.is_confirmed_vulnerability is False


def test_the_origin_assertion_no_longer_maps_its_failure_to_a_security_label() -> None:
    """The scenario library itself, not a reconstruction of it."""
    specs = [spec for step in ORIGIN.steps for spec in step.assertions]
    forbidden = next(spec for spec in specs if spec.assertion_id == "forbidden")

    assert forbidden.on_fail == Classification.SPEC_VIOLATION.value
    assert forbidden.security_relevant is True
    assert forbidden.normative_source is not None


def test_a_passing_origin_check_is_not_a_security_observation() -> None:
    """A pass is a pass. Counting it as security-relevant would inflate every tally."""
    dimensions = derive_dimensions(
        ORIGIN, classification=Classification.PASS, assertions=[passed()]
    )

    assert dimensions.security_relevant is False
    assert dimensions.vulnerability_status is VulnerabilityStatus.NOT_ASSESSED
    # Still a normative result: the requirement was checked and met.
    assert dimensions.normative is True


# ---------------------------------------------- the harness's own uncertainty


@pytest.mark.parametrize(
    "classification",
    [Classification.INCONCLUSIVE, Classification.HARNESS_ERROR, Classification.NOT_APPLICABLE],
)
def test_an_undecided_result_carries_no_security_dimension(
    classification: Classification,
) -> None:
    """ "We could not tell" is not a security finding about anyone's server."""
    dimensions = derive_dimensions(ORIGIN, classification=classification, assertions=[])

    assert dimensions.security_relevant is False
    assert dimensions.vulnerability_status is VulnerabilityStatus.NOT_ASSESSED


def test_a_run_without_usable_containment_produces_no_security_observation() -> None:
    """The harness did not execute what it claims to have executed."""
    dimensions = derive_dimensions(
        ORIGIN,
        classification=Classification.SPEC_VIOLATION,
        assertions=[failed()],
        usable_evidence=False,
    )

    assert dimensions.security_relevant is False


# ------------------------------------------------------------- model invariants


def test_a_security_relevant_result_must_state_what_it_does_not_establish() -> None:
    with pytest.raises(ValidationError, match="records no limitations"):
        ResultDimensions(
            security_relevant=True,
            security_category=SecurityCategory.ORIGIN_VALIDATION,
        )


def test_a_security_relevant_result_must_name_its_category() -> None:
    with pytest.raises(ValidationError, match="no security category"):
        ResultDimensions(security_relevant=True, security_limitations=["x"])


def test_a_vulnerability_status_needs_a_security_relevant_subject() -> None:
    with pytest.raises(ValidationError, match="has no subject"):
        ResultDimensions(vulnerability_status=VulnerabilityStatus.CONFIRMED_PRIVATE)


def test_only_an_established_status_counts_as_a_confirmed_vulnerability() -> None:
    unconfirmed = ResultDimensions(
        security_relevant=True,
        security_category=SecurityCategory.ORIGIN_VALIDATION,
        security_limitations=["not established"],
        vulnerability_status=VulnerabilityStatus.UNCONFIRMED,
    )
    confirmed = unconfirmed.model_copy(
        update={"vulnerability_status": VulnerabilityStatus.CONFIRMED_PRIVATE}
    )

    assert unconfirmed.is_confirmed_vulnerability is False
    assert confirmed.is_confirmed_vulnerability is True


def test_rewording_a_limitation_does_not_change_the_digest_material() -> None:
    """Prose is for a reader. A reworded caveat must not read as a changed result."""
    base = ResultDimensions(
        security_relevant=True,
        security_category=SecurityCategory.ORIGIN_VALIDATION,
        security_limitations=["one phrasing"],
    )
    reworded = base.model_copy(update={"security_limitations": ["another phrasing entirely"]})

    assert base.digest_material == reworded.digest_material


# ------------------------------------------------------- refusing the collapse


def test_a_normative_assertion_may_not_map_its_failure_to_a_security_label() -> None:
    """The exact mistake the invalid-Origin scenario used to make."""
    with pytest.raises(ValidationError, match="is a SPEC_VIOLATION"):
        AssertionSpec(
            assertion_id="forbidden",
            kind=AssertionKind.HTTP_STATUS_IN,
            expected_statuses=[403],
            normative_source="MCP transport binding: Servers MUST validate Origin",
            on_fail="SECURITY_RELEVANT",
        )


def test_a_security_category_without_security_relevance_is_refused() -> None:
    with pytest.raises(ValidationError, match="One of the two is wrong"):
        AssertionSpec(
            assertion_id="x",
            kind=AssertionKind.RESPONSE_IS_RESULT,
            security_category=SecurityCategory.ORIGIN_VALIDATION,
        )


def test_a_security_relevant_scenario_must_name_the_property_it_is_about() -> None:
    with pytest.raises(ValidationError, match="no security_category"):
        ScenarioDefinition(
            scenario_id="x.y",
            title="t",
            category=ScenarioCategory.HTTP_AUTHZ,
            nature=ScenarioNature.SECURITY_RELEVANT,
            purpose="p",
        )


# ------------------------------------------- category precision within a scenario


def test_a_shape_failure_is_not_filed_under_the_scenarios_security_property() -> None:
    """One scenario may hold a security assertion and several ordinary ones.

    The category has to follow the assertion that actually failed, or a
    malformed-response failure gets counted as an Origin-validation observation.
    """
    scenario = Scenario(
        definition=ScenarioDefinition(
            scenario_id="mixed.scenario",
            title="mixed",
            category=ScenarioCategory.HTTP_AUTHZ,
            nature=ScenarioNature.NORMATIVE,
            purpose="p",
            severity=Severity.MEDIUM,
        ),
        steps=[
            Step(
                step_id="s",
                kind=StepKind.REQUEST,
                method="tools/list",
                assertions=[
                    AssertionSpec(
                        assertion_id="shape",
                        kind=AssertionKind.RESPONSE_IS_RESULT,
                    ),
                    AssertionSpec(
                        assertion_id="origin",
                        kind=AssertionKind.HTTP_STATUS_IN,
                        expected_statuses=[403],
                        security_relevant=True,
                        security_category=SecurityCategory.ORIGIN_VALIDATION,
                        security_limitations=["one exchange"],
                    ),
                ],
            )
        ],
    )

    shape_only = derive_dimensions(
        scenario,
        classification=Classification.SPEC_VIOLATION,
        assertions=[failed("shape", security=False)],
    )
    assert shape_only.security_relevant is False

    origin_failed = derive_dimensions(
        scenario,
        classification=Classification.SPEC_VIOLATION,
        assertions=[failed("origin")],
    )
    assert origin_failed.security_relevant is True
    assert origin_failed.security_category is SecurityCategory.ORIGIN_VALIDATION
    assert origin_failed.security_limitations == ["one exchange"]
