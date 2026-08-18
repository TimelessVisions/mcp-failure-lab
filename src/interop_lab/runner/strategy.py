"""How a scenario is executed, as an explicit choice rather than a transport detail.

There are two genuinely different execution shapes here, and the earlier design
had only one abstraction for both:

* **Interactive.** The harness holds a live session, sends one message, waits for
  the answer, decides what to do next. stdio works this way, and so does host
  Streamable HTTP. ``TransportSession`` describes it well.
* **Contained plan.** The harness never holds a socket at all. It compiles the
  whole scenario into one plan, hands it to a probe inside a container, and
  interprets the recorded result afterwards. There is no ``send`` and no
  ``receive`` on the host side, because the host is not on the wire.

Forcing the second shape through ``TransportSession`` would mean implementing
``send``/``receive``/``request`` as methods that do not send, receive or request
— they would queue work for a batch that has not run yet. Every caller would
then be reading an interface whose method names describe something the
implementation does not do. The abstraction would be satisfied and the code
would be a lie.

So the boundary is drawn one level up: a strategy takes a scenario and a target
and returns evidence. ``ScenarioExecutor.run()`` is unchanged in shape — it
still selects, executes, classifies and persists — and both strategies feed the
same assertion evaluation and the same ``classify`` function, because a second
classification path is how two answers to the same question appear.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from interop_lab.containment.model import ContainmentRecord, ExecutionMode, NetworkMode
from interop_lab.events.model import WireTrace
from interop_lab.models.core import (
    AssertionResult,
    ProcessOutcome,
    TargetDefinition,
    TimeoutCause,
)
from interop_lab.profiles.registry import ProtocolProfile, Transport
from interop_lab.scenarios.model import Scenario

if TYPE_CHECKING:
    from interop_lab.runner.images import ExecutionContext


class StrategySelectionError(Exception):
    """No strategy implements this combination, and none will be substituted.

    Raised rather than defaulting to anything. A silent fallback is the specific
    failure this module exists to prevent: a contained HTTP target quietly
    executed by the host session would write `contained` into an artifact
    describing a process that talked to the target over an ordinary local
    socket.
    """


@dataclass
class ExecutionOutcome:
    """Everything a strategy observed, in the form the executor grades.

    Deliberately evidence, not verdicts. No strategy decides whether a target
    passed; each one reports what happened and ``classify`` reduces it, so the
    failure vocabulary stays in a single place.
    """

    trace: WireTrace
    process: ProcessOutcome
    assertions: list[AssertionResult] = field(default_factory=list)
    faults_applied: list[str] = field(default_factory=list)
    containment: ContainmentRecord | None = None

    timed_out: bool = False
    timeout_cause: TimeoutCause | None = None
    process_alive_at_timeout: bool = False
    expected_timeout: bool = False
    harness_error: str | None = None

    #: Strategy-specific evidence persisted next to the trace: the resolved probe
    #: plan, the probe's identity, the observed network. Kept out of the typed
    #: models because it is genuinely strategy-shaped, and dropping it would lose
    #: the record of how the evidence was obtained.
    extra_artifacts: dict[str, object] = field(default_factory=dict)


class ScenarioExecutionStrategy(Protocol):
    """One way of turning a scenario plus a target into evidence."""

    #: Stable identifier recorded in the run artifact, so a reader can tell
    #: which execution shape produced the evidence they are looking at.
    name: str

    async def execute(
        self,
        scenario: Scenario,
        target: TargetDefinition,
        *,
        run_id: str,
        profile: ProtocolProfile,
        context: ExecutionContext,
    ) -> ExecutionOutcome: ...


def select_strategy(target: TargetDefinition) -> ScenarioExecutionStrategy:
    """Choose the execution strategy for a target. Never guesses.

    The routing is exhaustive over the combinations that exist, and every
    combination that has no implementation raises. In particular a contained
    Streamable HTTP target can only reach the sidecar path: there is no branch
    that would let it reach ``StreamableHttpSession`` on the host.
    """
    # Imported here rather than at module scope: the strategies import the
    # executor's helpers, and the executor imports this module.
    from interop_lab.runner.contained_http import ContainedHttpPlanStrategy
    from interop_lab.runner.interactive import InteractiveSessionStrategy

    match (target.transport, target.execution_mode):
        case (Transport.STDIO, _):
            # Host and contained stdio are both interactive: the contained form
            # attaches to the container's stdin and stdout, so the harness still
            # holds a live session and the shape is unchanged.
            return InteractiveSessionStrategy()

        case (Transport.STREAMABLE_HTTP, ExecutionMode.HOST):
            return InteractiveSessionStrategy()

        case (Transport.STREAMABLE_HTTP, ExecutionMode.CONTAINER):
            spec = target.container
            if spec is None:  # pragma: no cover - the model rejects this at load
                raise StrategySelectionError(
                    f"target {target.target_id!r} requests container execution without a spec"
                )
            if spec.network is NetworkMode.ISOLATED_SIDECAR:
                return ContainedHttpPlanStrategy()
            raise StrategySelectionError(
                f"target {target.target_id!r} is a contained Streamable HTTP target on "
                f"network={spec.network.value}. Only isolated_sidecar is implemented, and "
                "the harness will not fall back to opening a host socket to a contained "
                "target: the artifact would claim an isolation the run did not have."
            )

        case _:  # pragma: no cover - both enums are exhaustively matched above
            raise StrategySelectionError(
                f"no execution strategy for transport={target.transport.value} "
                f"mode={target.execution_mode.value}"
            )
