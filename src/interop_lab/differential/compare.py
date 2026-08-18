"""Cross-implementation differential comparison.

Compares semantically, not byte-for-byte, and separates three questions that are
easy to conflate:

  1. Do the traces differ at all?
  2. Is the difference meaningful, or an allowed implementation choice?
  3. Does it violate a dated normative requirement?

A divergence is never reported as a defect on its own.
"""

from __future__ import annotations

from typing import Any

from interop_lab.events.model import Direction, WireEvent, WireTrace
from interop_lab.models.core import (
    Classification,
    DifferentialResult,
    DifferentialVerdict,
    RunResult,
    TraceDifference,
)
from interop_lab.normalization.normalize import normalize_trace

#: Result members that implementations may legitimately differ on.
TOLERATED_RESULT_KEYS: frozenset[str] = frozenset(
    {
        # Advisory cache hints.
        "cacheScope",
        "ttlMs",
        # Server-supplied prose and identity.
        "instructions",
        "_meta",
        "serverInfo",
        "protocolVersions",
    }
)

#: Differences in these members change how a client behaves.
SEMANTIC_RESULT_KEYS: frozenset[str] = frozenset({"content", "isError", "resultType", "tools"})


def _first_raw_difference(left: WireTrace, right: WireTrace) -> int | None:
    left_events = [e for e in left.events if e.direction in (Direction.OUTBOUND, Direction.INBOUND)]
    right_events = [
        e for e in right.events if e.direction in (Direction.OUTBOUND, Direction.INBOUND)
    ]
    for index in range(max(len(left_events), len(right_events))):
        if index >= len(left_events) or index >= len(right_events):
            return index
        if left_events[index].raw_sha256 != right_events[index].raw_sha256:
            return index
    return None


def _describe(value: Any) -> Any:
    if isinstance(value, dict):
        return dict.fromkeys(sorted(value), "<...>")
    if isinstance(value, list):
        return f"<array len={len(value)}>"
    return value


def _compare_results(
    seq: int, left: dict[str, Any], right: dict[str, Any]
) -> list[TraceDifference]:
    differences: list[TraceDifference] = []
    for key in sorted(set(left) | set(right)):
        left_value = left.get(key, "<absent>")
        right_value = right.get(key, "<absent>")
        if left_value == right_value:
            continue

        if key in SEMANTIC_RESULT_KEYS:
            meaningful = True
            if (
                key in ("content", "tools")
                and isinstance(left_value, list)
                and isinstance(right_value, list)
            ):
                # Payload shape differences within content are frequently just
                # richer metadata; only differing length or type is semantic.
                meaningful = len(left_value) != len(right_value)
                explanation = (
                    f"result.{key} differs in length ({len(left_value)} vs {len(right_value)})"
                    if meaningful
                    else f"result.{key} differs only in per-item detail"
                )
            else:
                explanation = (
                    f"result.{key} differs; this member changes how a client "
                    f"parses or acts on the result"
                )
        elif key in TOLERATED_RESULT_KEYS:
            meaningful = False
            explanation = f"result.{key} is an implementation-specific or advisory member"
        else:
            meaningful = False
            explanation = (
                f"result.{key} is not in the harness's semantic key set; "
                "reported as an observed difference only"
            )

        differences.append(
            TraceDifference(
                seq=seq,
                field_path=f"result.{key}",
                left=_describe(left_value),
                right=_describe(right_value),
                meaningful=meaningful,
                explanation=explanation,
            )
        )
    return differences


def _compare_event(
    seq: int, left: WireEvent | None, right: WireEvent | None
) -> list[TraceDifference]:
    if left is None or right is None:
        present = "left" if right is None else "right"
        return [
            TraceDifference(
                seq=seq,
                field_path="<event>",
                left=left.rpc_method if left else None,
                right=right.rpc_method if right else None,
                meaningful=True,
                explanation=f"only the {present} trace has an event at this position",
            )
        ]

    if left.message is None or right.message is None:
        if left.message is None and right.message is None:
            return []
        return [
            TraceDifference(
                seq=seq,
                field_path="<parse>",
                left=left.parse_error,
                right=right.parse_error,
                meaningful=True,
                explanation="one side emitted output that is not a JSON object",
            )
        ]

    left_msg, right_msg = left.message, right.message
    differences: list[TraceDifference] = []

    left_kind = "error" if "error" in left_msg else "result" if "result" in left_msg else "request"
    right_kind = (
        "error" if "error" in right_msg else "result" if "result" in right_msg else "request"
    )
    if left_kind != right_kind:
        return [
            TraceDifference(
                seq=seq,
                field_path="<envelope>",
                left=left_kind,
                right=right_kind,
                meaningful=True,
                explanation=(
                    f"one implementation answered with a {left_kind} and the other with a "
                    f"{right_kind}"
                ),
            )
        ]

    if left_kind == "error":
        left_code = left.error_code()
        right_code = right.error_code()
        if left_code != right_code:
            differences.append(
                TraceDifference(
                    seq=seq,
                    field_path="error.code",
                    left=left_code,
                    right=right_code,
                    meaningful=True,
                    explanation="JSON-RPC error codes are normative and differ",
                )
            )
        return differences

    if left_kind == "result":
        left_result = left_msg.get("result")
        right_result = right_msg.get("result")
        if isinstance(left_result, dict) and isinstance(right_result, dict):
            differences.extend(_compare_results(seq, left_result, right_result))

    return differences


def compare(
    left_run: RunResult,
    left_trace: WireTrace,
    right_run: RunResult,
    right_trace: WireTrace,
) -> DifferentialResult:
    """Compare two runs of the same scenario against different targets."""
    limitations: list[str] = []
    verdict = DifferentialVerdict.EQUIVALENT

    if left_run.scenario_id != right_run.scenario_id:
        raise ValueError("differential comparison requires the same scenario on both sides")

    incomparable_reason: str | None = None
    if Classification.NOT_APPLICABLE in (left_run.classification, right_run.classification):
        incomparable_reason = (
            "the scenario is not applicable to one of the targets, so no behavioural "
            "comparison is possible"
        )
    elif left_run.spec_version != right_run.spec_version:
        limitations.append(
            f"targets ran at different specification revisions "
            f"({left_run.spec_version} vs {right_run.spec_version}); differences may be "
            f"mandated by the specifications rather than chosen by the implementations"
        )

    if incomparable_reason is not None:
        return DifferentialResult(
            scenario_id=left_run.scenario_id,
            left_target=left_run.target_id,
            right_target=right_run.target_id,
            left_classification=left_run.classification,
            right_classification=right_run.classification,
            verdict=DifferentialVerdict.INCOMPARABLE,
            normative_interpretation="",
            limitations=incomparable_reason,
        )

    left_norm = normalize_trace(left_trace)
    right_norm = normalize_trace(right_trace)

    differences: list[TraceDifference] = []
    first_normalized: int | None = None
    length = max(len(left_norm.events), len(right_norm.events))
    for index in range(length):
        left_event = left_norm.events[index] if index < len(left_norm.events) else None
        right_event = right_norm.events[index] if index < len(right_norm.events) else None
        event_differences = _compare_event(index, left_event, right_event)
        if (
            event_differences
            and first_normalized is None
            and any(d.meaningful for d in event_differences)
        ):
            first_normalized = index
        differences.extend(event_differences)

    meaningful = [d for d in differences if d.meaningful]

    if left_run.classification is not right_run.classification:
        verdict = DifferentialVerdict.MEANINGFUL_DIVERGENCE
        interpretation = (
            f"{left_run.target_id} was classified {left_run.classification.value} while "
            f"{right_run.target_id} was classified {right_run.classification.value} on the "
            f"same scenario."
        )
        if Classification.SPEC_VIOLATION in (
            left_run.classification,
            right_run.classification,
        ):
            verdict = DifferentialVerdict.NORMATIVE_DIVERGENCE
    elif meaningful:
        verdict = DifferentialVerdict.MEANINGFUL_DIVERGENCE
        interpretation = (
            f"Both targets were classified {left_run.classification.value}, but "
            f"{len(meaningful)} semantic difference(s) were observed on the wire."
        )
    elif differences:
        verdict = DifferentialVerdict.EXPECTED_DIFFERENCE
        interpretation = (
            "All observed differences are in advisory or implementation-specific members."
        )
    else:
        interpretation = "The normalised traces are equivalent."

    limitations.append(
        "Equivalence is asserted only over the scenario's steps, this transport, and "
        "these pinned versions. It is not a general compatibility claim."
    )

    return DifferentialResult(
        scenario_id=left_run.scenario_id,
        left_target=left_run.target_id,
        right_target=right_run.target_id,
        left_classification=left_run.classification,
        right_classification=right_run.classification,
        verdict=verdict,
        first_raw_difference_seq=_first_raw_difference(left_trace, right_trace),
        first_normalized_difference_seq=first_normalized,
        differences=differences,
        normative_interpretation=interpretation,
        limitations=" ".join(limitations),
    )
