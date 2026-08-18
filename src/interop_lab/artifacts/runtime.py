"""Runtime fingerprints: what was declared versus what actually ran.

A target YAML states an intent. The process that starts is a fact. Serialising
the declared value as though it were observed is how a reproduction quietly
stops being reproducible — the artifact says Python 3.12.11 because the YAML
said so, while the run actually happened on 3.12.13.

The two are captured separately and compared. A disagreement is recorded and
surfaced, never silently resolved in either direction.
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from interop_lab.naming import ARTIFACT_SCHEMA_VERSION, HARNESS_VERSION


class RuntimeFingerprint(BaseModel):
    """Identity of the environment a run actually executed in."""

    model_config = ConfigDict(extra="forbid")

    artifact_schema_version: str = ARTIFACT_SCHEMA_VERSION
    harness_version: str = HARNESS_VERSION

    os: str
    arch: str
    #: Interpreter identity without a machine-specific path.
    python_version: str | None = None
    python_implementation: str | None = None
    node_version: str | None = None

    target_package: str | None = None
    target_package_version: str | None = None
    sdk_version: str | None = None
    target_commit_sha: str | None = None

    protocol_profile: str
    transport: str
    #: Hash of uv.lock, so a dependency change is visible without shipping it.
    dependency_lock_sha256: str | None = None
    #: Populated once containment exists.
    container_image_digest: str | None = None


class RuntimeMismatch(BaseModel):
    """One disagreement between declared and observed runtime."""

    model_config = ConfigDict(extra="forbid")

    field: str
    declared: str | None
    observed: str | None

    def describe(self) -> str:
        return f"{self.field}: target declares {self.declared!r}, process reports {self.observed!r}"


class RuntimeRecord(BaseModel):
    """Declared and observed side by side, plus any disagreements."""

    model_config = ConfigDict(extra="forbid")

    expected: RuntimeFingerprint
    observed: RuntimeFingerprint
    mismatches: list[RuntimeMismatch] = Field(default_factory=list)

    @property
    def has_mismatch(self) -> bool:
        return bool(self.mismatches)

    def warnings(self) -> list[str]:
        return [m.describe() for m in self.mismatches]


def lock_hash(lock_path: Path | None = None) -> str | None:
    path = lock_path or Path("uv.lock")
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def observed_python_version() -> str:
    return platform.python_version()


def observed_node_version() -> str | None:
    try:
        completed = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def package_version(name: str) -> str | None:
    import importlib.metadata as metadata

    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def build_runtime_record(
    *,
    protocol_profile: str,
    transport: str,
    language: str,
    declared_runtime_version: str | None,
    target_package: str | None = None,
    declared_package_version: str | None = None,
    target_commit_sha: str | None = None,
) -> RuntimeRecord:
    """Capture declared and observed runtime facts and compare them."""
    is_python = language.lower() == "python"
    is_node = language.lower() in ("typescript", "javascript", "node")

    observed_python = observed_python_version() if is_python else None
    observed_node = observed_node_version() if is_node else None
    observed_package = package_version(target_package) if target_package else None

    expected = RuntimeFingerprint(
        os=platform.system(),
        arch=platform.machine(),
        python_version=declared_runtime_version if is_python else None,
        python_implementation=platform.python_implementation() if is_python else None,
        node_version=declared_runtime_version if is_node else None,
        target_package=target_package,
        target_package_version=declared_package_version,
        sdk_version=declared_package_version,
        target_commit_sha=target_commit_sha,
        protocol_profile=protocol_profile,
        transport=transport,
        dependency_lock_sha256=lock_hash(),
    )

    observed = RuntimeFingerprint(
        os=platform.system(),
        arch=platform.machine(),
        python_version=observed_python,
        python_implementation=platform.python_implementation() if is_python else None,
        node_version=observed_node,
        target_package=target_package,
        target_package_version=observed_package,
        sdk_version=observed_package,
        target_commit_sha=target_commit_sha,
        protocol_profile=protocol_profile,
        transport=transport,
        dependency_lock_sha256=lock_hash(),
    )

    mismatches: list[RuntimeMismatch] = []
    for field in ("python_version", "node_version", "target_package_version"):
        declared_value = getattr(expected, field)
        observed_value = getattr(observed, field)
        # Only compare when both sides actually claim something.
        if declared_value and observed_value and declared_value != observed_value:
            mismatches.append(
                RuntimeMismatch(field=field, declared=declared_value, observed=observed_value)
            )

    return RuntimeRecord(expected=expected, observed=observed, mismatches=mismatches)


def interpreter_identity() -> str:
    """A stable description of the interpreter, with no filesystem path."""
    return (
        f"{platform.python_implementation()} {platform.python_version()} "
        f"({sys.version_info.releaselevel})"
    )
