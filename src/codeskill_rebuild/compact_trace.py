"""Compact text form of a normalized trajectory for P1 extraction (docs/DECISIONS.md P1, P3).

The paper feeds whole trajectories to Figs. 6-7; raw OpenClaw traces are too long
(median ~18k tokens, max 216k with full thinking), so thinking and long outputs
are cut while every action is kept.  Step numbers are what the model cites in
`evidence_steps`.
"""

from __future__ import annotations

import ast
import json
from typing import Any

from codeskill_rebuild.retrieval_query import problem_statement

THINK_CHARS = 400
OUTPUT_CHARS = 1500
COMMAND_CHARS = 300
COMMAND_KEEP = 40


def cut(text: str, limit: int = OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n…[{len(text) - limit} chars omitted]…\n{text[-half:]}"


def _parts(step: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [x for x in step["content"] if isinstance(x, dict) and x.get("type") == kind]


def _text(step: dict[str, Any], kind: str = "text") -> str:
    return "".join(x.get("text", "") for x in _parts(step, kind)).strip()


def _arguments(call: dict[str, Any]) -> dict[str, Any]:
    raw = call.get("partial_arguments") or call.get("arguments")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return ast.literal_eval(raw)


def action(call: dict[str, Any]) -> str:
    args = _arguments(call)
    if call["tool_name"] == "exec" and "command" in args:
        return args["command"]
    return f"{call['tool_name']} {json.dumps(args, ensure_ascii=False)}"


def official_result(trajectory: dict[str, Any]) -> str:
    reward = trajectory["outcome"]["official_reward"]
    return {"1": "pass", "0": "fail"}[str(reward)]


def _turns(trajectory: dict[str, Any]) -> list[tuple[dict[str, Any], list[tuple[dict[str, Any], dict[str, Any] | None]]]]:
    """Assistant/user steps after the task prompt, each assistant step paired with its call results."""
    results = {s["tool_result"]["tool_call_id"]: s for s in trajectory["steps"] if s["role"] == "toolResult"}
    turns = []
    for step in trajectory["steps"][1:]:
        if step["role"] == "toolResult":
            continue
        calls = [(c, results.get(c["tool_call_id"])) for c in _parts(step, "tool_call")] if step["role"] == "assistant" else []
        turns.append((step, calls))
    return turns


def _result(result: dict[str, Any] | None) -> tuple[str, str]:
    if result is None:
        return "no result", ""
    exit_code = (result["tool_result"].get("details") or {}).get("exitCode")
    label = f"exit {exit_code}" if exit_code is not None else ("error" if result["tool_result"].get("is_error") else "ok")
    body = _text(result) or ("[image]" if _parts(result, "image_reference") else "")
    return label, body


def step_count(trajectory: dict[str, Any]) -> int:
    return len(_turns(trajectory))


def task_statement(trajectory: dict[str, Any]) -> str:
    return problem_statement(_text(trajectory["steps"][0]))


def render(trajectory: dict[str, Any]) -> str:
    lines = [f"TASK:\n{task_statement(trajectory)}", f"OFFICIAL RESULT: {official_result(trajectory)}", ""]
    for number, (step, calls) in enumerate(_turns(trajectory), 1):
        lines.append(f"STEP {number}")
        if step["role"] == "user":
            lines.append(f"USER: {cut(_text(step))}")
            continue
        thinking = _text(step, "thinking")
        if thinking:
            lines.append(f"THINK: {thinking[:THINK_CHARS]}")
        said = _text(step)
        if said:
            lines.append(f"SAY: {cut(said)}")
        numbered = len(calls) > 1
        for i, (call, _) in enumerate(calls, 1):
            lines.append(f"ACTION{f'[{i}]' if numbered else ''}: {action(call)}")
        for i, (_, result) in enumerate(calls, 1):
            label, body = _result(result)
            lines.append(f"RESULT{f'[{i}]' if numbered else ''} ({label}):\n{cut(body)}")
    return "\n".join(lines)


def command_sequence(trajectory: dict[str, Any]) -> str:
    """Actions in order with exit codes and no outputs, for pairing."""
    entries = []
    for number, (_, calls) in enumerate(_turns(trajectory), 1):
        for call, result in calls:
            args = _arguments(call)
            if call["tool_name"] == "write":
                text = f"write {args.get('path', '')} ({len(str(args.get('content', '')))} chars)"
            else:
                text = action(call)
            text = " ".join(text.split())
            if len(text) > COMMAND_CHARS:
                text = text[:COMMAND_CHARS] + "…"
            entries.append(f"S{number}: {text}  [{_result(result)[0]}]")
    if len(entries) > 2 * COMMAND_KEEP:
        omitted = len(entries) - 2 * COMMAND_KEEP
        entries = entries[:COMMAND_KEEP] + [f"…[{omitted} commands omitted]…"] + entries[-COMMAND_KEEP:]
    return "\n".join(entries)
