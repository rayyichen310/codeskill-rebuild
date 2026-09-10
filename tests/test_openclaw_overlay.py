from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codeskill_rebuild.openclaw_overlay import DurableOverlay, EventSelectionSettings, OverlayContextError, OverlayError
from codeskill_rebuild.types import read_json


TASK = {"skill_id": "task-1", "version": 1, "title": "Inspect a failure", "when_to_apply": "At task start", "rules": ["Read the task first."]}
EVENT = {"skill_id": "event-1", "version": 2, "title": "Check a tool result", "when_to_apply": "After a command fails", "rules": ["Inspect the result before retrying."]}
EVENT_2 = {"skill_id": "event-2", "version": 1, "title": "Check a changed file", "when_to_apply": "After a command changes files", "rules": ["Inspect the file before the next decision."]}


def user_task() -> dict:
    return {"role": "user", "content": "Solve the task."}


def tool_batch(*, complete: bool = True) -> list[dict]:
    messages = [
        user_task(),
        {"role": "assistant", "content": None, "tool_calls": [{"id": "a", "type": "function", "function": {"name": "exec", "arguments": "{}"}}, {"id": "b", "type": "function", "function": {"name": "exec", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "first"},
    ]
    if complete:
        messages.append({"role": "tool", "tool_call_id": "b", "content": "second"})
    return messages


def second_batch() -> list[dict]:
    return tool_batch() + [
        {"role": "assistant", "tool_calls": [{"id": "c", "type": "function", "function": {"name": "exec", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c", "content": "third"},
    ]


def native_compaction(*, before: int, after: int) -> dict:
    return {
        "kind": "native_compaction",
        "confirmed": True,
        "compaction_id": "c1",
        "native_session_event_ref": "openclaw.session.jsonl:42",
        "observed_at_utc": "2026-09-07T00:00:00Z",
        "before_forwarded_request_ordinal": before,
        "after_proxy_attempt_ordinal": after,
    }


class DurableOverlayTest(unittest.TestCase):
    def make_overlay(self, root: Path, *, event=EVENT, counter=None, task=True, events=True, event_selector=None, event_settings=None) -> DurableOverlay:
        return DurableOverlay(
            trial_id="trial-a",
            state_path=root / "state.json",
            evidence_dir=root / "evidence",
            token_counter=counter or (lambda payload: len(payload["messages"])),
            task_selector=(lambda _user, _messages: {"skills": [TASK], "bank_snapshot": {"sequence": 1}}) if task else None,
            event_selector=event_selector or ((lambda _anchor, _prefix: {"skill": event, "bank_snapshot": {"sequence": 1}}) if events else None),
            event_selection_settings=event_settings,
            enable_task=task,
            enable_event=events,
        )

    def test_task_is_attached_to_original_user_and_event_is_anchored_after_complete_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            overlay = self.make_overlay(Path(tmp))
            overlay.prepare({"model": "m", "messages": [user_task()]})
            forwarded, record = overlay.prepare({"model": "m", "messages": tool_batch()})
        self.assertIn("[CODESKILL TASK PRIOR KNOWLEDGE]", forwarded["messages"][0]["content"])
        self.assertEqual(forwarded["messages"][3]["role"], "tool")
        self.assertEqual(forwarded["messages"][4]["role"], "user")
        self.assertIn("[CODESKILL EVENT PRIOR KNOWLEDGE]", forwarded["messages"][4]["content"])
        self.assertEqual(record["event_selection"][0]["decision"], "injected_after_complete_batch")

    def test_incomplete_multi_tool_batch_is_not_injected_and_later_history_reoverlays_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            overlay = self.make_overlay(root)
            overlay.prepare({"messages": [user_task()]})
            incomplete, first = overlay.prepare({"messages": tool_batch(complete=False)})
            self.assertEqual(first["event_selection"], [])
            self.assertEqual(len(incomplete["messages"]), 3)
            complete, _ = overlay.prepare({"messages": tool_batch(complete=True)})
            later_native = tool_batch(complete=True) + [{"role": "assistant", "content": "next decision"}]
            later, later_record = overlay.prepare({"messages": later_native})
        self.assertEqual(len([item for item in complete["messages"] if item.get("role") == "user" and "EVENT PRIOR" in str(item.get("content"))]), 1)
        self.assertEqual(len([item for item in later["messages"] if item.get("role") == "user" and "EVENT PRIOR" in str(item.get("content"))]), 1)
        self.assertEqual(later["messages"][4]["role"], "user")
        self.assertEqual(later_record["event_selection"], [])

    def test_same_event_skill_version_is_not_injected_at_a_second_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            overlay = self.make_overlay(Path(tmp))
            overlay.prepare({"messages": [user_task()]})
            overlay.prepare({"messages": tool_batch()})
            forwarded, record = overlay.prepare({"messages": second_batch()})
        blocks = [item for item in forwarded["messages"] if item.get("role") == "user" and "EVENT PRIOR" in str(item.get("content"))]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(record["event_selection"][-1]["decision"], "deduplicated_prior_version")

    def test_no_skill_arm_passes_complete_payload_through_but_still_counts_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seen: list[dict] = []
            overlay = self.make_overlay(Path(tmp), task=False, events=False, counter=lambda payload: seen.append(payload) or 7)
            payload = {"model": "m", "messages": tool_batch(), "tools": [{"type": "function", "function": {"name": "exec"}}], "tool_choice": "auto"}
            forwarded, record = overlay.prepare(payload)
        self.assertEqual(forwarded, payload)
        self.assertEqual(record["exact_forwarded_input_tokens"], 7)
        self.assertEqual(record["token_count_scope"], "complete_forwarded_openai_payload")
        self.assertEqual(seen[0], payload)

    def test_cross_trial_state_is_isolated_and_budget_never_trims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            overlay = self.make_overlay(root, counter=lambda _payload: 11)
            overlay.max_input_tokens = 10
            with self.assertRaisesRegex(OverlayContextError, "exceeds configured input budget"):
                overlay.prepare({"messages": [user_task()]})
            second = DurableOverlay(
                trial_id="trial-b",
                state_path=root / "other-state.json",
                evidence_dir=root / "other-evidence",
                token_counter=lambda payload: len(payload["messages"]),
                enable_task=False,
                enable_event=False,
            )
            forwarded, _ = second.prepare({"messages": [user_task()]})
        self.assertEqual(forwarded["messages"], [user_task()])

    def test_compaction_requires_current_native_evidence_and_failure_attempt_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            overlay = self.make_overlay(root)
            overlay.prepare({"messages": [user_task()]})
            overlay.prepare({"messages": tool_batch()})
            compacted = {"messages": [{"role": "system", "content": "system"}, {"role": "user", "content": "native compacted history"}]}
            with self.assertRaisesRegex(OverlayContextError, "without current, explicit native compaction evidence"):
                overlay.prepare(compacted)
            self.assertEqual(read_json(root / "evidence" / "upstream_requests" / "attempt-0003.json")["outcome"], "context_or_overlay_error")
            with self.assertRaisesRegex(OverlayContextError, "without current, explicit native compaction evidence"):
                overlay.prepare(compacted, compaction_evidence=native_compaction(before=1, after=4))
            forwarded, record = overlay.prepare(compacted, compaction_evidence=native_compaction(before=2, after=5))
            continued, continued_record = overlay.prepare(
                {"messages": [*compacted["messages"], {"role": "assistant", "content": "next native decision"}]}
            )
        carried = [item for item in forwarded["messages"] if item.get("role") == "user" and "CARRIED" in str(item.get("content"))]
        continued_carried = [item for item in continued["messages"] if item.get("role") == "user" and "CARRIED" in str(item.get("content"))]
        self.assertEqual(len(carried), 1)
        self.assertEqual(len(continued_carried), 1)
        self.assertEqual(forwarded["messages"][1]["role"], "user")
        self.assertIn("carried_priors", record)
        self.assertIn("continued_carried_priors", continued_record)
        self.assertEqual(record["retired_event_priors"]["events"][0]["skill_id"], EVENT["skill_id"])

    def test_current_anchor_repositioning_does_not_lose_event_prior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            overlay = self.make_overlay(Path(tmp))
            overlay.prepare({"messages": [user_task()]})
            overlay.prepare({"messages": tool_batch()})
            reframed = [{"role": "system", "content": "new native prefix"}, *tool_batch(), {"role": "assistant", "content": "later native decision"}]
            moved, _ = overlay.prepare({"messages": reframed})
        self.assertEqual(moved["messages"][4]["role"], "tool")
        self.assertIn("[CODESKILL EVENT PRIOR KNOWLEDGE]", moved["messages"][5]["content"])

    def test_event_anchor_return_after_compaction_stays_retired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            overlay = self.make_overlay(Path(tmp))
            overlay.prepare({"messages": [user_task()]})
            overlay.prepare({"messages": tool_batch()})
            compacted = {"messages": [{"role": "system", "content": "system"}, {"role": "user", "content": "native compacted history"}]}
            overlay.prepare(compacted, compaction_evidence=native_compaction(before=2, after=3))
            restored, record = overlay.prepare({"messages": tool_batch()})
            with self.assertRaisesRegex(OverlayContextError, "without current, explicit native compaction evidence"):
                overlay.prepare(compacted)
        self.assertIn("recovered_anchors", record)
        self.assertEqual(len([item for item in restored["messages"] if "CARRIED" in str(item.get("content"))]), 0)
        self.assertEqual(len([item for item in restored["messages"] if "EVENT PRIOR" in str(item.get("content"))]), 0)

    def test_multiple_event_skills_require_an_explicit_development_setting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            overlay = self.make_overlay(
                Path(tmp),
                event_selector=lambda _anchor, _prefix: {"skills": [EVENT, EVENT_2], "bank_snapshot": {"sequence": 9}},
            )
            overlay.prepare({"messages": [user_task()]})
            with self.assertRaisesRegex(OverlayError, "explicit EventSelectionSettings"):
                overlay.prepare({"messages": tool_batch()})

    def test_multiple_matching_events_are_injected_in_selector_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            overlay = self.make_overlay(
                Path(tmp),
                event_settings=EventSelectionSettings(max_matching_skills=2, profile_ref="fixture-r012-multi-event"),
                event_selector=lambda _anchor, _prefix: {"skills": [EVENT, EVENT_2], "bank_snapshot": {"sequence": 9}},
            )
            overlay.prepare({"messages": [user_task()]})
            forwarded, record = overlay.prepare({"messages": tool_batch()})
        blocks = [item["content"] for item in forwarded["messages"] if "EVENT PRIOR" in str(item.get("content"))]
        self.assertEqual(len(blocks), 2)
        self.assertIn(EVENT["title"], blocks[0])
        self.assertIn(EVENT_2["title"], blocks[1])
        self.assertEqual([item["skill"]["skill_id"] for item in record["event_selection"][0]["injected_skills"]], ["event-1", "event-2"])
        self.assertEqual(record["event_selection"][0]["event_selection_settings"]["relevance_threshold"], None)

    def test_retired_event_can_reinject_at_a_later_matching_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            overlay = self.make_overlay(root)
            overlay.prepare({"messages": [user_task()]})
            overlay.prepare({"messages": tool_batch()})
            compacted = {"messages": [{"role": "system", "content": "system"}, {"role": "user", "content": "native compacted history"}]}
            _, retirement = overlay.prepare(compacted, compaction_evidence=native_compaction(before=2, after=3))
            later_batch = [
                *compacted["messages"],
                {"role": "assistant", "tool_calls": [{"id": "new", "type": "function", "function": {"name": "exec", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "new", "content": "new result"},
            ]
            forwarded, reinjection = overlay.prepare({"messages": later_batch})
            state = read_json(root / "state.json")
        self.assertEqual(retirement["retired_event_priors"]["count"], 1)
        self.assertEqual([event["status"] for event in state["events"]], ["retired_native_compaction", "active"])
        self.assertEqual(reinjection["event_selection"][0]["decision"], "injected_after_complete_batch")
        self.assertEqual(len([item for item in forwarded["messages"] if "CARRIED EVENT" in str(item.get("content"))]), 0)
        self.assertEqual(len([item for item in forwarded["messages"] if "EVENT PRIOR" in str(item.get("content"))]), 1)

    def test_preexisting_history_is_never_retroactively_selected_and_selector_gets_only_boundary_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefixes: list[list[dict]] = []
            overlay = self.make_overlay(
                Path(tmp),
                event_selector=lambda _anchor, prefix: prefixes.append(prefix) or {"skill": None, "bank_snapshot": {"sequence": 1}},
            )
            _, first = overlay.prepare({"messages": tool_batch()})
            _, second = overlay.prepare({"messages": tool_batch() + [{"role": "assistant", "content": "already decided"}]})
            _, third = overlay.prepare({"messages": second_batch()})
            _, fourth = overlay.prepare({"messages": second_batch() + [{"role": "assistant", "content": "already decided"}]})
        self.assertEqual(first["event_selection"][0]["decision"], "preexisting_before_overlay")
        self.assertEqual(second["event_selection"], [])
        self.assertEqual(len(prefixes), 1)
        self.assertEqual(prefixes[0], second_batch())
        self.assertEqual(third["event_selection"][-1]["decision"], "no_skill_selected")
        self.assertEqual(fourth["event_selection"], [])


if __name__ == "__main__":
    unittest.main()
