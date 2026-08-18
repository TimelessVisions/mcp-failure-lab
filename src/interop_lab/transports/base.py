"""The contract every transport session satisfies.

Deliberately narrow. It covers only what the executor, the minimiser and replay
genuinely need, so transport-specific behaviour is not forced into a false
common abstraction: HTTP status codes and stdio exit codes stay on their own
sessions and reach artifacts through ``WireEvent.headers`` and ``note``.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Protocol, Self, runtime_checkable

from interop_lab.events.model import WireEvent, WireTrace
from interop_lab.models.core import ProcessOutcome, TimeoutCause


class TransportTimeoutError(Exception):
    """No response arrived before the deadline.

    ``cause`` records *whose* deadline expired, which is the difference between
    a target defect and a harness artefact.
    """

    def __init__(self, cause: TimeoutCause, detail: str) -> None:
        super().__init__(detail)
        self.cause = cause
        self.detail = detail


@runtime_checkable
class TransportSession(Protocol):
    """A launched target plus the trace of everything that crossed the wire."""

    trace: WireTrace
    outcome: ProcessOutcome
    faults_applied: list[str]

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    def begin_scenario(self) -> None:
        """Arm faults and reset occurrence counting after setup traffic."""
        ...

    def next_rpc_id(self) -> int: ...

    async def send(self, message: dict[str, Any]) -> None: ...

    async def receive(self, *, timeout_s: float) -> WireEvent: ...

    async def request(
        self, message: dict[str, Any], *, timeout_s: float, expect_id: str | int | None = None
    ) -> WireEvent: ...

    @property
    def supports_raw(self) -> bool:
        """Whether the transport can emit bytes that are not a valid message."""
        ...

    async def send_raw(self, payload: bytes) -> None: ...
