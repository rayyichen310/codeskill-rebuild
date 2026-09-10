from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from codeskill_rebuild.pipeline import validate_event_extraction_with_evidence
from codeskill_rebuild.traces import TraceImportError, normalize_openclaw_trial


def make_trial(root: Path, *, spine: bool = False, image: bool = False) -> Path:
    campaign = "dsv4-v1d-spineB" if spine else "dsv4-v1d-baseline"
    trial = root / "summary-spine" / campaign / "2026-09-04" / "task-a__abc"
    agent = trial / "agent"
    (trial / "verifier").mkdir(parents=True)
    agent.mkdir(exist_ok=True)
    (agent / "instruction.txt").write_text("Repair a reusable build issue.", encoding="utf-8")
    (trial / "config.json").write_text(json.dumps({"task": {"name": "terminal-bench/task-a", "ref": "sha256:abc", "source": "terminal-bench/terminal-bench-2-1"}}), encoding="utf-8")
    (trial / "result.json").write_text(json.dumps({"finished_at": "now", "reward": 1}), encoding="utf-8")
    (trial / "verifier" / "reward.txt").write_text("1\n", encoding="utf-8")
    user_content: object = "Repair a reusable build issue."
    if image:
        user_content = [{"type": "text", "text": "inspect image"}, {"type": "image", "data": "data:image/png;base64," + base64.b64encode(b"png").decode()}]
    events = [
        {"type": "session", "id": "s", "timestamp": "t"},
        {"type": "message", "id": "u", "parentId": "s", "timestamp": "t", "message": {"role": "user", "content": user_content}},
        {"type": "message", "id": "a", "parentId": "u", "timestamp": "t", "stopReason": "toolUse", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "Check the build output."}, {"type": "text", "text": "I will inspect it."}, {"type": "toolCall", "id": "call-1", "name": "exec", "arguments": {"command": "make test"}}]}},
        {"type": "message", "id": "r", "parentId": "a", "timestamp": "t", "message": {"role": "toolResult", "toolCallId": "call-1", "toolName": "exec", "content": [{"type": "text", "text": "failed with code 2"}], "details": {"exitCode": 2}, "isError": True}},
    ]
    (agent / "openclaw.session.jsonl").write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    return trial


class TraceImportTest(unittest.TestCase):
    def test_preserves_thinking_text_tools_and_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = normalize_openclaw_trial(make_trial(Path(tmp)))
        assistant = next(step["assistant"] for step in trace["steps"] if step["role"] == "assistant")
        self.assertEqual(assistant["thinking"], ["Check the build output."])
        self.assertEqual(assistant["text"], ["I will inspect it."])
        self.assertEqual(assistant["tool_calls"][0]["tool_name"], "exec")
        self.assertEqual(trace["tool_pairing"]["missing_results"], [])
        self.assertEqual(trace["outcome"]["official_reward"], "1")
        self.assertEqual(trace["source"]["official_task_name"], "terminal-bench/task-a")
        self.assertEqual(trace["source"]["instance_id"], "task-a")

    def test_spine_b_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TraceImportError):
                normalize_openclaw_trial(make_trial(Path(tmp), spine=True))

    def test_image_is_not_silently_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = normalize_openclaw_trial(make_trial(Path(tmp), image=True))
        self.assertTrue(trace["multimodal"]["present"])
        self.assertEqual(trace["multimodal"]["blocks"][0]["status"], "multimodal_pending")
        self.assertFalse(trace["text_manager_eligible"])

    def test_message_level_usage_user_clarification_and_control_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trial = make_trial(Path(tmp))
            session = trial / "agent" / "openclaw.session.jsonl"
            events = [json.loads(line) for line in session.read_text(encoding="utf-8").splitlines()]
            assistant = next(event for event in events if event.get("id") == "a")
            assistant.pop("stopReason")
            assistant["message"]["stopReason"] = "toolUse"
            assistant["message"]["usage"] = {"input": 12, "output": 3}
            events.extend(
                [
                    {"type": "custom", "customType": "compaction", "id": "c", "parentId": "r", "timestamp": "t", "data": {"note": "boundary"}},
                    {"type": "message", "id": "u2", "parentId": "c", "timestamp": "t", "message": {"role": "user", "content": "Please preserve the source tree."}},
                ]
            )
            session.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
            trace = normalize_openclaw_trial(trial)
        assistant_record = next(record for record in trace["entries"] if record["source_entry_id"] == "a")
        self.assertEqual(assistant_record["stop_reason"], "toolUse")
        self.assertEqual(assistant_record["usage"], {"input": 12, "output": 3})
        self.assertIn("Please preserve the source tree.", [item["content"][0]["text"] for item in trace["entries"] if item["role"] == "user"])
        self.assertEqual(trace["control_events"][-1]["custom_type"], "compaction")

    def test_native_compaction_summary_is_not_promoted_to_observed_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trial = make_trial(Path(tmp))
            session = trial / "agent" / "openclaw.session.jsonl"
            events = [json.loads(line) for line in session.read_text(encoding="utf-8").splitlines()]
            native_compaction = {
                "type": "compaction",
                "id": "c",
                "parentId": "r",
                "timestamp": "t",
                "summary": "The tool failed because of a made-up reason.",
                "firstKeptEntryId": "r",
                "tokensBefore": 123,
            }
            events.append(native_compaction)
            session.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
            trace = normalize_openclaw_trial(trial)
        self.assertTrue(trace["historical_compaction"]["summary_is_not_observed_evidence"])
        self.assertEqual(trace["historical_compaction"]["control_event_ids"], ["c"])
        self.assertNotIn("c", trace["historical_compaction"]["raw_message_step_ids"])
        control = trace["control_events"][-1]
        self.assertEqual(control["raw_event"], native_compaction)
        self.assertEqual(control["raw_event"]["firstKeptEntryId"], "r")
        self.assertEqual(control["raw_event"]["tokensBefore"], 123)
        self.assertEqual(len(control["raw_event_sha256"]), 64)
        output = {
            "action": "generate",
            "skill": {"title": "Use an observed failure", "granularity": "event-driven", "when_to_apply": "After failure", "rules": ["Act on observed evidence."]},
            "evidence": {"trigger_step_ids": ["c"], "response_step_ids": ["a"], "outcome_step_ids": ["r"], "rule_evidence": [{"rule_index": 0, "step_ids": ["c", "a", "r"]}]},
        }
        with self.assertRaisesRegex(ValueError, "compaction summary/control"):
            validate_event_extraction_with_evidence(output, trace, benchmark="terminal-bench")
        raw_missing = {
            "action": "generate",
            "skill": {"title": "Use an observed failure", "granularity": "event-driven", "when_to_apply": "After failure", "rules": ["Act on observed evidence."]},
            "evidence": {"trigger_step_ids": ["r"], "response_step_ids": ["a"], "outcome_step_ids": ["r"], "rule_evidence": [{"rule_index": 0, "step_ids": ["a", "r"]}]},
        }
        trace["historical_compaction"]["raw_message_step_ids"] = ["u", "a"]
        with self.assertRaisesRegex(ValueError, "without preserved raw source content"):
            validate_event_extraction_with_evidence(raw_missing, trace, benchmark="terminal-bench")

    def test_graph_and_tool_pairing_errors_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trial = make_trial(Path(tmp))
            session = trial / "agent" / "openclaw.session.jsonl"
            events = [json.loads(line) for line in session.read_text(encoding="utf-8").splitlines()]
            events.append({"type": "message", "id": "orphan", "parentId": "r", "timestamp": "t", "message": {"role": "toolResult", "toolCallId": "unknown", "toolName": "process", "content": "nope"}})
            session.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(TraceImportError, "unknown calls"):
                normalize_openclaw_trial(trial)
        with tempfile.TemporaryDirectory() as tmp:
            trial = make_trial(Path(tmp))
            session = trial / "agent" / "openclaw.session.jsonl"
            events = [json.loads(line) for line in session.read_text(encoding="utf-8").splitlines()]
            events[-1]["parentId"] = "missing"
            session.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(TraceImportError, "Unknown parent"):
                normalize_openclaw_trial(trial)

    def test_visible_token_like_task_content_is_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trial = make_trial(Path(tmp))
            instruction = trial / "agent" / "instruction.txt"
            instruction.write_text("Preserve token=fixture-value in the task evidence.", encoding="utf-8")
            trace = normalize_openclaw_trial(trial)
        self.assertIn("token=fixture-value", trace["instruction"])
        self.assertFalse(trace["credential_handling"]["content_redacted"])
