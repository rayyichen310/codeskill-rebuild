"""Message assembly with auditable task and event prior-knowledge injection."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def render_skill(skill: dict[str, Any]) -> str:
    rules = "\n".join(f"- {rule}" for rule in skill["rules"])
    return "\n".join(
        [
            f"### {skill['title']} ({skill['skill_id']} v{skill['version']})",
            f"When to apply: {skill['when_to_apply']}",
            "Rules:",
            rules,
        ]
    )


def build_initial_messages(initial_user_prompt: str, task_skills: list[dict[str, Any]]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    messages = [{"role": "user", "content": initial_user_prompt}]
    if not task_skills:
        return messages, {"injected": False, "skill_ids": [], "message_index": None}
    block = "[CODESKILL TASK PRIOR KNOWLEDGE]\n" + "\n\n".join(render_skill(skill) for skill in task_skills)
    messages[0]["content"] += "\n\n" + block
    return messages, {"injected": True, "skill_ids": [skill["skill_id"] for skill in task_skills], "message_index": 0, "block": block}


def inject_event_message(messages: list[dict[str, Any]], event_skill: dict[str, Any], already_injected: set[tuple[str, int]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    key = (str(event_skill["skill_id"]), int(event_skill["version"]))
    copied = deepcopy(messages)
    if key in already_injected:
        return copied, {"injected": False, "reason": "already_injected", "skill_id": key[0], "version": key[1]}
    if not copied or copied[-1].get("role") != "tool":
        raise ValueError("Event skills can only be inserted after a tool observation")
    last_assistant_index = next((index for index in range(len(copied) - 1, -1, -1) if copied[index].get("role") == "assistant"), None)
    if last_assistant_index is not None:
        declared = copied[last_assistant_index].get("tool_calls")
        if isinstance(declared, list) and declared:
            expected_ids = {str(call.get("id")) for call in declared if isinstance(call, dict) and call.get("id")}
            resolved_ids = {
                str(message.get("tool_call_id"))
                for message in copied[last_assistant_index + 1 :]
                if message.get("role") == "tool" and message.get("tool_call_id")
            }
            if expected_ids - resolved_ids:
                return copied, {
                    "injected": False,
                    "reason": "tool_batch_incomplete",
                    "pending_tool_call_ids": sorted(expected_ids - resolved_ids),
                }
    block = "[CODESKILL EVENT PRIOR KNOWLEDGE]\n" + render_skill(event_skill)
    copied.append({"role": "user", "content": block})
    return copied, {"injected": True, "skill_id": key[0], "version": key[1], "message_index": len(copied) - 1, "block": block}
