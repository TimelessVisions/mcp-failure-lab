"""The dated conformance baseline must be the official frozen requirement set.

``--spec-version`` answers a different question from ``--requirements``: it
filters the suite as it exists today. The official CLI rejects combining them.
An unscoped run that passes ``--spec-version`` and then records
``requirements: <date>`` reports a requirement-set result it did not execute.

A single ``--scenario`` run is a wire-scoped probe. The official CLI also
rejects combining ``--requirements`` with ``--scenario``, so that probe must
not be stored as a frozen requirement-set pass.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from interop_lab.config.loader import load_target
from interop_lab.conformance.adapter import ConformanceAdapter
from interop_lab.conformance.models import ConformanceOutcome, ConformanceRun, ConformanceSelector

TARGET = load_target("targets/http/fixture-2026.yaml")


class _FakeProcess:
    def __init__(
        self, command: tuple[str, ...], *, returncode: int, checks: list[dict[str, Any]]
    ) -> None:
        self.returncode = returncode
        self._command = command
        self._checks = checks

    async def communicate(self) -> tuple[bytes, bytes]:
        output = Path(self._command[self._command.index("--output-dir") + 1])
        scenario_dir = output / "server-captured"
        scenario_dir.mkdir(parents=True, exist_ok=True)
        (scenario_dir / "checks.json").write_text(json.dumps(self._checks), encoding="utf-8")
        return b"", b""


def _patch_cli(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int,
    checks: list[dict[str, Any]],
) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*command: str, **kwargs: object) -> _FakeProcess:
        del kwargs
        calls.append(command)
        return _FakeProcess(command, returncode=returncode, checks=checks)

    monkeypatch.setattr(
        "interop_lab.conformance.adapter.asyncio.create_subprocess_exec",
        fake_exec,
    )
    monkeypatch.setattr("interop_lab.conformance.adapter.is_available", lambda: True)
    return calls


def _selector(run: object) -> object:
    selector = getattr(run, "selector", None)
    return getattr(selector, "value", selector)


def _success(check_id: str) -> dict[str, Any]:
    return {"id": check_id, "name": check_id, "status": "SUCCESS", "description": "d"}


def _failure(check_id: str) -> dict[str, Any]:
    return {"id": check_id, "name": check_id, "status": "FAILURE", "description": "d"}


@pytest.mark.asyncio
async def test_unscoped_run_invokes_frozen_requirements_not_spec_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Negative control: a passing check must not launder a non-zero official exit.

    The official server CLI's exit code is the scored verdict. Not-scored
    extension failures do not flip it, and a zero count of FAILURE checks is
    not a substitute for that verdict.
    """
    calls = _patch_cli(
        monkeypatch,
        returncode=1,
        checks=[_success("tools-list")],
    )
    run = await ConformanceAdapter(output_root=tmp_path).run_server(
        target=TARGET,
        url="http://127.0.0.1:9/mcp",
    )

    command = calls[0]
    assert "--requirements" in command
    assert "2026-07-28" in command
    assert "--spec-version" not in command
    assert "--scenario" not in command
    assert "--suite" not in command
    assert _selector(run) == "requirements"
    assert run.outcome is ConformanceOutcome.OFFICIAL_CONFORMANCE_FAILURE
    assert run.exit_code == 1


@pytest.mark.asyncio
async def test_requirements_exit_zero_is_a_pass_even_when_an_unscored_check_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The official CLI exits 0 when only not-scored scenarios fail.

    Recounting every FAILURE check as a conformance failure reimplements the
    requirement set and disagrees with the pinned CLI.
    """
    _patch_cli(
        monkeypatch,
        returncode=0,
        checks=[_success("tools-list"), _failure("tasks-lifecycle")],
    )
    run = await ConformanceAdapter(output_root=tmp_path).run_server(
        target=TARGET,
        url="http://127.0.0.1:9/mcp",
    )

    assert _selector(run) == "requirements"
    assert run.outcome is ConformanceOutcome.OFFICIAL_CONFORMANCE_PASS
    assert run.scenarios_failed == 1


@pytest.mark.asyncio
async def test_scenario_probe_is_not_a_requirements_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A one-scenario pass is not the frozen 2026-07-28 requirement set."""
    calls = _patch_cli(
        monkeypatch,
        returncode=0,
        checks=[_success("tools-list"), _success("tools-name-format")],
    )
    run = await ConformanceAdapter(output_root=tmp_path).run_server(
        target=TARGET,
        url="http://127.0.0.1:9/mcp",
        scenario="tools-list",
    )

    command = calls[0]
    assert "--scenario" in command
    assert "tools-list" in command
    assert "--spec-version" in command
    assert "2026-07-28" in command
    assert "--requirements" not in command
    assert _selector(run) == "scenario"
    assert run.outcome is ConformanceOutcome.OFFICIAL_CONFORMANCE_PASS


@pytest.mark.asyncio
async def test_scenario_probe_does_not_treat_a_zero_exit_as_a_pass_over_a_failed_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_cli(
        monkeypatch,
        returncode=0,
        checks=[_success("tools-list"), _failure("tools-name-format")],
    )
    run = await ConformanceAdapter(output_root=tmp_path).run_server(
        target=TARGET,
        url="http://127.0.0.1:9/mcp",
        scenario="tools-list",
    )

    assert _selector(run) == "scenario"
    assert run.outcome is ConformanceOutcome.OFFICIAL_CONFORMANCE_FAILURE


def test_command_builder_never_mixes_requirements_with_spec_version() -> None:
    """The pinned CLI exits 1 before writing checks if these flags are combined."""
    from interop_lab.conformance.adapter import build_server_command

    requirements, selector = build_server_command(
        cli=Path("cli.js"),
        url="http://127.0.0.1:9/mcp",
        spec_version="2026-07-28",
        output_dir=Path("/tmp/out"),
        scenario=None,
    )
    scenario, scenario_selector = build_server_command(
        cli=Path("cli.js"),
        url="http://127.0.0.1:9/mcp",
        spec_version="2025-11-25",
        output_dir=Path("/tmp/out"),
        scenario="server-initialize",
    )

    assert selector == "requirements"
    assert "--requirements" in requirements
    assert "--spec-version" not in requirements
    assert scenario_selector == "scenario"
    assert "--requirements" not in scenario
    assert "--scenario" in scenario
    assert "--spec-version" in scenario
    assert "2025-11-25" in scenario


def test_skipped_and_informational_checks_are_counted_apart_from_passes() -> None:
    from interop_lab.conformance.parser import parse_checks

    checks = parse_checks(
        [
            _success("a"),
            _failure("b"),
            {"id": "c", "name": "c", "status": "SKIPPED", "description": "d"},
            {"id": "d", "name": "d", "status": "WARNING", "description": "d"},
            {"id": "e", "name": "e", "status": "INFO", "description": "d"},
        ]
    )
    assert [c.status for c in checks] == ["SUCCESS", "FAILURE", "SKIPPED", "WARNING", "INFO"]
    skipped = [c for c in checks if c.passed is None and c.status == "SKIPPED"]
    informational = [c for c in checks if c.passed is None and c.status in {"WARNING", "INFO"}]
    assert len(skipped) == 1
    assert len(informational) == 2
    assert not any(c.passed is True for c in skipped + informational)


def test_scenario_probe_does_not_serialize_a_requirements_field() -> None:
    run = ConformanceRun(
        revision="2026-07-28",
        selector=ConformanceSelector.SCENARIO,
        requirements="2026-07-28",
        target_id="t",
        url="http://127.0.0.1:1/mcp",
        command=["node"],
        outcome=ConformanceOutcome.OFFICIAL_CONFORMANCE_PASS,
    )
    data = run.model_dump(mode="json")
    assert "requirements" not in data
    assert data["revision"] == "2026-07-28"
    assert data["selector"] == "scenario"
    assert "requirements" not in run.provenance()


def test_requirements_run_keeps_the_requirements_field() -> None:
    run = ConformanceRun(
        revision="2025-11-25",
        selector=ConformanceSelector.REQUIREMENTS,
        target_id="t",
        url="http://127.0.0.1:1/mcp",
        command=["node"],
        outcome=ConformanceOutcome.OFFICIAL_CONFORMANCE_FAILURE,
    )
    data = run.model_dump(mode="json")
    assert data["requirements"] == "2025-11-25"
    assert run.provenance()["requirements"] == "2025-11-25"


@pytest.mark.asyncio
async def test_timeout_kills_and_reaps_the_official_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _Hanging:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.killed = False
            self.waited = False

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            self.waited = True
            self.returncode = -9
            return -9

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(30)
            return b"", b""

    hanging = _Hanging()

    async def fake_exec(*command: str, **kwargs: object) -> _Hanging:
        del command, kwargs
        return hanging

    monkeypatch.setattr(
        "interop_lab.conformance.adapter.asyncio.create_subprocess_exec",
        fake_exec,
    )
    monkeypatch.setattr("interop_lab.conformance.adapter.is_available", lambda: True)

    run = await ConformanceAdapter(output_root=tmp_path).run_server(
        target=TARGET,
        url="http://127.0.0.1:9/mcp",
        timeout_s=0.05,
    )

    assert hanging.killed is True
    assert hanging.waited is True
    assert run.outcome is ConformanceOutcome.HARNESS_FAILURE
    assert run.parse_note is not None and "exceeded" in run.parse_note
