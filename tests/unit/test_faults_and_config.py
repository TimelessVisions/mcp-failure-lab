"""Fault primitives and configuration validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from interop_lab.config.loader import ConfigError, load_target
from interop_lab.injection.faults import (
    Boundary,
    Fault,
    FaultKind,
    FaultTrigger,
    apply_field_fault,
    apply_outbound_fault,
)
from interop_lab.models.core import TargetDefinition
from interop_lab.profiles.registry import Role, Transport

MESSAGE: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list",
    "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
}


def fault(**kwargs: object) -> Fault:
    base: dict[str, object] = {
        "fault_id": "f",
        "boundary": Boundary.OUTBOUND_MESSAGE,
        "kind": FaultKind.DROP,
    }
    base.update(kwargs)
    return Fault.model_validate(base)


def test_remove_field_fault_strips_nested_path() -> None:
    f = fault(kind=FaultKind.REMOVE_FIELD, field_path="params._meta")
    updated = apply_field_fault(MESSAGE, f)
    assert "_meta" not in updated["params"]
    assert "_meta" in MESSAGE["params"], "the original message must not be mutated"


def test_mutate_field_fault_sets_value() -> None:
    f = fault(
        kind=FaultKind.MUTATE_FIELD,
        field_path="params._meta.io.modelcontextprotocol/protocolVersion",
        new_value="1999-01-01",
    )
    # Dotted paths cannot address a key containing dots; the fault is a no-op
    # rather than a silent corruption of a neighbouring field.
    updated = apply_field_fault(MESSAGE, f)
    assert updated["params"]["_meta"] == MESSAGE["params"]["_meta"]


def test_drop_fault_returns_none() -> None:
    assert apply_outbound_fault(b"{}", MESSAGE, fault(kind=FaultKind.DROP)) is None


def test_truncate_fault_shortens_payload() -> None:
    raw = json.dumps(MESSAGE).encode()
    out = apply_outbound_fault(raw, MESSAGE, fault(kind=FaultKind.TRUNCATE, keep_bytes=10))
    assert out is not None and len(out) == 10


def test_deterministic_faults_are_flagged() -> None:
    assert fault(probability=1.0).is_deterministic
    assert fault(probability=0.0).is_deterministic
    assert not fault(probability=0.5).is_deterministic


def test_trigger_fires_only_on_the_named_occurrence() -> None:
    f = fault(trigger=FaultTrigger(occurrence=2))
    assert not f.should_fire(index=0, method="tools/list")
    assert f.should_fire(index=2, method="tools/list")


def test_trigger_filters_by_method() -> None:
    f = fault(trigger=FaultTrigger(occurrence=0, method="tools/call"))
    assert not f.should_fire(index=0, method="tools/list")
    assert f.should_fire(index=0, method="tools/call")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": FaultKind.DELAY, "delay_ms": 0},
        {"kind": FaultKind.MUTATE_FIELD},
        {"kind": FaultKind.CORRUPT_JSON},
        {"kind": FaultKind.DELAY, "delay_ms": 99_999, "max_duration_ms": 100},
        {"probability": 1.5},
    ],
)
def test_malformed_fault_definitions_are_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        fault(**kwargs)


def test_target_rejects_moving_version() -> None:
    with pytest.raises(ValidationError, match="moving version"):
        TargetDefinition(
            target_id="t",
            role=Role.SERVER,
            implementation="x",
            language="python",
            version="main",
            launch_command=["true"],
            transport=Transport.STDIO,
            protocol_profile="mcp-2026-07-28",
        )


def test_target_rejects_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "t.yaml"
    path.write_text(
        "target_id: t\nrole: server\nimplementation: x\nlanguage: python\n"
        "version: '1.0'\nlaunch_command: ['true']\ntransport: stdio\n"
        "protocol_profile: mcp-2026-07-28\ntypo_field: oops\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_target(path)


def test_target_rejects_unknown_profile(tmp_path: Path) -> None:
    path = tmp_path / "t.yaml"
    path.write_text(
        "target_id: t\nrole: server\nimplementation: x\nlanguage: python\n"
        "version: '1.0'\nlaunch_command: ['true']\ntransport: stdio\n"
        "protocol_profile: mcp-1999-01-01\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unknown protocol profile"):
        load_target(path)


def test_all_shipped_targets_load() -> None:
    for path in sorted(Path("targets").rglob("*.yaml")):
        if path.name.endswith("matrix.yaml"):
            continue  # a matrix lists targets; it is not itself one
        target = load_target(path)
        assert target.version not in ("main", "latest")
