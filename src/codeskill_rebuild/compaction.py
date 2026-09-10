"""Evidence-preserving fallback for a manager request that exceeds its budget."""

from __future__ import annotations

from typing import Any, Callable

from .manager_projection import assistant_tool_calls, tool_result_call_id


class EvidenceCompactionError(ValueError):
    pass


def action_observation_segments(
    steps: list[dict[str, Any]],
    *,
    message_count: Callable[[list[dict[str, Any]]], int],
    messages_for_segment: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    max_input_tokens: int,
) -> list[dict[str, Any]]:
    """Split only after complete tool batches, using the real message counter."""
    if max_input_tokens <= 0:
        raise EvidenceCompactionError("max_input_tokens must be positive")
    safe_boundaries: list[int] = []
    pending_tools: set[str] = set()
    for index, step in enumerate(steps, start=1):
        if step.get("role") == "assistant":
            for call in assistant_tool_calls(step):
                call_id = call.get("tool_call_id")
                if isinstance(call_id, str):
                    pending_tools.add(call_id)
        elif step.get("role") == "toolResult":
            call_id = tool_result_call_id(step)
            if isinstance(call_id, str):
                pending_tools.discard(call_id)
        if not pending_tools:
            safe_boundaries.append(index)
    if pending_tools or not safe_boundaries or safe_boundaries[-1] != len(steps):
        raise EvidenceCompactionError("trace has no complete action-observation boundary at its end")

    segments: list[dict[str, Any]] = []
    start = 0
    while start < len(steps):
        fitting: list[tuple[int, int]] = []
        for end in (boundary for boundary in safe_boundaries if boundary > start):
            count = message_count(messages_for_segment(steps[start:end]))
            if count <= max_input_tokens:
                fitting.append((end, count))
            else:
                break
        if not fitting:
            raise EvidenceCompactionError(f"one complete action-observation segment exceeds {max_input_tokens} tokens")
        end, count = fitting[-1]
        segment = steps[start:end]
        segments.append(
            {
                "step_ids": [str(step["source_entry_id"]) for step in segment],
                "steps": segment,
                "exact_input_tokens": count,
                "range": {"start_index": start, "end_exclusive": end},
            }
        )
        start = end
    return segments


def expand_evidence_fragments(steps: list[dict[str, Any]], evidence_step_ids: list[str]) -> list[dict[str, Any]]:
    """Keep cited entries and their matching tool calls/results in source order."""
    by_id = {str(step.get("source_entry_id")): step for step in steps if step.get("source_entry_id") is not None}
    if len(by_id) != len(steps):
        raise EvidenceCompactionError("steps require unique source_entry_id values")
    call_to_assistant: dict[str, str] = {}
    call_to_result: dict[str, str] = {}
    for entry_id, step in by_id.items():
        if step.get("role") == "assistant":
            for call in assistant_tool_calls(step):
                call_id = call.get("tool_call_id")
                if isinstance(call_id, str):
                    call_to_assistant[call_id] = entry_id
        elif step.get("role") == "toolResult":
            call_id = tool_result_call_id(step)
            if isinstance(call_id, str):
                call_to_result[call_id] = entry_id
    selected = set(evidence_step_ids)
    if not selected <= set(by_id):
        raise EvidenceCompactionError("summary cites an unknown source step")
    changed = True
    while changed:
        changed = False
        for entry_id in list(selected):
            step = by_id[entry_id]
            if step.get("role") == "assistant":
                call_ids = [call["tool_call_id"] for call in assistant_tool_calls(step) if isinstance(call.get("tool_call_id"), str)]
            elif step.get("role") == "toolResult":
                call_id = tool_result_call_id(step)
                call_ids = [call_id] if isinstance(call_id, str) else []
            else:
                call_ids = []
            for call_id in call_ids:
                for related in (call_to_assistant.get(call_id), call_to_result.get(call_id)):
                    if related is not None and related not in selected:
                        selected.add(related)
                        changed = True
    return [step for step in steps if str(step.get("source_entry_id")) in selected]


def complete_tool_pair_count(steps: list[dict[str, Any]]) -> int:
    """Count complete original tool-call/result pairs in a supplied fragment."""
    calls: set[str] = set()
    results: set[str] = set()
    for step in steps:
        if step.get("role") == "assistant":
            calls.update(
                str(call["tool_call_id"])
                for call in assistant_tool_calls(step)
                if isinstance(call.get("tool_call_id"), str)
            )
        elif step.get("role") == "toolResult":
            call_id = tool_result_call_id(step)
            if isinstance(call_id, str):
                results.add(call_id)
    if calls != results:
        raise EvidenceCompactionError("verbatim fragment has an incomplete action-observation pair")
    return len(calls)
