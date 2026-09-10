"""Lossless manager-input projection for normalized OpenClaw trajectories.

The normalized trace intentionally keeps both original ordered content blocks
and convenient derived fields (``assistant`` and ``details.aggregated``).  The
derived values must stay in the normalized artifact for audit, but serializing
both forms into a manager prompt doubles visible evidence.  This module makes a
separate, versioned view which removes a field only after strict equality is
shown.  It never summarizes, redacts, reorders, or merges source steps.
"""

from __future__ import annotations

import copy
import json
from typing import Any


PROJECTION_VERSION = "manager_projection_v2"


class ProjectionError(ValueError):
    pass


def assistant_tool_calls(step: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one ordered representation of a step's calls, for raw or projected views."""
    from_content = [item for item in step.get("content", []) if isinstance(item, dict) and item.get("type") == "tool_call"]
    if from_content:
        return from_content
    assistant = step.get("assistant")
    if isinstance(assistant, dict) and isinstance(assistant.get("tool_calls"), list):
        return [item for item in assistant["tool_calls"] if isinstance(item, dict)]
    return []


def tool_result_call_id(step: dict[str, Any]) -> str | None:
    tool_result = step.get("tool_result")
    if isinstance(tool_result, dict) and isinstance(tool_result.get("tool_call_id"), str):
        return tool_result["tool_call_id"]
    return None


def _typed_json_equal(left: Any, right: Any) -> bool:
    """JSON equality that does not collapse Python's ``True == 1`` edge case."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_typed_json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_typed_json_equal(a, b) for a, b in zip(left, right, strict=True))
    return left == right


def _parsed_json_equal(value: Any, serialized: Any) -> bool:
    if not isinstance(serialized, str):
        return False
    try:
        return _typed_json_equal(json.loads(serialized), value)
    except json.JSONDecodeError:
        return False


def _project_content(content: list[Any], step_id: str) -> tuple[list[Any], list[dict[str, str]], list[dict[str, str]]]:
    projected: list[Any] = []
    removed: list[dict[str, str]] = []
    preserved_difference: list[dict[str, str]] = []
    for index, item in enumerate(content):
        if not isinstance(item, dict) or item.get("type") != "tool_call":
            projected.append(copy.deepcopy(item))
            continue
        call = copy.deepcopy(item)
        if "partial_arguments" in call and _parsed_json_equal(call.get("arguments"), call["partial_arguments"]):
            call.pop("partial_arguments")
            removed.append(
                {
                    "raw_path": f"steps[{step_id}].content[{index}].partial_arguments",
                    "represented_by": f"steps[{step_id}].content[{index}].arguments",
                    "reason": "strict_json_value_equality",
                }
            )
        elif "partial_arguments" in call:
            preserved_difference.append(
                {
                    "raw_path": f"steps[{step_id}].content[{index}].partial_arguments",
                    "projected_path": f"steps[{step_id}].content[{index}].partial_arguments",
                    "reason": "not_strictly_equal_to_arguments",
                }
            )
        projected.append(call)
    return projected, removed, preserved_difference


def _assistant_derived_difference(step: dict[str, Any], raw_content: list[Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    assistant = step.get("assistant")
    if not isinstance(assistant, dict):
        return {}, []
    expected = {
        # Compare with the original content before independently collapsing a
        # JSON-equivalent partial_arguments field.  Otherwise that secondary
        # projection would make a genuinely duplicate assistant.tool_calls
        # field look different and serialize it a second time.
        "thinking": [item.get("text", "") for item in raw_content if isinstance(item, dict) and item.get("type") == "thinking"],
        "text": [item.get("text", "") for item in raw_content if isinstance(item, dict) and item.get("type") == "text"],
        "tool_calls": [item for item in raw_content if isinstance(item, dict) and item.get("type") == "tool_call"],
    }
    differing: dict[str, Any] = {}
    removed: list[dict[str, str]] = []
    for field, value in assistant.items():
        if field in expected and value == expected[field]:
            removed.append(
                {
                    "raw_path": f"assistant.{field}",
                    "represented_by": "content",
                    "reason": "strict_structural_equality",
                }
            )
        else:
            differing[field] = copy.deepcopy(value)
    return differing, removed


def _single_content_text(content: list[Any]) -> tuple[str, int] | None:
    values = [
        (item["text"], index)
        for index, item in enumerate(content)
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
    ]
    return values[0] if len(values) == 1 else None


def project_trace_for_manager(trace: dict[str, Any]) -> dict[str, Any]:
    """Return a prompt-ready trace and complete raw-to-projected mapping record."""
    raw_steps = trace.get("steps")
    if not isinstance(raw_steps, list):
        raise ProjectionError("normalized trace has no steps list")
    seen: set[str] = set()
    projected_steps: list[dict[str, Any]] = []
    mappings: list[dict[str, Any]] = []
    removed_total: list[dict[str, str]] = []
    preserved_total: list[dict[str, str]] = []
    counts = {"source_content_blocks": 0, "projected_content_blocks": 0, "thinking_blocks": 0, "text_blocks": 0, "tool_call_blocks": 0, "other_content_blocks": 0, "tool_result_steps": 0, "stop_reason_steps": 0, "usage_steps": 0}

    for raw_index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, dict):
            raise ProjectionError(f"step {raw_index} is not an object")
        step_id = raw_step.get("source_entry_id")
        if not isinstance(step_id, str) or not step_id or step_id in seen:
            raise ProjectionError("every projected step needs one unique source_entry_id")
        seen.add(step_id)
        content = raw_step.get("content")
        if not isinstance(content, list):
            raise ProjectionError(f"step {step_id} has no content blocks")
        projected_content, removed, preserved = _project_content(content, step_id)
        counts["source_content_blocks"] += len(content)
        counts["projected_content_blocks"] += len(projected_content)
        for item in content:
            if not isinstance(item, dict):
                counts["other_content_blocks"] += 1
            elif item.get("type") == "thinking":
                counts["thinking_blocks"] += 1
            elif item.get("type") == "text":
                counts["text_blocks"] += 1
            elif item.get("type") == "tool_call":
                counts["tool_call_blocks"] += 1
            else:
                counts["other_content_blocks"] += 1
        projected = {
            key: copy.deepcopy(raw_step[key])
            for key in ("source_entry_id", "source_parent_id", "timestamp", "role", "stop_reason", "usage")
            if key in raw_step
        }
        projected["content"] = projected_content
        differing_assistant, assistant_removed = _assistant_derived_difference(raw_step, content)
        removed.extend(
            {
                **entry,
                "raw_path": f"steps[{step_id}].{entry['raw_path']}",
                "represented_by": f"steps[{step_id}].{entry['represented_by']}",
            }
            for entry in assistant_removed
        )
        if differing_assistant:
            projected["assistant_derived_nonduplicate"] = differing_assistant
            preserved.append(
                {
                    "raw_path": f"steps[{step_id}].assistant",
                    "projected_path": f"steps[{step_id}].assistant_derived_nonduplicate",
                    "reason": "one_or_more_derived_fields_differ_from_ordered_content",
                }
            )
        tool_result = raw_step.get("tool_result")
        if isinstance(tool_result, dict):
            counts["tool_result_steps"] += 1
            projected_result = copy.deepcopy(tool_result)
            details = projected_result.get("details")
            aggregated = details.get("aggregated") if isinstance(details, dict) else None
            content_text = _single_content_text(content)
            if isinstance(aggregated, str) and content_text is not None and content_text[0] == aggregated:
                details.pop("aggregated")
                removed.append(
                    {
                        "raw_path": f"steps[{step_id}].tool_result.details.aggregated",
                        "represented_by": f"steps[{step_id}].content[{content_text[1]}].text",
                        "reason": "strict_text_equality",
                    }
                )
            elif isinstance(aggregated, str):
                preserved.append(
                    {
                        "raw_path": f"steps[{step_id}].tool_result.details.aggregated",
                        "projected_path": f"steps[{step_id}].tool_result.details.aggregated",
                        "reason": "not_strictly_equal_to_single_content_text_block",
                    }
                )
            projected["tool_result"] = projected_result
        if raw_step.get("stop_reason") is not None:
            counts["stop_reason_steps"] += 1
        if raw_step.get("usage") is not None:
            counts["usage_steps"] += 1
        projected_steps.append(projected)
        mappings.append(
            {
                "raw_step_id": step_id,
                "projected_step_id": step_id,
                "raw_content_block_indices": list(range(len(content))),
                "projected_content_block_indices": list(range(len(projected_content))),
                "removed_strict_duplicates": removed,
                "preserved_nonduplicates": preserved,
            }
        )
        removed_total.extend(removed)
        preserved_total.extend(preserved)

    if counts["source_content_blocks"] != counts["projected_content_blocks"]:
        raise ProjectionError("projection must preserve one ordered content block for every source block")
    # Keep every top-level source and integrity field by default. ``entries``
    # is the importer's deliberate alias of ``steps``; omit only that exact
    # duplicate and make the reason auditable. A non-identical entries field is
    # retained rather than silently treated as an alias.
    manager_trace = copy.deepcopy(trace)
    top_level_omissions: list[dict[str, str]] = []
    entries = manager_trace.get("entries")
    if "entries" in manager_trace and _typed_json_equal(entries, raw_steps):
        manager_trace.pop("entries")
        top_level_omissions.append(
            {
                "raw_path": "entries",
                "represented_by": "steps",
                "reason": "strict_structural_equality",
            }
        )
    manager_trace["steps"] = projected_steps
    return {
        "projection_version": PROJECTION_VERSION,
        "manager_trace": manager_trace,
        "mapping": {
            "projection_version": PROJECTION_VERSION,
            "raw_step_count": len(raw_steps),
            "projected_step_count": len(projected_steps),
            "raw_to_projected_steps": mappings,
            "strict_duplicate_fields_omitted": removed_total,
            "nonduplicate_fields_preserved": preserved_total,
            "top_level_omissions": top_level_omissions,
            "top_level_fields_retained": sorted(manager_trace),
            "integrity": {
                **counts,
                "content_block_count_preserved": True,
                "source_truncation": copy.deepcopy(trace.get("truncation")),
            },
        },
    }
