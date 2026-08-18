"""Deterministic fault primitives applied at explicit boundaries.

Release-gate scenarios must not depend on uncontrolled randomness. Every fault
carries a trigger, a seed and a bounded effect; probability defaults to a
deterministic 0 or 1.
"""

from __future__ import annotations

import json
import random
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Boundary(StrEnum):
    #: Bytes the harness is about to send to the target.
    OUTBOUND_MESSAGE = "outbound_message"
    #: Bytes the harness has just received from the target.
    INBOUND_MESSAGE = "inbound_message"
    #: The target process itself.
    PROCESS = "process"


class FaultKind(StrEnum):
    DELAY = "delay"
    DROP = "drop"
    DUPLICATE = "duplicate"
    TRUNCATE = "truncate"
    MUTATE_FIELD = "mutate_field"
    REMOVE_FIELD = "remove_field"
    CORRUPT_JSON = "corrupt_json"
    TERMINATE_PROCESS = "terminate_process"


class FaultTrigger(BaseModel):
    """When the fault fires. Defaults to the first matching message."""

    model_config = ConfigDict(extra="forbid")

    #: Fire on the nth matching message at the boundary, zero-indexed.
    occurrence: int = 0
    #: Restrict to a JSON-RPC method name.
    method: str | None = None

    def matches(self, *, index: int, method: str | None) -> bool:
        if self.method is not None and method != self.method:
            return False
        return index == self.occurrence


class Fault(BaseModel):
    """A single bounded, deterministic mutation."""

    model_config = ConfigDict(extra="forbid")

    fault_id: str
    boundary: Boundary
    kind: FaultKind
    trigger: FaultTrigger = Field(default_factory=FaultTrigger)
    #: 1.0 = always fire, 0.0 = never. Anything else requires a seed and is
    #: rejected for release-gate suites by the runner.
    probability: float = 1.0
    seed: int = 0

    #: DELAY
    delay_ms: int = 0
    #: TRUNCATE — keep this many bytes.
    keep_bytes: int = 0
    #: MUTATE_FIELD / REMOVE_FIELD — dotted path within the JSON-RPC message.
    field_path: str | None = None
    new_value: Any = None
    #: CORRUPT_JSON — literal replacement payload.
    replacement: str | None = None
    #: TERMINATE_PROCESS
    signal_name: str = "SIGKILL"

    max_duration_ms: int = 30_000

    @model_validator(mode="after")
    def _check_shape(self) -> Fault:
        if self.kind is FaultKind.DELAY and self.delay_ms <= 0:
            raise ValueError(f"{self.fault_id}: DELAY requires delay_ms > 0")
        if self.kind is FaultKind.TRUNCATE and self.keep_bytes < 0:
            raise ValueError(f"{self.fault_id}: TRUNCATE requires keep_bytes >= 0")
        if self.kind in (FaultKind.MUTATE_FIELD, FaultKind.REMOVE_FIELD) and not self.field_path:
            raise ValueError(f"{self.fault_id}: {self.kind} requires field_path")
        if self.kind is FaultKind.CORRUPT_JSON and self.replacement is None:
            raise ValueError(f"{self.fault_id}: CORRUPT_JSON requires replacement")
        if self.kind is FaultKind.DELAY and self.delay_ms > self.max_duration_ms:
            raise ValueError(f"{self.fault_id}: delay_ms exceeds max_duration_ms")
        if not 0.0 <= self.probability <= 1.0:
            raise ValueError(f"{self.fault_id}: probability must be within [0, 1]")
        return self

    @property
    def is_deterministic(self) -> bool:
        return self.probability in (0.0, 1.0)

    def should_fire(self, *, index: int, method: str | None) -> bool:
        if not self.trigger.matches(index=index, method=method):
            return False
        if self.probability >= 1.0:
            return True
        if self.probability <= 0.0:
            return False
        return random.Random(self.seed + index).random() < self.probability  # noqa: S311


def _split_path(path: str) -> list[str]:
    return [part for part in path.split(".") if part]


def apply_field_fault(message: dict[str, Any], fault: Fault) -> dict[str, Any]:
    """Return a copy of ``message`` with a field mutated or removed."""
    assert fault.field_path is not None
    parts = _split_path(fault.field_path)
    updated = json.loads(json.dumps(message))
    cursor: Any = updated
    for part in parts[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            return updated  # Path absent: nothing to mutate, recorded by the caller.
        cursor = cursor[part]
    leaf = parts[-1]
    if not isinstance(cursor, dict):
        return updated
    if fault.kind is FaultKind.REMOVE_FIELD:
        cursor.pop(leaf, None)
    else:
        cursor[leaf] = fault.new_value
    return updated


def apply_outbound_fault(raw: bytes, message: dict[str, Any], fault: Fault) -> bytes | None:
    """Apply a byte-level or field-level fault to an outbound payload.

    Returns the replacement bytes, or ``None`` when the message should be dropped.
    """
    match fault.kind:
        case FaultKind.DROP:
            return None
        case FaultKind.TRUNCATE:
            return raw[: fault.keep_bytes]
        case FaultKind.CORRUPT_JSON:
            assert fault.replacement is not None
            return fault.replacement.encode()
        case FaultKind.MUTATE_FIELD | FaultKind.REMOVE_FIELD:
            return json.dumps(apply_field_fault(message, fault)).encode()
        case _:
            return raw
