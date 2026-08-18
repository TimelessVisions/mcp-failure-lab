"""Classification ordering and the distinctions the vocabulary exists to keep."""

from __future__ import annotations

from interop_lab.models.core import (
    AssertionResult,
    AssertionStatus,
    Classification,
    TimeoutCause,
)
from interop_lab.runner.executor import classify


def failing(assertion_id: str, classification: Classification) -> AssertionResult:
    return AssertionResult(
        assertion_id=assertion_id,
        status=AssertionStatus.FAIL,
        expected="e",
        actual="a",
        classification=classification,
    )


def passing(assertion_id: str = "ok") -> AssertionResult:
    return AssertionResult(
        assertion_id=assertion_id, status=AssertionStatus.PASS, expected="e", actual="e"
    )


def base(**overrides: object) -> tuple[Classification, TimeoutCause | None]:
    kwargs: dict[str, object] = {
        "timed_out": False,
        "timeout_cause": None,
        "process_alive_at_timeout": False,
        "exit_code": 0,
        "killed_by_harness": False,
        "cleanup_ok": True,
        "expected_timeout": False,
    }
    kwargs.update(overrides)
    assertions = kwargs.pop("assertions", [passing()])
    return classify(assertions, **kwargs)  # type: ignore[arg-type]


def test_all_passing_is_pass() -> None:
    assert base()[0] is Classification.PASS


def test_no_assertions_is_inconclusive_not_pass() -> None:
    assert base(assertions=[])[0] is Classification.INCONCLUSIVE


def test_crash_outranks_assertion_failure() -> None:
    """A non-zero exit explains a missing response; report the crash, not a violation."""
    result, _ = base(
        assertions=[failing("x", Classification.SPEC_VIOLATION)],
        exit_code=1,
        killed_by_harness=False,
    )
    assert result is Classification.CRASH


def test_harness_kill_is_not_a_crash() -> None:
    result, _ = base(exit_code=-15, killed_by_harness=True)
    assert result is Classification.PASS


def test_cleanup_failure_outranks_everything() -> None:
    result, _ = base(cleanup_ok=False, exit_code=1)
    assert result is Classification.CLEANUP_FAILURE


def test_silent_but_alive_is_hang_not_timeout() -> None:
    result, cause = base(timed_out=True, process_alive_at_timeout=True)
    assert result is Classification.HANG
    assert cause is TimeoutCause.TARGET_NO_RESPONSE


def test_dead_target_is_timeout_not_hang() -> None:
    result, _ = base(timed_out=True, process_alive_at_timeout=False)
    assert result is Classification.TIMEOUT


def test_expected_timeout_does_not_fail_the_run() -> None:
    result, _ = base(expected_timeout=True, timed_out=False)
    assert result is Classification.PASS


def test_assertion_supplies_its_own_classification() -> None:
    result, _ = base(assertions=[failing("recover", Classification.RECOVERY_FAILURE)])
    assert result is Classification.RECOVERY_FAILURE


def test_behavioral_divergence_is_not_counted_as_a_failure() -> None:
    """A divergence is an observation, not a defect; CI must not treat it as one."""
    assert Classification.BEHAVIORAL_DIVERGENCE not in _failing_set()
    assert Classification.EXPECTED_DIFFERENCE not in _failing_set()
    assert Classification.NOT_APPLICABLE not in _failing_set()


def test_inconclusive_is_not_a_pass() -> None:
    result, _ = base(
        assertions=[
            AssertionResult(
                assertion_id="i",
                status=AssertionStatus.INCONCLUSIVE,
                expected="e",
                actual="unknown",
            )
        ]
    )
    assert result is Classification.INCONCLUSIVE


def _failing_set() -> frozenset[Classification]:
    from interop_lab.models.core import FAILING

    return FAILING
