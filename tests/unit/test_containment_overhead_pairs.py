"""The fixture pairs must be capable of measuring what they claim to measure.

Checked statically, because the failure mode is quiet. A pair whose scenario does
not apply to its targets produces no samples, and "no samples" surfaces in the
artifact as ``NOT_MEASURED`` — which is indistinguishable from the legitimate case
of a fixture with no contained twin. The benchmark would then report that
containment overhead was not measured, when in fact it was mis-configured.

That is exactly what happened: both HTTP pairs named ``baseline.tools_list``,
which is restricted to stdio, so every HTTP sample came back NOT_APPLICABLE and
the whole measurement was empty while looking merely unavailable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from interop_lab.benchmark.containment_overhead import (
    FIXTURE_PAIRS,
    OVERHEAD_SCENARIOS,
    FixturePair,
)
from interop_lab.containment.model import ExecutionMode
from interop_lab.models.core import TargetDefinition
from interop_lab.scenarios.library import get_scenario

REPO_ROOT = Path(__file__).resolve().parents[2]


def target(path: str) -> TargetDefinition:
    return TargetDefinition.model_validate(
        yaml.safe_load((REPO_ROOT / path).read_text(encoding="utf-8"))
    )


def ids(pair: FixturePair) -> str:
    return pair.label


@pytest.mark.parametrize("pair", FIXTURE_PAIRS, ids=ids)
def test_both_sides_of_a_pair_exist(pair: FixturePair) -> None:
    assert (REPO_ROOT / pair.host_target).is_file()
    assert (REPO_ROOT / pair.contained_target).is_file()


@pytest.mark.parametrize("pair", FIXTURE_PAIRS, ids=ids)
def test_a_pairs_scenario_applies_to_both_of_its_targets(pair: FixturePair) -> None:
    """The check that would have caught the stdio-only scenario on an HTTP pair."""
    scenario = get_scenario(pair.scenario)
    restricted = scenario.definition.applicability.transports

    for side in (pair.host_target, pair.contained_target):
        loaded = target(side)
        if restricted:
            assert loaded.transport in restricted, (
                f"{pair.label}: scenario {pair.scenario!r} is restricted to "
                f"{[t.value for t in restricted]} and {side} uses "
                f"{loaded.transport.value}, so this pair would measure nothing"
            )


@pytest.mark.parametrize("pair", FIXTURE_PAIRS, ids=ids)
def test_a_pair_that_cannot_be_compared_says_why(pair: FixturePair) -> None:
    """A gap with a reason, rather than a pair quietly deleted.

    Two of the three pairs here cannot yield a figure: the stdio fixture has no
    contained twin, and the contained 2025 fixture issues session identifiers its
    host twin does not. Both are listed anyway so the artifact reports the gap.
    """
    if pair.comparable:
        assert pair.not_comparable_reason is None
        return
    assert pair.not_comparable_reason
    assert len(pair.not_comparable_reason) > 40, "a one-word reason explains nothing"


@pytest.mark.parametrize("pair", [p for p in FIXTURE_PAIRS if p.comparable], ids=ids)
def test_both_sides_of_a_pair_run_the_same_program(pair: FixturePair) -> None:
    """Otherwise the difference measures the programs, not containment.

    Asserted on what actually executes — the launch command, the language, the
    transport, the version and the profile — rather than on ``implementation``,
    which is a human-readable description and legitimately carries a ", contained"
    suffix on one side. Comparing the prose would fail on a correct pair and would
    have to be relaxed, which is how a check stops checking anything.
    """
    host, contained = target(pair.host_target), target(pair.contained_target)

    assert host.launch_command == contained.launch_command
    # Environment is compared minus the keys that exist *because* one side is
    # contained: a listener inside a container has to bind an address the sidecar
    # can reach, which is part of being contained rather than a different program.
    # Everything else — the profile the fixture serves, its defect switches — must
    # be identical, and those are what would change the behaviour under test.
    containment_only = {"INTEROP_FIXTURE_BIND", "INTEROP_FIXTURE_PORT"}
    # Anything outside that set changing the fixture's behaviour makes the pair
    # non-comparable, and the pair must then declare a reason rather than yield a
    # figure — see test_a_pair_that_cannot_be_compared_says_why.
    assert {k: v for k, v in host.env.items() if k not in containment_only} == {
        k: v for k, v in contained.env.items() if k not in containment_only
    }
    assert host.language == contained.language
    assert host.version == contained.version
    assert host.transport == contained.transport
    assert host.protocol_profile == contained.protocol_profile


@pytest.mark.parametrize("pair", [p for p in FIXTURE_PAIRS if p.comparable], ids=ids)
def test_a_comparable_pair_differs_only_in_containment(pair: FixturePair) -> None:
    host, contained = target(pair.host_target), target(pair.contained_target)
    assert host.execution_mode is ExecutionMode.HOST
    assert contained.execution_mode is ExecutionMode.CONTAINER


@pytest.mark.parametrize("pair", FIXTURE_PAIRS, ids=ids)
def test_the_overhead_scenario_injects_no_faults_and_awaits_no_deadline(
    pair: FixturePair,
) -> None:
    """A scenario with its own timing would put that timing in the comparison."""
    scenario = get_scenario(pair.scenario)

    assert scenario.faults == []
    assert all(step.timeout_is_expected is False for step in scenario.steps)


def test_at_least_one_pair_can_actually_be_measured() -> None:
    """If none could, the artifact would report NOT_MEASURED for everything."""
    assert any(pair.comparable for pair in FIXTURE_PAIRS)


def test_the_recorded_scenario_list_matches_the_pairs() -> None:
    assert set(OVERHEAD_SCENARIOS) == {pair.scenario for pair in FIXTURE_PAIRS}
