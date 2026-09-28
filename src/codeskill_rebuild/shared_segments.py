"""Original trajectory segments shared by Task and Event extraction."""

from __future__ import annotations

from typing import Any

from .compaction import action_observation_segments
from .types import canonical_json


TASK_PROMPT = "custom/r015_task_candidate_from_segment.md"
EVENT_PROMPT = "custom/r015_event_from_segment.md"


def segment_messages(trace: dict[str, Any], steps: list[dict[str, Any]],
                     prompt: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": prompt},
            {"role": "user", "content": canonical_json({
                "source": trace["source"],
                "task_context": trace["instruction"],
                "outcome": trace.get("outcome"),
                "segment_steps": steps,
            })}]


def original_steps(trace: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the ordered original steps visible under the thinking policy."""
    compacted = trace.get("historical_compaction", {})
    raw = compacted.get("raw_message_step_ids")
    raw_ids = set(raw) if isinstance(raw, list) else None
    controls = set(compacted.get("control_event_ids", []))
    return [step for step in trace["steps"] if step["source_entry_id"] not in controls
            and (raw_ids is None or step["source_entry_id"] in raw_ids)]


def request_input_tokens(trace: dict[str, Any], steps: list[dict[str, Any]], *,
                         chat: Any, prompt: str, schema: dict[str, Any]) -> int:
    return chat.token_counter(segment_messages(trace, steps, prompt),
        request_options={"model": chat.model, "temperature": 0,
            "max_tokens": chat.output_tokens, "reasoning_effort": "max",
            "response_format": {"type": "json_schema", "json_schema": schema}})


def whole_original_segment(trace: dict[str, Any], *, chat: Any,
                           prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
    steps = original_steps(trace)
    if not steps:
        raise ValueError("original trajectory has no visible steps")
    count = request_input_tokens(trace, steps, chat=chat, prompt=prompt, schema=schema)
    if count > chat.allowance:
        raise ValueError(f"whole original trajectory exceeds input allowance: {count} > {chat.allowance}")
    return {"step_ids": [str(step["source_entry_id"]) for step in steps],
            "steps": steps, "exact_input_tokens": count,
            "range": {"start_index": 0, "end_exclusive": len(steps)}}


def prepare_original_segments(trace: dict[str, Any], *, chat: Any,
                              prompt: str, schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Use the whole original request when it fits; otherwise pack complete batches."""
    steps = original_steps(trace)
    if not steps:
        raise ValueError("original trajectory has no visible steps")

    def count(steps: list[dict[str, Any]]) -> int:
        return request_input_tokens(trace, steps, chat=chat, prompt=prompt, schema=schema)

    full_count = count(steps)
    if full_count <= chat.allowance:
        return [{"step_ids": [str(step["source_entry_id"]) for step in steps],
                 "steps": steps, "exact_input_tokens": full_count,
                 "range": {"start_index": 0, "end_exclusive": len(steps)}}]
    return action_observation_segments(steps, message_count=count,
        messages_for_segment=lambda steps: steps,
        max_input_tokens=chat.allowance)
