"""The benchmark's JSON Schema, generated from the models that define it.

Generated rather than hand-written, for the same reason the definition is
generated: a schema someone typed alongside a model drifts from it silently, and
the first thing anyone learns is that validation passed against a shape the code
no longer produces. Here the drift is checkable — ``benchmark schema --check``
fails when the committed file disagrees with the models.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from interop_lab.benchmark.coverage import MeasurementCoverage, MeasurementCoverageEntry
from interop_lab.benchmark.model import (
    BENCHMARK_SCHEMA_VERSION,
    BenchmarkDefinition,
    BenchmarkOutputs,
    BenchmarkSample,
    CategoryObservation,
    TimingSummary,
)
from interop_lab.benchmark.operations import MeasuredOperations, OperationMeasurement
from interop_lab.benchmark.reproducibility import ReproducibilityComparison

DEFAULT_SCHEMA_PATH = Path("benchmarks/schemas/benchmark-0.3.schema.json")

#: The models a benchmark artifact is validated against.
EXPORTED_MODELS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("BenchmarkDefinition", BenchmarkDefinition),
    ("BenchmarkSample", BenchmarkSample),
    ("CategoryObservation", CategoryObservation),
    ("TimingSummary", TimingSummary),
    ("BenchmarkOutputs", BenchmarkOutputs),
    ("ReproducibilityComparison", ReproducibilityComparison),
    ("MeasurementCoverage", MeasurementCoverage),
    ("MeasurementCoverageEntry", MeasurementCoverageEntry),
    ("MeasuredOperations", MeasuredOperations),
    ("OperationMeasurement", OperationMeasurement),
)


def build_schema() -> dict[str, Any]:
    """Assemble one document holding every benchmark artifact's shape."""
    definitions: dict[str, Any] = {}
    for name, model in EXPORTED_MODELS:
        sub = model.model_json_schema(ref_template="#/$defs/{model}")
        for def_name, def_schema in sub.pop("$defs", {}).items():
            definitions[def_name] = def_schema
        definitions[name] = sub

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": (
            f"https://interop-lab.invalid/schemas/benchmark-{BENCHMARK_SCHEMA_VERSION}.schema.json"
        ),
        "title": (
            "MCP Interoperability and Failure-Recovery Benchmark — Private "
            f"{BENCHMARK_SCHEMA_VERSION}"
        ),
        "description": (
            "Generated from the pydantic models in interop_lab.benchmark.model. Every "
            "definition rejects unknown fields: a document carrying a key this schema "
            "does not name was written against a different version, and validating it "
            "leniently would certify a shape the harness does not produce."
        ),
        "$defs": definitions,
    }


def render(schema: dict[str, Any]) -> str:
    """Serialize deterministically, so an unchanged schema produces no diff."""
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def validate_artifact(payload: Any, model_name: str, schema: dict[str, Any]) -> list[str]:
    """Validate one artifact against a named definition in the schema.

    Returns the violations rather than raising, so a caller can report every
    failing artifact in one pass instead of one per run.
    """
    from jsonschema.validators import Draft202012Validator

    definition = schema["$defs"].get(model_name)
    if definition is None:
        return [f"the schema defines no {model_name!r}"]

    # The whole document is supplied as the resolution root so `$ref`s into
    # `$defs` resolve; the sub-schema alone would leave every reference dangling
    # and validation would pass by not checking anything.
    resolved = {**definition, "$defs": schema["$defs"]}
    validator = Draft202012Validator(resolved)
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    ]
