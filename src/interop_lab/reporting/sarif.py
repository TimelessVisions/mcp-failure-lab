"""SARIF 2.1.0 output.

Rule ids are stable so a code-scanning dashboard can track a finding across
runs. Locations use artifact-relative URIs — a SARIF file carrying an absolute
path from the machine that produced it is useless to anyone else.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from interop_lab.models.core import (
    FAILING,
    AssertionStatus,
    Classification,
    RunResult,
)
from interop_lab.naming import HARNESS_VERSION, PRODUCT_NAME

SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json"

#: Stable rule identifiers. Changing one breaks dashboard history, so they are
#: treated as a public interface.
RULE_IDS: dict[Classification, str] = {
    Classification.SPEC_VIOLATION: "MCP-SPEC-VIOLATION",
    Classification.INVALID_WIRE_MESSAGE: "MCP-INVALID-WIRE-MESSAGE",
    Classification.CRASH: "MCP-CRASH",
    Classification.HANG: "MCP-HANG",
    Classification.TIMEOUT: "MCP-TIMEOUT",
    Classification.RECOVERY_FAILURE: "MCP-RECOVERY-FAILURE",
    Classification.CLEANUP_FAILURE: "MCP-CLEANUP-FAILURE",
    Classification.SECURITY_RELEVANT: "MCP-SECURITY-RELEVANT",
    Classification.BEHAVIORAL_DIVERGENCE: "MCP-BEHAVIORAL-DIVERGENCE",
    Classification.HARNESS_ERROR: "INTEROP-HARNESS-ERROR",
    Classification.INCONCLUSIVE: "INTEROP-INCONCLUSIVE",
}

ENVIRONMENT_RULE = "INTEROP-ENVIRONMENT-MISMATCH"

_LEVELS: dict[Classification, str] = {
    Classification.SPEC_VIOLATION: "error",
    Classification.INVALID_WIRE_MESSAGE: "error",
    Classification.CRASH: "error",
    Classification.HANG: "error",
    Classification.TIMEOUT: "error",
    Classification.RECOVERY_FAILURE: "error",
    Classification.CLEANUP_FAILURE: "error",
    Classification.SECURITY_RELEVANT: "warning",
    Classification.BEHAVIORAL_DIVERGENCE: "note",
    Classification.HARNESS_ERROR: "error",
    Classification.INCONCLUSIVE: "note",
}

_DESCRIPTIONS: dict[str, str] = {
    "MCP-SPEC-VIOLATION": "The target violated a requirement of its dated protocol profile.",
    "MCP-INVALID-WIRE-MESSAGE": "The target emitted output that is not a valid protocol message.",
    "MCP-CRASH": "The target process exited unexpectedly.",
    "MCP-HANG": "The target remained running but stopped responding.",
    "MCP-TIMEOUT": "No response arrived before the deadline.",
    "MCP-RECOVERY-FAILURE": "The target did not recover after a fault.",
    "MCP-CLEANUP-FAILURE": "The target's process tree could not be reclaimed.",
    "MCP-SECURITY-RELEVANT": "Security-relevant behaviour was observed. Not a vulnerability claim.",
    "MCP-BEHAVIORAL-DIVERGENCE": (
        "Implementations differed without violating a dated requirement."
    ),
    "INTEROP-HARNESS-ERROR": "The harness failed. This is not a statement about the target.",
    "INTEROP-INCONCLUSIVE": "The result could not be decided.",
    ENVIRONMENT_RULE: "Declared and observed runtime disagree.",
}


def _rules() -> list[dict[str, Any]]:
    return [
        {
            "id": rule_id,
            "name": rule_id.replace("-", ""),
            "shortDescription": {"text": _DESCRIPTIONS[rule_id]},
            "fullDescription": {"text": _DESCRIPTIONS[rule_id]},
            "defaultConfiguration": {
                "level": next(
                    (lvl for cls, lvl in _LEVELS.items() if RULE_IDS.get(cls) == rule_id),
                    "warning",
                )
            },
            "properties": {"tags": ["mcp", "protocol", "interoperability"]},
        }
        for rule_id in sorted(set(RULE_IDS.values()) | {ENVIRONMENT_RULE})
    ]


def _result(run: RunResult) -> dict[str, Any]:
    rule_id = RULE_IDS.get(run.classification, "INTEROP-INCONCLUSIVE")
    failed = [a for a in run.assertions if a.status is AssertionStatus.FAIL]
    normative = next((a.normative_source for a in failed if a.normative_source), None)

    message = f"{run.classification.value} on {run.target_id} in {run.scenario_id}"
    if failed:
        message += f": expected {failed[0].expected}, observed {failed[0].actual}"

    # Artifact-relative, never a machine path.
    evidence_uri = run.trace_path or f"artifact://runs/{run.run_id}/wire.jsonl"

    properties: dict[str, Any] = {
        "scenarioId": run.scenario_id,
        "scenarioVersion": run.scenario_version,
        "targetId": run.target_id,
        "protocolProfile": run.protocol_profile,
        "specVersion": run.spec_version,
        "transport": run.transport.value,
        "classification": run.classification.value,
        "seed": run.seed,
        "evidenceUri": evidence_uri,
        "reproductionCommand": "uv run mcp-failure-lab replay <package>/repro.yaml",
        "limitations": (
            "Valid only for the pinned target version, transport and protocol profile "
            "recorded in the artifact. A local fixture with a deliberate defect is a "
            "test of the harness, not a third-party finding."
        ),
    }
    if normative:
        properties["normativeSource"] = normative
    if failed:
        properties["failedAssertions"] = [a.assertion_id for a in failed]
    if run.timeout_cause:
        properties["timeoutCause"] = run.timeout_cause.value
    if run.resolved_target:
        properties["targetVersion"] = run.resolved_target.version

    # Both dimensions travel, and the rule id stays the *outcome*. Routing a
    # normative failure to MCP-SECURITY-RELEVANT because it also touches a
    # security property would break the one thing a stable rule id is for —
    # tracking one finding across runs — and would tell a dashboard that a
    # violated MUST is not a conformance result.
    dimensions = run.dimensions
    properties["normative"] = dimensions.normative
    properties["securityRelevant"] = dimensions.security_relevant
    properties["resilienceRelevant"] = dimensions.resilience_relevant
    properties["severity"] = dimensions.severity.value
    properties["vulnerabilityStatus"] = dimensions.vulnerability_status.value
    if dimensions.security_category is not None:
        properties["securityCategory"] = dimensions.security_category.value
    if dimensions.security_relevant:
        properties["securityLimitations"] = list(dimensions.security_limitations)
        properties["securityInterpretation"] = (
            "Security-relevant observation. The rule id above is the conformance "
            "outcome; this property records that the same result touches a security "
            "property. Neither implies the other, and vulnerabilityStatus states what "
            "has actually been established about exploiting it."
        )

    return {
        "ruleId": rule_id,
        "level": _LEVELS.get(run.classification, "warning"),
        "message": {"text": message},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": evidence_uri.replace("artifact://", "")},
                    "region": {"startLine": 1},
                }
            }
        ],
        "partialFingerprints": {
            "interopLabSignature": f"{run.target_id}::{run.scenario_id}::{run.classification.value}"
        },
        "properties": properties,
    }


#: The pinned schema this project validates against. Offline by design: a schema
#: that can change under you is not a pin.
#:
#: Not every distribution carries it. The OASIS IPR Policy is not an open source
#: license and its redistribution terms for the schema file are unconfirmed, so
#: the public distribution excludes the copy. Building SARIF does not need the
#: schema and is unaffected; only ``validate`` does, and it says so rather than
#: quietly passing. Use ``pinned_schema_available`` to ask before validating.
PINNED_SCHEMA_PATH = Path("schemas/sarif-2.1.0.json")
PINNED_SCHEMA_SHA256 = "d98ca0ea753fe2c5ad127ce0a4eb2899614ad277141ab071e12c905457b708c0"
PINNED_SCHEMA_COMMIT = "123e95847b13fbdd4cbe2120fa5e33355d4a042b"


class SarifSchemaError(Exception):
    """The pinned schema is missing or has changed."""


class SarifValidationError(Exception):
    """Generated SARIF does not conform to the pinned 2.1.0 schema."""


def pinned_schema_available(path: Path | None = None) -> bool:
    """Whether this installation carries the pinned schema at all.

    Distinguishes "cannot validate here" from "failed validation". A caller that
    treats the two the same either reports a false failure or, worse, skips a
    real one.
    """
    return (path or PINNED_SCHEMA_PATH).is_file()


def load_pinned_schema(path: Path | None = None) -> dict[str, Any]:
    """Load the pinned SARIF schema, verifying it has not changed."""
    schema_path = path or PINNED_SCHEMA_PATH
    if not schema_path.is_file():
        raise SarifSchemaError(
            f"pinned SARIF schema missing at {schema_path}. SARIF is still "
            "generated; offline validation against the OASIS schema is not "
            "included in this distribution because the schema's redistribution "
            "terms are unconfirmed. See THIRD_PARTY_NOTICES."
        )
    raw = schema_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != PINNED_SCHEMA_SHA256:
        raise SarifSchemaError(
            f"pinned SARIF schema changed: expected {PINNED_SCHEMA_SHA256}, got {digest}"
        )
    return json.loads(raw)


def validate(log: dict[str, Any], *, schema_path: Path | None = None) -> None:
    """Validate a SARIF log against the pinned 2.1.0 schema.

    Raises ``jsonschema.ValidationError`` on any violation. Shape assertions are
    not a substitute for this: they only check what someone thought to check.
    """
    from jsonschema.validators import Draft202012Validator

    schema = load_pinned_schema(schema_path)
    # The validator is constructed directly rather than via jsonschema.validate(),
    # which would first run check_schema() against the 2020-12 metaschema. The
    # published OASIS document does not itself pass that check, and the question
    # here is whether *our output* conforms to SARIF — not whether OASIS's file
    # conforms to a metaschema. Validating the instance is the useful assertion.
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(log), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        location = "/".join(str(p) for p in first.absolute_path) or "<root>"
        raise SarifValidationError(
            f"{len(errors)} SARIF schema violation(s); first at {location}: {first.message}"
        )


def build(runs: list[RunResult]) -> dict[str, Any]:
    """Build a SARIF 2.1.0 log. Only decided, non-skipped results become results."""
    reportable = [
        run
        for run in runs
        if run.classification is not Classification.PASS
        and run.classification is not Classification.NOT_APPLICABLE
    ]
    return {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": PRODUCT_NAME,
                        "version": HARNESS_VERSION,
                        "informationUri": "https://github.com/TimelessVisions/mcp-failure-lab",
                        "rules": _rules(),
                    }
                },
                "results": [_result(run) for run in reportable],
                "invocations": [
                    {
                        "executionSuccessful": not any(r.classification in FAILING for r in runs),
                    }
                ],
            }
        ],
    }
