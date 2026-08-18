"""Which execution strategy runs a target, and what must never happen.

The single most damaging failure this layer can have is a *silent* one: a
contained HTTP target executed by the host session. Nothing would look wrong —
the run would pass, the artifact would say `contained`, and the target would
have been reached over an ordinary local socket. So the routing is asserted
explicitly, in both directions: the right strategy for each combination, and a
hard failure for every combination that has no implementation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from interop_lab.containment.model import ExecutionMode, NetworkMode
from interop_lab.models.core import TargetDefinition
from interop_lab.runner.contained_http import ContainedHttpPlanStrategy
from interop_lab.runner.interactive import InteractiveSessionStrategy
from interop_lab.runner.strategy import StrategySelectionError, select_strategy

CONTAINED_HTTP_2026 = "targets/http/fixture-2026-contained.yaml"
CONTAINED_HTTP_2025 = "targets/http/fixture-2025-contained.yaml"
HOST_HTTP = "targets/http/fixture-2026.yaml"
HOST_STDIO = "targets/fixtures/reference.yaml"
CONTAINED_STDIO = "targets/fixtures/reference-contained.yaml"


def load(path: str) -> TargetDefinition:
    return TargetDefinition.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (HOST_STDIO, InteractiveSessionStrategy),
        (CONTAINED_STDIO, InteractiveSessionStrategy),
        (HOST_HTTP, InteractiveSessionStrategy),
        (CONTAINED_HTTP_2026, ContainedHttpPlanStrategy),
        (CONTAINED_HTTP_2025, ContainedHttpPlanStrategy),
    ],
)
def test_each_committed_target_routes_where_it_should(path: str, expected: type) -> None:
    assert isinstance(select_strategy(load(path)), expected)


@pytest.mark.parametrize("path", [CONTAINED_HTTP_2026, CONTAINED_HTTP_2025])
def test_a_contained_http_target_never_reaches_the_host_session(path: str) -> None:
    """The property the whole strategy boundary exists to guarantee.

    `StreamableHttpSession` opens a socket from the host to the target. A
    contained target has no host-reachable port, so a run that got there would
    either fail confusingly or — worse, if a port were ever published — succeed
    while recording isolation it did not have.
    """
    strategy = select_strategy(load(path))
    assert not isinstance(strategy, InteractiveSessionStrategy)
    assert isinstance(strategy, ContainedHttpPlanStrategy)


def test_a_contained_http_target_on_an_unimplemented_network_is_refused() -> None:
    """No silent fallback: an unimplemented combination fails, it does not degrade."""
    target = load(CONTAINED_HTTP_2026)
    assert target.container is not None
    weakened = target.model_copy(
        update={
            "container": target.container.model_copy(
                update={"network": NetworkMode.PUBLISHED_LOOPBACK}
            )
        }
    )

    with pytest.raises(StrategySelectionError) as caught:
        select_strategy(weakened)
    message = str(caught.value)
    assert "isolated_sidecar" in message
    assert "will not fall back" in message


def test_the_selection_never_returns_none_for_a_valid_target() -> None:
    """Every loadable target has a strategy or an explicit refusal, never a null."""
    for path in (HOST_STDIO, CONTAINED_STDIO, HOST_HTTP, CONTAINED_HTTP_2026):
        strategy = select_strategy(load(path))
        assert strategy is not None
        assert strategy.name


def test_the_strategy_name_reaches_the_artifact() -> None:
    """A reader must not have to infer the execution path from other fields."""
    assert InteractiveSessionStrategy().name == "interactive_session"
    assert ContainedHttpPlanStrategy().name == "contained_http_plan"


def test_a_contained_http_target_without_a_port_fails_to_load() -> None:
    """Caught in the model, before any strategy has a chance to guess at it."""
    raw: dict[str, Any] = yaml.safe_load(Path(CONTAINED_HTTP_2026).read_text(encoding="utf-8"))
    del raw["container"]["container_port"]
    with pytest.raises(ValidationError):
        TargetDefinition.model_validate(raw)


def test_host_http_targets_still_route_to_the_interactive_session() -> None:
    """The contained path must not have quietly captured the host path too."""
    target = load(HOST_HTTP)
    assert target.execution_mode is ExecutionMode.HOST
    assert target.container is None
    assert isinstance(select_strategy(target), InteractiveSessionStrategy)
