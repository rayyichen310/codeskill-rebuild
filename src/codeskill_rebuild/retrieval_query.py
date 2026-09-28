"""P2 retrieval query construction (docs/DECISIONS.md P2, EXPERIMENTS §3).

The live R013 query used the first user message plus the OpenClaw system
prompt for task retrieval and the head of the tool output for event retrieval,
so harness boilerplate dominated both.  These helpers keep only the problem
statement, the error lines plus the tail of the tool output, the plain command,
and the solver's own reasoning.  The sidecar and the offline evaluation share
them so the evaluated query is the deployed query.
"""

from __future__ import annotations

import json
import re
from typing import Any

QUERY_CONSTRUCTION = "p2-20260928"

ERROR_RE = re.compile(
    r"(?i)(traceback|exception|\berror\b|error:|fatal:|failed|failure|not found|no such file|"
    r"permission denied|cannot |unable to|refused|timed out|exited with code [1-9])"
)
_SWE_INSTANCE = re.compile(r"^[\w.-]+__[\w.-]+-\d+$")


def message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(item["text"] for item in content if isinstance(item, dict) and isinstance(item.get("text"), str))
    return ""


def problem_statement(first_user_text: str) -> str:
    """The issue body for the SWE harness wrapper; otherwise the user text without OpenClaw's timestamp."""
    match = re.search(r"<issue_description>(.*?)</issue_description>", first_user_text, re.S)
    if match:
        return match.group(1).strip()
    return re.sub(r"^\[[^\]]*UTC\]\s*", "", first_user_text).strip()


def repo_context(instance_id: str) -> str:
    """SWE-bench instance IDs name the repository; Terminal-Bench tasks carry no repo context."""
    if _SWE_INSTANCE.match(instance_id):
        return instance_id.rsplit("-", 1)[0].replace("__", "/")
    return ""


def plain_action(assistant: dict[str, Any]) -> str:
    lines = []
    for call in assistant.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            args = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"raw": function.get("arguments")}
        if isinstance(args, dict) and "command" in args:
            lines.append(str(args["command"]))
        else:
            lines.append(str(function.get("name", "")) + " " + json.dumps(args, ensure_ascii=False)[:300])
    return "\n".join(lines)


def observation(results: list[dict[str, Any]]) -> str:
    return "\n".join(message_text(message) for message in results)


def error_lines_then_tail(text: str, tail_chars: int = 600, max_lines: int = 5) -> str:
    errors: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line and ERROR_RE.search(line) and line not in errors:
            errors.append(line[:200])
    return "\n".join(errors[-max_lines:] + [text[-tail_chars:]])


def task_query_fields(first_user_text: str, instance_id: str) -> dict[str, str]:
    return {"goal_problem": problem_statement(first_user_text), "repo_context": repo_context(instance_id)}


def event_query_fields(first_user_text: str, assistant: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, str]:
    reasoning = assistant.get("reasoning_content") or ""
    return {
        "observation_errors_tests": error_lines_then_tail(observation(results)),
        "recent_action": plain_action(assistant),
        "public_reasoning": reasoning[-600:],
        "task_context": problem_statement(first_user_text),
    }


def judge_state(first_user_text: str, assistant: dict[str, Any] | None, results: list[dict[str, Any]]) -> dict[str, str]:
    """Compact situation for the relevance judge; task selection has no action yet."""
    state = {"task": problem_statement(first_user_text)[:1500]}
    if assistant is not None:
        reasoning = assistant.get("reasoning_content") or ""
        state.update({
            "agent_reasoning": reasoning[-600:],
            "last_action": plain_action(assistant)[:600],
            "tool_output": error_lines_then_tail(observation(results), tail_chars=1500),
        })
    return state
