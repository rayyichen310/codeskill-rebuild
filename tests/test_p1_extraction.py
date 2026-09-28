from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_p1_extraction as p1  # noqa: E402


def trajectory(task: str, command: str) -> dict[str, Any]:
    return {
        "outcome": {"official_reward": "1"},
        "steps": [
            {"role": "user", "content": [{"type": "text", "text": f"[Mon 2026-09-28 02:50 UTC] {task}"}]},
            {"role": "assistant", "content": [{"type": "tool_call", "tool_call_id": "a", "tool_name": "exec",
                                               "partial_arguments": json.dumps({"command": command})}]},
            {"role": "toolResult", "content": [{"type": "text", "text": "sh: xxd: not found"}],
             "tool_result": {"tool_call_id": "a", "details": {"exitCode": 127}}},
        ],
    }


def generate(granularity: str, title: str, when: str, steps: Any) -> dict[str, Any]:
    return {"action": "generate", "evidence_steps": steps,
            "skill": {"title": title, "granularity": granularity, "when_to_apply": when, "rules": ["Use od -c instead."]}}


class ScriptedCaller:
    """Answers by call purpose; records every request for inspection."""

    replies: dict[str, dict[str, Any]] = {}
    requests: list[tuple[str, list[dict[str, str]]]] = []

    def __init__(self, directory: Path, profile: Any, contract: Any) -> None:
        pass

    def __call__(self, purpose: str, messages: list[dict[str, str]]) -> dict[str, Any]:
        self.requests.append((purpose, messages))
        value = self.replies[purpose]
        return {"call_id": purpose, "json": value, "content": json.dumps(value)}


class FakeEncoder:
    def index_skill(self, skill: dict[str, Any]) -> tuple[list[float], dict[str, Any]]:
        return [1.0, float(len(skill["title"]))], {}


class P1ExtractionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.original = p1.Caller
        p1.Caller = ScriptedCaller
        ScriptedCaller.requests = []

    def tearDown(self) -> None:
        p1.Caller = self.original

    def test_events_lint_pairing_task_and_maintenance(self) -> None:
        run = {"run": "tb2-r1", "benchmark": "terminal-bench"}
        first = {"task": "a-task", "trajectory": trajectory("Decode a file.", "xxd f"), "vector": [1.0, 0.0]}
        second = {"task": "b-task", "trajectory": trajectory("Inspect bytes.", "xxd g"), "vector": [1.0, 0.1]}
        ScriptedCaller.replies = {
            "tb2-r1:b-task:event-1": generate("event-driven", "Leaky", "When `self._decode()` fails", [1]),
            "tb2-r1:b-task:event-1-lint-revision": generate("event-driven", "Fallback when xxd is missing", "When xxd is not found", [1]),
            "tb2-r1:b-task:event-2": generate("event-driven", "Second", "When a command exits 127", [9]),
            "tb2-r1:b-task:event-3": {"action": "skip", "reason": "none left"},
            "tb2-r1:b-task:pairing": {"action": "select", "selected": ["C1"], "shared_approach": "x", "reason": "y"},
            "tb2-r1:b-task:task": generate("general", "Inspect binary files", "When a task needs raw bytes", {"T1": [1], "T2": [1]}),
        }
        tpl = p1.templates()
        with tempfile.TemporaryDirectory() as tmp:
            record = p1.extract_unit(second, run, [first], Path(tmp), None, {}, tpl, frozenset())
        self.assertEqual([e["outcome"] for e in record["events"]], ["candidate", "candidate", "skip"])
        titles = [c["title"] for c in record["candidates"]]
        self.assertEqual(titles, ["Fallback when xxd is missing", "Second", "Inspect binary files"])
        self.assertEqual([c["evidence_valid"] for c in record["candidates"]], [True, False, True])
        self.assertEqual(record["candidates"][2]["source_instance_ids"], ["b-task", "a-task"])
        purposes = [p for p, _ in ScriptedCaller.requests]
        repeat = dict(ScriptedCaller.requests)["tb2-r1:b-task:event-2"][1]["content"]
        self.assertIn("Choose a different event", repeat)
        self.assertIn('"title": "Leaky"', repeat)
        revision = dict(ScriptedCaller.requests)["tb2-r1:b-task:event-1-lint-revision"]
        self.assertEqual(revision[-2]["role"], "assistant")
        self.assertIn("code_symbol `self._decode()`", revision[-1]["content"])
        self.assertIn("COMMANDS:\nS1: xxd g  [exit 127]", dict(ScriptedCaller.requests)["tb2-r1:b-task:pairing"][1]["content"])
        self.assertEqual(purposes[-1], "tb2-r1:b-task:task")

        candidates = record["candidates"]
        ScriptedCaller.replies = {
            "tb2-r1:maintenance-001": {"action": "add", "reason": "new"},
            "tb2-r1:maintenance-002": {"action": "merge", "merge_target_skill_id": None, "reason": "same",
                                       "skill": {"title": "Merged", "granularity": "event-driven", "when_to_apply": "When xxd or od is missing", "rules": ["r"]}},
            "tb2-r1:maintenance-003": {"action": "drop", "reason": "covered"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            first_bank, _ = p1.maintain(run, candidates[:1], Path(tmp), None, {}, tpl, frozenset(), FakeEncoder(), threading.Lock())
            ScriptedCaller.replies["tb2-r1:maintenance-002"]["merge_target_skill_id"] = first_bank[0]["skill_id"]
            bank, log = p1.maintain(run, candidates, Path(tmp), None, {}, tpl, frozenset(), FakeEncoder(), threading.Lock())
        self.assertEqual([e["action"] for e in log], ["add", "merge", "drop"])
        self.assertEqual([(s["title"], s["status"], s["version"]) for s in bank],
                         [("Fallback when xxd is missing", "superseded", 1), ("Merged", "active", 2)])
        self.assertEqual(bank[1]["provenance"]["parent_skill_ids"], [bank[0]["skill_id"]])
        self.assertEqual(bank[1]["granularity"], "event")

    def test_unreachable_tokenizer_marks_only_that_trajectory_failed(self) -> None:
        from codeskill_rebuild.context import ContextBlocked

        class DownCaller(ScriptedCaller):
            def __call__(self, purpose: str, messages: list[dict[str, str]]) -> dict[str, Any]:
                raise ContextBlocked('{"state": "tokenizer_unavailable"}')

        p1.Caller = DownCaller
        unit = {"task": "a-task", "trajectory": trajectory("Decode a file.", "xxd f"), "vector": [1.0, 0.0]}
        with tempfile.TemporaryDirectory() as tmp:
            record = p1.extract_unit(unit, {"run": "tb2-r1", "benchmark": "terminal-bench"}, [], Path(tmp), None, {}, p1.templates(), frozenset())
        self.assertIn("tokenizer_unavailable", record["failed"])


if __name__ == "__main__":
    unittest.main()
