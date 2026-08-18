"""Configuration loading.

Targets are YAML on disk but are fully resolved before use, and the resolved
object — not the path — is what gets embedded in artifacts. A run must remain
reproducible after the YAML is edited.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from interop_lab.models.core import TargetDefinition
from interop_lab.profiles.registry import get_profile


class ConfigError(Exception):
    """Raised when configuration is missing, malformed, or internally inconsistent."""


def load_target(path: Path | str) -> TargetDefinition:
    target_path = Path(path)
    if not target_path.is_file():
        raise ConfigError(f"target definition not found: {target_path}")

    try:
        raw: Any = yaml.safe_load(target_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{target_path}: invalid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{target_path}: expected a mapping at the top level")

    try:
        target = TargetDefinition.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{target_path}: {exc}") from exc

    try:
        profile = get_profile(target.protocol_profile)
    except KeyError as exc:
        raise ConfigError(f"{target_path}: {exc}") from exc

    if not profile.supports(target.transport):
        raise ConfigError(
            f"{target_path}: profile {profile.profile_id} does not support "
            f"transport {target.transport}"
        )
    return target


def load_targets(paths: list[Path | str]) -> list[TargetDefinition]:
    targets = [load_target(p) for p in paths]
    seen: set[str] = set()
    for target in targets:
        if target.target_id in seen:
            raise ConfigError(f"duplicate target_id {target.target_id!r}")
        seen.add(target.target_id)
    return targets


def discover_targets(root: Path) -> list[Path]:
    """Find every target YAML under ``root``, sorted for deterministic ordering."""
    return sorted(p for p in root.rglob("*.yaml") if p.is_file())
