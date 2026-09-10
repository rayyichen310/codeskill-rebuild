"""Import authorized OpenClaw sessions without depending on legacy CODESKILL."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from .types import canonical_instance_id, canonical_json, sha256_file, sha256_text, utc_now

class TraceImportError(ValueError):
    pass


def _text_block(value: Any) -> str:
    """Keep visible task evidence byte-for-byte; no broad credential regex."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("thinking"), str):
            return value["thinking"]
    return ""


def _image_metadata(block: dict[str, Any]) -> dict[str, Any]:
    value = block.get("data") or block.get("image") or block.get("image_url") or block.get("url")
    raw: bytes | None = None
    if isinstance(value, str) and value.startswith("data:") and "," in value:
        try:
            raw = base64.b64decode(value.split(",", 1)[1], validate=False)
        except ValueError:
            raw = value.encode("utf-8")
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    return {
        "block_type": block.get("type", "unknown"),
        "mime_type": block.get("mimeType") or block.get("media_type"),
        "sha256": hashlib.sha256(raw or b"").hexdigest(),
        "byte_length": len(raw or b""),
        "content_preserved": False,
        "status": "multimodal_pending",
    }


def _content_items(content: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if isinstance(content, str):
        return ([{"type": "text", "text": content}], [])
    if not isinstance(content, list):
        return ([], [])
    items: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "thinking":
            items.append({"type": "thinking", "text": _text_block(block)})
        elif kind == "text":
            items.append({"type": "text", "text": _text_block(block)})
        elif kind == "toolCall":
            items.append(
                {
                    "type": "tool_call",
                    "tool_call_id": block.get("id"),
                    "tool_name": block.get("name"),
                    "arguments": block.get("arguments"),
                    "partial_arguments": block.get("partialArgs"),
                }
            )
        elif kind in {"image", "image_url", "input_image"}:
            image = _image_metadata(block)
            images.append(image)
            items.append({"type": "image_reference", **image})
        else:
            items.append({"type": str(kind or "unknown"), "raw": block})
    return items, images


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TraceImportError(f"Cannot parse {path}: {error}") from error


def _validate_parent_graph(events: list[dict[str, Any]]) -> dict[str, str | None]:
    parents: dict[str, str | None] = {}
    for event in events:
        entry_id = event.get("id")
        parent_id = event.get("parentId")
        if entry_id is None:
            if parent_id is not None:
                raise TraceImportError("Entry has parentId but no id")
            continue
        if not isinstance(entry_id, str):
            raise TraceImportError("Entry id is not a string")
        if entry_id in parents:
            raise TraceImportError(f"Duplicate entry id: {entry_id}")
        if parent_id is not None and not isinstance(parent_id, str):
            raise TraceImportError(f"parentId is not a string for {entry_id}")
        parents[entry_id] = parent_id
    for entry_id, parent_id in parents.items():
        if parent_id is not None and parent_id not in parents:
            raise TraceImportError(f"Unknown parent {parent_id} referenced by {entry_id}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(entry_id: str) -> None:
        if entry_id in visited:
            return
        if entry_id in visiting:
            raise TraceImportError(f"Cycle in session parent graph at {entry_id}")
        visiting.add(entry_id)
        parent_id = parents[entry_id]
        if parent_id is not None:
            walk(parent_id)
        visiting.remove(entry_id)
        visited.add(entry_id)

    for entry_id in parents:
        walk(entry_id)
    return parents


def _outcome_summary(result: Any, reward: str | None) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"official_reward": reward, "result_present": False}
    allowed = ("finished_at", "started_at", "duration", "duration_seconds", "trial_exception", "exception", "status")
    summary = {key: result[key] for key in allowed if key in result}
    summary["official_reward"] = reward
    summary["result_present"] = True
    return summary


def normalize_openclaw_trial(trial_dir: Path, *, expected_session_sha256: str | None = None) -> dict[str, Any]:
    """Normalize one authorized raw OpenClaw trial.

    ``openclaw.session.jsonl`` is authoritative because ATIF may replace an
    assistant message with a placeholder.  The function intentionally does
    not read run.env, full effective configuration, verifier internals, or
    legacy CODESKILL artifacts.
    """

    trial_dir = Path(trial_dir)
    # The authorized baseline lives under a historical ``summary-spine``
    # parent directory.  Only the separate Spine B run itself is disallowed.
    if any("spineb" in part.lower() for part in trial_dir.parts):
        raise TraceImportError("Spine traces are not permitted as v1 primary source")
    session_path = trial_dir / "agent" / "openclaw.session.jsonl"
    instruction_path = trial_dir / "agent" / "instruction.txt"
    config_path = trial_dir / "config.json"
    result_path = trial_dir / "result.json"
    reward_path = trial_dir / "verifier" / "reward.txt"
    if not session_path.is_file() or not instruction_path.is_file() or not config_path.is_file():
        raise TraceImportError(f"Incomplete raw trial at {trial_dir}")
    actual_hash = sha256_file(session_path)
    if expected_session_sha256 and expected_session_sha256 != actual_hash:
        raise TraceImportError("Raw session hash differs from approved manifest")
    config = _load_json(config_path)
    task = config.get("task", {})
    task_name = task.get("name")
    if not isinstance(task_name, str):
        raise TraceImportError("Trial config has no task.name")

    raw_events: list[dict[str, Any]] = []
    for lineno, line in enumerate(session_path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            raw_events.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise TraceImportError(f"Invalid JSONL at line {lineno}: {error}") from error
    parents = _validate_parent_graph(raw_events)
    entries: list[dict[str, Any]] = []
    control_events: list[dict[str, Any]] = []
    image_blocks: list[dict[str, Any]] = []
    tool_calls: dict[str, dict[str, Any]] = {}
    tool_results: dict[str, dict[str, Any]] = {}
    for event in raw_events:
        entry_id = event.get("id")
        event_type = event.get("type")
        if event_type != "message":
            control_events.append(
                {
                    "source_entry_id": entry_id,
                    "source_parent_id": event.get("parentId"),
                    "timestamp": event.get("timestamp"),
                    "type": event_type,
                    "custom_type": event.get("customType"),
                    "data": event.get("data"),
                    # Retain the authoritative native control record exactly
                    # as it appeared in the source JSONL.  It is audit data,
                    # never a manager-visible observed step.
                    "raw_event": event,
                    "raw_event_sha256": sha256_text(canonical_json(event)),
                }
            )
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            raise TraceImportError(f"Message entry {entry_id} has no message object")
        role = message.get("role")
        if not isinstance(role, str):
            raise TraceImportError(f"Message entry {entry_id} has no role")
        content, images = _content_items(message.get("content"))
        image_blocks.extend(images)
        normalized = {
            "source_entry_id": entry_id,
            "source_parent_id": event.get("parentId"),
            "timestamp": event.get("timestamp"),
            "role": role,
            "content": content,
            "stop_reason": message.get("stopReason", event.get("stopReason")),
            "usage": message.get("usage", event.get("usage")),
        }
        if role == "toolResult":
            details = message.get("details") if isinstance(message.get("details"), dict) else {}
            normalized["tool_result"] = {
                "tool_call_id": message.get("toolCallId"),
                "tool_name": message.get("toolName"),
                "is_error": bool(message.get("isError")),
                "details": details,
            }
            tool_call_id = message.get("toolCallId")
            if not isinstance(tool_call_id, str):
                raise TraceImportError(f"Tool result {entry_id} has no toolCallId")
            if tool_call_id in tool_results:
                raise TraceImportError(f"Duplicate tool result for {tool_call_id}")
            tool_results[tool_call_id] = normalized
        if role == "assistant":
            calls = [item for item in content if item.get("type") == "tool_call"]
            for call in calls:
                call_id = call.get("tool_call_id")
                if not isinstance(call_id, str):
                    raise TraceImportError(f"Tool call in {entry_id} has no id")
                if call_id in tool_calls:
                    raise TraceImportError(f"Duplicate tool call id: {call_id}")
                tool_calls[call_id] = call
            normalized["assistant"] = {
                "thinking": [item.get("text", "") for item in content if item.get("type") == "thinking"],
                "text": [item.get("text", "") for item in content if item.get("type") == "text"],
                "tool_calls": calls,
            }
        entries.append(normalized)

    missing_results = sorted(set(tool_calls) - set(tool_results))
    if missing_results:
        raise TraceImportError(f"Missing tool results for calls: {missing_results}")
    orphan_results = sorted(set(tool_results) - set(tool_calls))
    if orphan_results:
        raise TraceImportError(f"Tool results refer to unknown calls: {orphan_results}")
    result = _load_json(result_path) if result_path.is_file() else None
    reward = reward_path.read_text(encoding="utf-8").strip() if reward_path.is_file() else None
    truncated_markers = [
        entry["source_entry_id"]
        for entry in entries
        if "truncat" in json.dumps(entry, ensure_ascii=False).lower()
    ]
    # A native compaction control record may contain text that describes old
    # history, but it is not an observed source message.  Preserve the raw
    # control payload for audit while making the evidence boundary explicit:
    # only entries actually present in the authorized raw session can be
    # cited.  We deliberately do not reconstruct vanished messages from a
    # summary.
    compaction_control_ids = [
        str(event["source_entry_id"])
        for event in control_events
        if (
            event.get("type") == "compaction"
            or str(event.get("custom_type", "")).casefold() == "compaction"
        )
        and event.get("source_entry_id") is not None
    ]
    raw_message_step_ids = [str(entry["source_entry_id"]) for entry in entries]
    return {
        "schema_version": 2,
        "kind": "normalized_openclaw_trace",
        "imported_at_utc": utc_now(),
        "historical": True,
        "source": {
            "trial_path": str(trial_dir),
            "session_path": str(session_path),
            "session_sha256": actual_hash,
            "instruction_sha256": sha256_file(instruction_path),
            "task_name": task_name,
            "official_task_name": task_name,
            "task_ref": task.get("ref"),
            "dataset_source": task.get("source"),
            "instance_id": canonical_instance_id(task_name),
            "canonical_instance_id": canonical_instance_id(task_name),
        },
        "instruction": instruction_path.read_text(encoding="utf-8"),
        "entries": entries,
        "steps": entries,
        "control_events": control_events,
        "historical_compaction": {
            "control_event_ids": compaction_control_ids,
            "raw_message_step_ids": raw_message_step_ids,
            "summary_is_not_observed_evidence": True,
            "missing_raw_history_policy": "do_not_reconstruct; generated claims without a cited raw_message_step_id are unsupported and must be rejected",
        },
        "tool_pairing": {
            "tool_call_ids": sorted(tool_calls),
            "tool_result_ids": sorted(tool_results),
            "missing_results": missing_results,
            "orphan_results": orphan_results,
        },
        "session_parent_map": parents,
        "outcome": _outcome_summary(result, reward),
        "multimodal": {"present": bool(image_blocks), "blocks": image_blocks},
        "text_manager_eligible": not image_blocks,
        "truncation": {"source_markers": truncated_markers, "status": "observed" if truncated_markers else "not_observed"},
        "credential_handling": {"run_env_imported": False, "content_redacted": False, "policy": "Visible task evidence is preserved; run.env and endpoint credentials are excluded."},
    }


def write_normalized_trace(trace: dict[str, Any], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def source_step_ids(trace: dict[str, Any]) -> list[str]:
    return [str(step["source_entry_id"]) for step in trace.get("steps", []) if step.get("source_entry_id")]
