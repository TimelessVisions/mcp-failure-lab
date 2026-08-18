"""Matrix and suite configuration for the CI gate."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from interop_lab.config.loader import ConfigError, load_target
from interop_lab.models.core import TargetDefinition
from interop_lab.scenarios.library import get_scenario
from interop_lab.scenarios.model import Scenario


class MatrixDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matrix_id: str
    description: str = ""
    targets: list[str]


class SuiteDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite_id: str
    description: str = ""
    scenarios: list[str]


#: Shown when a matrix or suite file is malformed. A validation error names the
#: missing field but not what a correct file looks like, which is only obvious to
#: someone who already has one — exactly the insider knowledge that makes a tool
#: hard to adopt. Found by installing this harness into an unrelated repository.
_EXAMPLES: dict[str, str] = {
    "MatrixDocument": (
        "matrix_id: my-matrix\n"
        "description: What this matrix covers.\n"
        "targets:\n"
        "  - path/to/target.yaml\n"
    ),
    "SuiteDocument": (
        "suite_id: my-suite\n"
        "description: What this suite protects.\n"
        "scenarios:\n"
        "  - baseline.tools_list\n"
        "  - wire.unknown_method\n"
    ),
}


def _load(path: Path, model: type[BaseModel]) -> BaseModel:
    if not path.is_file():
        raise ConfigError(f"not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return model.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as exc:
        example = _EXAMPLES.get(model.__name__)
        hint = f"\n\nA valid file looks like:\n\n{example}" if example else ""
        raise ConfigError(f"{path}: {exc}{hint}") from exc


def load_matrix(path: Path) -> tuple[MatrixDocument, list[TargetDefinition]]:
    document = _load(path, MatrixDocument)
    assert isinstance(document, MatrixDocument)
    return document, [load_target(Path(p)) for p in document.targets]


def load_suite(path: Path) -> tuple[SuiteDocument, list[Scenario]]:
    document = _load(path, SuiteDocument)
    assert isinstance(document, SuiteDocument)
    try:
        scenarios = [get_scenario(sid) for sid in document.scenarios]
    except KeyError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return document, scenarios
