"""The viewer dataset's JSON Schema, generated from the models that define it.

The frontend validates against this file at load time. Generating it rather than
writing it is what makes that validation worth doing: a schema typed alongside a
model agrees with it until the model changes, at which point the frontend
certifies a shape the exporter no longer produces.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from interop_lab.viewer.model import VIEWER_SCHEMA_VERSION, ViewerDataset

DEFAULT_SCHEMA_PATH = Path("viewer/src/data/viewer-dataset.schema.json")


def build_schema() -> dict[str, Any]:
    schema = ViewerDataset.model_json_schema(ref_template="#/$defs/{model}")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = (
        f"https://interop-lab.invalid/schemas/viewer-{VIEWER_SCHEMA_VERSION}.schema.json"
    )
    schema["description"] = (
        "Generated from interop_lab.viewer.model. Every definition rejects unknown "
        "fields: a dataset carrying a key this schema does not name was written "
        "against a different version of the exporter, and rendering it leniently "
        "would put values on a screen that nothing checked."
    )
    return schema


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"
