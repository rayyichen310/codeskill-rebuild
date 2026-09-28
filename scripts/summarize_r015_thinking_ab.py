#!/usr/bin/env python3
"""Create a compact, evidence-linked summary from two completed thinking A/B arms."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def strip_reasoning(value: Any) -> Any:
    if isinstance(value, list):
        result = []
        for item in value:
            if (
                isinstance(item, dict)
                and item.get("type") in {"thinking", "reasoning"}
                and isinstance(item.get("text"), str)
                and set(item).issubset({"type", "text"})
            ):
                continue
            result.append(strip_reasoning(item))
        return result
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in {"thinking", "reasoning"} and (
                isinstance(item, str)
                or (isinstance(item, list) and all(isinstance(part, str) for part in item))
            ):
                continue
            result[key] = strip_reasoning(item)
        return result
    return value


def normalized_messages(request_path: Path, *, strip: bool) -> list[dict[str, Any]]:
    record = read_json(request_path)
    messages = record["request"]["messages"]
    normalized = []
    for message in messages:
        item = dict(message)
        content = item.get("content")
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                pass
            else:
                item["content"] = strip_reasoning(parsed) if strip else parsed
        normalized.append(item)
    return strip_reasoning(normalized) if strip else normalized


def status_summary(task: dict[str, Any]) -> dict[str, Any]:
    task_part = task.get("task_candidate")
    event = task.get("event")
    return {
        "task_candidate_status": task_part.get("evidence", {}).get("status") if isinstance(task_part, dict) else None,
        "task_candidate_count": len(task_part.get("candidates", [])) if isinstance(task_part, dict) else 0,
        "event_decision": event.get("evidence", {}).get("decision") if isinstance(event, dict) else None,
        "event_attempt_outcomes": [item.get("outcome") for item in event.get("attempts", [])] if isinstance(event, dict) else [],
        "event_candidate_count": len(event.get("candidates", [])) if isinstance(event, dict) else 0,
    }


def context_summary(arm_root: Path, task_id: str, phase: str) -> dict[str, Any] | None:
    path = arm_root / "tasks" / task_id / "manager-context" / f"{phase}.json"
    if not path.is_file():
        return None
    value = read_json(path)
    return {
        "path": str(path.resolve()),
        "historical_thinking_policy": value.get("historical_thinking_policy"),
        "trajectory_input_mode": value.get("trajectory_input_mode"),
        "original_full_request_tokens": value.get("original_full_request_tokens"),
        "deduplicated_request_tokens": value.get("deduplicated_request_tokens"),
        "forwarded_request_tokens": value.get("forwarded_request_tokens"),
        "state": value.get("state"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", required=True)
    parser.add_argument("--exclude", required=True)
    parser.add_argument("--comparison", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    keep_path = Path(args.keep).resolve()
    exclude_path = Path(args.exclude).resolve()
    keep = read_json(keep_path)
    exclude = read_json(exclude_path)
    comparison = read_json(Path(args.comparison).resolve())
    if keep.get("status") != "complete" or exclude.get("status") != "complete":
        raise ValueError("both arms must be complete")
    if len(keep["manager_calls"]) != len(exclude["manager_calls"]):
        raise ValueError("call counts differ; ordinal comparison is unsafe")

    per_call = []
    for keep_call, exclude_call in zip(keep["manager_calls"], exclude["manager_calls"], strict=True):
        keep_request = Path(keep_call["request_path"])
        exclude_request = Path(exclude_call["request_path"])
        keep_usage = keep_call.get("usage") or {}
        exclude_usage = exclude_call.get("usage") or {}
        keep_prompt = keep_usage.get("prompt_tokens")
        exclude_prompt = exclude_usage.get("prompt_tokens")
        if not isinstance(keep_prompt, int) or not isinstance(exclude_prompt, int):
            reduction = None
        else:
            reduction = {
                "tokens": keep_prompt - exclude_prompt,
                "percent_of_keep": ((keep_prompt - exclude_prompt) / keep_prompt * 100.0) if keep_prompt else None,
            }
        per_call.append({
            "ordinal": len(per_call) + 1,
            "keep_call_id": keep_call["call_id"],
            "exclude_call_id": exclude_call["call_id"],
            "keep_purpose": keep_call.get("purpose"),
            "exclude_purpose": exclude_call.get("purpose"),
            "request_options_equal": keep_call.get("request_options") == exclude_call.get("request_options"),
            "system_prompt_equal": normalized_messages(keep_request, strip=False)[0] == normalized_messages(exclude_request, strip=False)[0],
            "messages_equal_after_recognized_thinking_removal": normalized_messages(keep_request, strip=True) == normalized_messages(exclude_request, strip=False),
            "prompt_tokens": {"keep": keep_prompt, "exclude": exclude_prompt, "reduction": reduction},
            "completion_tokens": {"keep": keep_usage.get("completion_tokens"), "exclude": exclude_usage.get("completion_tokens")},
            "reasoning_tokens": {"keep": keep_usage.get("reasoning_tokens"), "exclude": exclude_usage.get("reasoning_tokens")},
            "finish_reason": {"keep": keep_call.get("finish_reason"), "exclude": exclude_call.get("finish_reason")},
            "classification": {"keep": keep_call.get("classification"), "exclude": exclude_call.get("classification")},
            "elapsed_seconds": {"keep": keep_call.get("elapsed_seconds"), "exclude": exclude_call.get("elapsed_seconds")},
        })

    task_ids = ("build-pmars", "fix-git", "git-leak-recovery", "schemelike-metacircular-eval")
    result = {
        "schema_version": 1,
        "kind": "r015_historical_thinking_ab_detailed_summary",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "comparison": comparison,
        "per_task_validation": {
            task_id: {
                "keep": status_summary(keep["results"][task_id]),
                "exclude": status_summary(exclude["results"][task_id]),
            }
            for task_id in task_ids
        },
        "fixed_source_group": {
            "keep": keep["results"].get("s07-fixed-source-group"),
            "exclude": exclude["results"].get("s07-fixed-source-group"),
        },
        "context_paths": {
            task_id: {
                "keep_task": context_summary(keep_path.parent, task_id, "task-sop-candidate"),
                "exclude_task": context_summary(exclude_path.parent, task_id, "task-sop-candidate"),
                "keep_event_001": context_summary(keep_path.parent, task_id, "event-001"),
                "exclude_event_001": context_summary(exclude_path.parent, task_id, "event-001"),
            }
            for task_id in task_ids
        },
        "per_call": per_call,
        "interpretation": {
            "direct_message_equivalence_field": "messages_equal_after_recognized_thinking_removal",
            "false_for_later_event_calls_can_reflect_arm_local_prior_candidates": True,
            "false_for_long_calls_can_reflect_policy_induced_context_path_divergence": True,
            "quality_or_solver_effect_established": False,
        },
    }
    output = Path(args.output).resolve()
    if output.exists():
        raise ValueError(f"refusing overwrite: {output}")
    write_json(output, result)
    print(json.dumps({"output": str(output)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
