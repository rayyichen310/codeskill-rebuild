"""Post-retrieval relevance stage using TypeSafe Jev (docs/DECISIONS.md P2).

MiniLM shortlists skills; Jev picks the one whose when_to_apply matches the
current situation, or none.  The paper has no such stage (PAPER_ALIGNMENT, D).
The question wording is part of the method and is versioned by JUDGE_PROMPT.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

JUDGE_PROMPT = "jev-choice-20260928"

EVENT_CHOICE = (
    "A coding agent just ran `last_action` and got `tool_output` while working on `task`. "
    "Which skill's condition is met by this situation right now, so that showing the skill "
    "would help the agent's next step? Answer none if no skill's condition is met."
)
EVENT_NOUL = (
    "The agent's current situation (`last_action` and `tool_output`, for `task`) meets the "
    "condition `when_to_apply`, so this skill should be shown to the agent now."
)
TASK_CHOICE = (
    "A coding agent is about to start `task`. Which skill's condition is met by this task, so that "
    "showing the skill at the start would help the agent? Answer none if no skill's condition is met."
)
TASK_NOUL = "The task `task` meets the condition `when_to_apply`, so this skill should be shown to the agent at the start."

_TRANSIENT = {408, 409, 429, 500, 502, 503, 504}


class JudgeConfigError(ValueError):
    """The judge cannot run as configured (bad key, schema, or model); never retried."""


def judge_request(phase: str, state: dict[str, str], shortlist: list[dict[str, Any]], model: str) -> dict[str, Any]:
    choice_text, noul_text = (EVENT_CHOICE, EVENT_NOUL) if phase == "event" else (TASK_CHOICE, TASK_NOUL)
    criteria: dict[str, Any] = {
        f"skill_{i}": {"title": s["title"], "when_to_apply": s["when_to_apply"]} for i, s in enumerate(shortlist)
    }
    criteria["none"] = "No listed skill's condition is met by the current situation."
    questions: dict[str, Any] = {"pick": {"type": "choice", "instructions": choice_text, "criteria": criteria}}
    for i, s in enumerate(shortlist):
        questions[f"fits_{i}"] = {"type": "noul", "instructions": {"when_to_apply": s["when_to_apply"], "question": noul_text}}
    return {"state": state, "model": model, "questions": questions}


def _httpx_transport(endpoint: str, api_key: str, timeout: float) -> Callable[[dict[str, Any]], tuple[int, dict[str, Any], dict[str, str]]]:
    import httpx

    def send(body: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, str]]:
        try:
            response = httpx.post(endpoint, json=body, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
        except httpx.TransportError as error:  # timeouts and connection failures are transient
            raise ConnectionError(f"{type(error).__name__}: {error}") from error
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text[:500]}
        return response.status_code, payload, dict(response.headers)

    return send


@dataclass
class JevJudge:
    model: str
    endpoint: str
    api_key_env: str
    shortlist: int
    timeout_seconds: float
    max_attempts: int
    transport: Callable[[dict[str, Any]], tuple[int, dict[str, Any], dict[str, str]]] | None = None
    sleep: Callable[[float], None] = field(default=time.sleep)

    @classmethod
    def from_config(cls, config: dict[str, Any], **overrides: Any) -> "JevJudge":
        if config.get("kind") != "typesafe_jev":
            raise JudgeConfigError("relevance judge kind must be typesafe_jev")
        if config.get("prompt") != JUDGE_PROMPT:
            raise JudgeConfigError(f"relevance judge prompt must be {JUDGE_PROMPT}")
        try:
            return cls(
                model=str(config["model"]),
                endpoint=str(config["endpoint"]),
                api_key_env=str(config["apiKeyEnv"]),
                shortlist=int(config["shortlist"]),
                timeout_seconds=float(config["timeoutSeconds"]),
                max_attempts=int(config["maxAttempts"]),
                **overrides,
            )
        except KeyError as missing:
            raise JudgeConfigError(f"relevance judge config needs {missing.args[0]}") from None

    def _send(self, body: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, str]]:
        if self.transport is None:
            api_key = os.environ.get(self.api_key_env)
            if not api_key:
                raise JudgeConfigError(f"relevance judge needs the {self.api_key_env} environment variable")
            self.transport = _httpx_transport(self.endpoint, api_key, self.timeout_seconds)
        return self.transport(body)

    def decide(self, phase: str, state: dict[str, str], shortlist: list[dict[str, Any]]) -> dict[str, Any]:
        """Return the picked shortlist index (or None) with the raw answer for the audit record."""
        body = judge_request(phase, state, shortlist, self.model)
        record: dict[str, Any] = {"prompt": JUDGE_PROMPT, "request": body, "attempts": []}
        for attempt in range(self.max_attempts):
            started = time.monotonic()
            try:
                status, payload, headers = self._send(body)
            except OSError as error:  # connection failure or timeout
                record["attempts"].append({"error": str(error)[:200], "latency_s": time.monotonic() - started})
                self.sleep(2 ** attempt)
                continue
            latency = time.monotonic() - started
            record["attempts"].append({"status": status, "latency_s": latency})
            if status in _TRANSIENT:
                self.sleep(float(headers.get("retry-after", 2 ** attempt)))
                continue
            if status != 200:
                raise JudgeConfigError(f"relevance judge returned HTTP {status}: {str(payload)[:300]}")
            choice = payload["answers"]["pick"]["choice"]
            index = None if choice == "none" else int(choice.split("_", 1)[1])
            if index is not None and not 0 <= index < len(shortlist):
                raise JudgeConfigError(f"relevance judge chose an option outside the shortlist: {choice}")
            record.update({"status": "ok", "model": payload.get("model"), "answers": payload["answers"], "choice_index": index})
            return record
        record.update({"status": "judge_failed", "choice_index": None})
        return record
