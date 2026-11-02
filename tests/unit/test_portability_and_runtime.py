"""Artifact portability and runtime fingerprinting.

These tests exist because a committed artifact that only works on the machine
that produced it is not evidence. The first version of this repository shipped
reproduction scripts containing a temporary scratch path; nothing caught it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from interop_lab.artifacts.portability import (
    PYTHON_TOKEN,
    PortabilityError,
    assert_portable,
    resolve_artifact_uri,
    resolve_executable_tokens,
    to_artifact_uri,
    validate_payload,
    violations_in_text,
)
from interop_lab.artifacts.runtime import build_runtime_record
from interop_lab.models.core import TargetDefinition
from interop_lab.profiles.registry import Role, Transport
from interop_lab.replay.repro import BASH_WRAPPER, POWERSHELL_WRAPPER

FORBIDDEN_SAMPLES = [
    "/tmp/claude-0/-home-user/scratchpad/ex/repro.yaml",
    "/home/user/mcp-failure-lab/artifacts/x.json",
    "C:\\Users\\george\\repo\\repro.yaml",
    "/Users/george/repo/repro.yaml",
    "/runner/work/repo/repo/artifacts",
    "\\\\server\\share\\repro.yaml",
]


@pytest.mark.parametrize("sample", FORBIDDEN_SAMPLES)
def test_absolute_and_machine_paths_are_rejected(sample: str) -> None:
    assert violations_in_text(sample, context="t"), f"failed to reject {sample}"


@pytest.mark.parametrize(
    "sample",
    [
        "artifact://runs/abc/wire.jsonl",
        "artifact://examples/http-header-mismatch/wire.jsonl",
        "fixtures/http_server.py",
        "tools/list",
        "http://127.0.0.1:8000/mcp",
        "${PYTHON}",
    ],
)
def test_portable_references_are_accepted(sample: str) -> None:
    assert not violations_in_text(sample, context="t"), f"wrongly rejected {sample}"


def test_conformance_test_uri_is_not_a_windows_path_and_absolute_paths_still_fail() -> None:
    """``test://`` is a conformance fixture URI. ``t:/`` inside it is not a drive.

    An absolute path beside that URI is still a portability failure until it
    is scrubbed. Scrubbing must keep the URI and remove the path.
    """
    from interop_lab.artifacts.portability import scrub_machine_paths

    uri = "test://nonexistent-resource-for-conformance-testing"
    absolute = "/tmp/port-repro/conformance/http.fixture.2026/run-1"
    windows = "C:/Users/someone/repo/file.json"

    assert not violations_in_text(uri, context="checks.requestedUri")
    assert violations_in_text(absolute, context="command")
    assert violations_in_text(windows, context="raw_stdout")

    command = ["node", "cli.js", "--output-dir", absolute]
    raw_stdout = f"Results saved to {absolute}/server-tools-list\n"
    payload = {
        "command": command,
        "raw_stdout": raw_stdout,
        "message": uri,
        "also": windows,
    }
    assert validate_payload(payload)
    sanitized = {
        "command": [scrub_machine_paths(part) for part in command],
        "raw_stdout": scrub_machine_paths(raw_stdout),
        "message": scrub_machine_paths(uri),
        "also": scrub_machine_paths(windows),
    }
    assert not validate_payload(sanitized), validate_payload(sanitized)
    assert uri in sanitized["message"]
    assert absolute not in sanitized["raw_stdout"]
    assert "<abs-path>" in sanitized["command"][3]
    assert "C:/Users" not in sanitized["also"]


def test_validate_payload_walks_nested_structures() -> None:
    payload = {"a": {"b": [{"c": "/tmp/claude-0/x/y"}]}}
    problems = validate_payload(payload)
    assert problems and "a.b[0].c" in problems[0]


def test_assert_portable_raises_with_context() -> None:
    with pytest.raises(PortabilityError, match="not portable"):
        assert_portable({"trace": "/home/user/x/y.jsonl"})


def test_artifact_uri_roundtrip(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    target = root / "runs" / "abc" / "wire.jsonl"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")

    uri = to_artifact_uri(target, root=root)
    assert uri == "artifact://runs/abc/wire.jsonl"
    assert resolve_artifact_uri(uri, root=root) == target


def test_python_token_resolves_to_the_running_interpreter() -> None:
    import sys

    resolved = resolve_executable_tokens([PYTHON_TOKEN, "script.py"])
    assert resolved == [sys.executable, "script.py"]


# ------------------------------------------------------------- wrappers


def test_bash_wrapper_is_portable_and_quotes_paths() -> None:
    assert "BASH_SOURCE" in BASH_WRAPPER
    assert 'cd -- "$(dirname -- "${BASH_SOURCE[0]}")"' in BASH_WRAPPER
    assert '"$SCRIPT_DIR/repro.yaml"' in BASH_WRAPPER, "must quote for paths with spaces"
    assert not violations_in_text(BASH_WRAPPER, context="run.sh")


def test_powershell_wrapper_is_portable_and_joins_paths() -> None:
    assert "$MyInvocation.MyCommand.Path" in POWERSHELL_WRAPPER
    assert 'Join-Path $ScriptDir "repro.yaml"' in POWERSHELL_WRAPPER
    assert not violations_in_text(POWERSHELL_WRAPPER, context="run.ps1")


@pytest.mark.parametrize("wrapper", [BASH_WRAPPER, POWERSHELL_WRAPPER])
def test_wrappers_contain_no_development_paths(wrapper: str) -> None:
    for fragment in ("/tmp/claude-", "scratchpad", "/home/user/", "C:\\Users\\"):
        assert fragment not in wrapper


# ------------------------------------------------------------- launch commands


def test_absolute_launch_command_is_rejected() -> None:
    with pytest.raises(ValidationError, match="non-portable launch command"):
        TargetDefinition(
            target_id="t",
            role=Role.FIXTURE,
            implementation="x",
            language="python",
            version="1.0",
            launch_command=["/home/user/repo/.venv/bin/python", "fixtures/x.py"],
            transport=Transport.STDIO,
            protocol_profile="mcp-2026-07-28",
        )


def test_python_token_launch_command_is_accepted() -> None:
    target = TargetDefinition(
        target_id="t",
        role=Role.FIXTURE,
        implementation="x",
        language="python",
        version="1.0",
        launch_command=[PYTHON_TOKEN, "fixtures/x.py"],
        transport=Transport.STDIO,
        protocol_profile="mcp-2026-07-28",
    )
    assert target.launch_command[0] == PYTHON_TOKEN


def test_shipped_targets_use_portable_launch_commands() -> None:
    from interop_lab.config.loader import load_target

    for path in sorted(Path("targets").rglob("*.yaml")):
        if path.name.endswith("matrix.yaml"):
            continue
        target = load_target(path)
        for part in target.launch_command:
            assert not violations_in_text(part, context=str(path)), path


# ------------------------------------------------------------- runtime record


def test_observed_python_version_is_measured_not_declared() -> None:
    """Serialising the declared version as observed is how reproducibility rots."""
    record = build_runtime_record(
        protocol_profile="mcp-2026-07-28",
        transport="stdio",
        language="python",
        declared_runtime_version="3.12.999",
    )
    assert record.expected.python_version == "3.12.999"
    assert record.observed.python_version != "3.12.999"
    assert record.has_mismatch
    assert "python_version" in record.warnings()[0]


def test_matching_runtime_produces_no_mismatch() -> None:
    import platform

    record = build_runtime_record(
        protocol_profile="mcp-2026-07-28",
        transport="stdio",
        language="python",
        declared_runtime_version=platform.python_version(),
    )
    assert not record.has_mismatch


def test_runtime_record_captures_identity_without_a_machine_path() -> None:
    record = build_runtime_record(
        protocol_profile="mcp-2026-07-28",
        transport="streamable_http",
        language="python",
        declared_runtime_version=None,
        target_package="mcp",
        declared_package_version="2.0.0",
    )
    payload = record.model_dump(mode="json")
    assert not validate_payload(payload, context="runtime")
    assert record.observed.os
    assert record.observed.arch
    assert record.observed.dependency_lock_sha256


def test_package_version_is_observed_from_the_installed_distribution() -> None:
    record = build_runtime_record(
        protocol_profile="mcp-2026-07-28",
        transport="stdio",
        language="python",
        declared_runtime_version=None,
        target_package="mcp",
        declared_package_version="0.0.1-not-installed",
    )
    assert record.observed.target_package_version == "2.0.0"
    assert record.has_mismatch
