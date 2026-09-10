from __future__ import annotations

import unittest

from codeskill_rebuild.compaction import action_observation_segments, complete_tool_pair_count, expand_evidence_fragments
from codeskill_rebuild.pipeline import validate_r006_budget_summary


def step(entry_id: str, role: str, *, calls: list[str] | None = None, result: str | None = None) -> dict:
    value = {"source_entry_id": entry_id, "role": role}
    if calls is not None:
        value["assistant"] = {"tool_calls": [{"tool_call_id": call} for call in calls]}
    if result is not None:
        value["tool_result"] = {"tool_call_id": result}
    return value


class CompactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.steps = [
            step("u", "user"),
            step("a1", "assistant", calls=["one", "two"]),
            step("r1", "toolResult", result="one"),
            step("r2", "toolResult", result="two"),
            step("a2", "assistant", calls=["three"]),
            step("r3", "toolResult", result="three"),
        ]

    def test_segments_never_split_a_multi_tool_batch(self) -> None:
        segments = action_observation_segments(
            self.steps,
            message_count=lambda messages: len(messages[0]["content"]) * 10,
            messages_for_segment=lambda values: [{"role": "user", "content": values}],
            max_input_tokens=50,
        )
        self.assertEqual(segments[0]["step_ids"], ["u", "a1", "r1", "r2"])
        self.assertEqual(segments[1]["step_ids"], ["a2", "r3"])

    def test_fragments_close_over_matching_tool_pairs(self) -> None:
        fragments = expand_evidence_fragments(self.steps, ["r1"])
        self.assertEqual([item["source_entry_id"] for item in fragments], ["a1", "r1", "r2"])
        self.assertEqual(complete_tool_pair_count(fragments), 2)

    def test_r006_summary_requires_coverage_bounded_original_pairs_and_final_observation(self) -> None:
        summary, fragments = validate_r006_budget_summary(
            {
                "summary": "Observed both tool batches.",
                "covered_step_ids": ["u", "a1", "r1", "r2", "a2", "r3"],
                "verbatim_evidence_step_ids": ["r1", "r3"],
            },
            segment_steps=self.steps,
            final_segment=True,
        )
        self.assertEqual(summary["covered_step_ids"], ["u", "a1", "r1", "r2", "a2", "r3"])
        self.assertEqual([item["source_entry_id"] for item in fragments], ["a1", "r1", "r2", "a2", "r3"])
        with self.assertRaisesRegex(ValueError, "final observed"):
            validate_r006_budget_summary(
                {"summary": "Missing final result.", "covered_step_ids": ["u", "a1", "r1", "r2", "a2", "r3"], "verbatim_evidence_step_ids": ["r1"]},
                segment_steps=self.steps,
                final_segment=True,
            )
