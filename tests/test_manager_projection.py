from __future__ import annotations

import json
import unittest

from codeskill_rebuild.compaction import action_observation_segments, expand_evidence_fragments
from codeskill_rebuild.manager_projection import (
    HISTORICAL_THINKING_POLICY_VERSION,
    PROJECTION_VERSION,
    ProjectionError,
    project_historical_thinking,
    project_trace_for_manager,
)


def trace(*, divergent: bool = False) -> dict:
    partial = '{"command":"echo duplicate"}' if not divergent else '{"command":"different"}'
    aggregate = "completed output" if not divergent else "details differ from output"
    assistant = {
        "thinking": ["reasoning"],
        "text": ["visible text"],
        "tool_calls": [{"type": "tool_call", "tool_call_id": "c1", "tool_name": "exec", "arguments": {"command": "echo duplicate"}, "partial_arguments": partial}],
    }
    if divergent:
        assistant["thinking"] = ["different derived thinking"]
    return {
        "schema_version": 1,
        "kind": "normalized_openclaw_trace",
        "historical": True,
        "source": {"task_name": "terminal-bench/example"},
        "instruction": "Do the task.",
        "outcome": {"official_reward": "1"},
        "truncation": {"status": "observed", "source_markers": ["a"]},
        "text_manager_eligible": True,
        "steps": [
            {"source_entry_id": "u", "role": "user", "content": [{"type": "text", "text": "Do the task."}], "stop_reason": None, "usage": None},
            {"source_entry_id": "a", "role": "assistant", "content": [{"type": "thinking", "text": "reasoning"}, {"type": "text", "text": "visible text"}, {"type": "tool_call", "tool_call_id": "c1", "tool_name": "exec", "arguments": {"command": "echo duplicate"}, "partial_arguments": partial}], "assistant": assistant, "stop_reason": "toolUse", "usage": {"input": 2, "output": 1}},
            {"source_entry_id": "r", "role": "toolResult", "content": [{"type": "text", "text": "completed output"}], "tool_result": {"tool_call_id": "c1", "tool_name": "exec", "is_error": False, "details": {"exitCode": 0, "aggregated": aggregate}}, "stop_reason": None, "usage": None},
        ],
    }


class ManagerProjectionTest(unittest.TestCase):
    def test_keep_is_default_and_byte_equivalent(self) -> None:
        source = trace()
        source["entries"] = source["steps"]
        projected = project_historical_thinking(source)
        self.assertEqual(projected["policy"], "keep")
        self.assertEqual(projected["policy_version"], HISTORICAL_THINKING_POLICY_VERSION)
        self.assertEqual(projected["manager_trace"], source)
        self.assertIsNot(projected["manager_trace"], source)
        self.assertEqual(projected["mapping"]["recognized_fields_removed"], [])

    def test_exclude_removes_only_recognized_history_reasoning_and_keeps_evidence(self) -> None:
        source = trace()
        source["entries"] = source["steps"]
        source["steps"][1]["content"][1]["text"] = "Visible answer literally mentions thinking and reasoning."
        source["steps"][2]["content"][0]["text"] = "tool output: thinking must remain visible"
        source["entries"] = [dict(item) for item in source["steps"]]
        original = json.loads(json.dumps(source))

        projected = project_historical_thinking(source, policy="exclude")
        value = projected["manager_trace"]
        self.assertEqual(source, original)
        self.assertEqual([item["type"] for item in value["steps"][1]["content"]], ["text", "tool_call"])
        self.assertNotIn("thinking", value["steps"][1]["assistant"])
        self.assertEqual(value["steps"][1]["assistant"]["text"], ["visible text"])
        self.assertEqual(value["steps"][1]["assistant"]["tool_calls"], source["steps"][1]["assistant"]["tool_calls"])
        self.assertIn("thinking and reasoning", value["steps"][1]["content"][0]["text"])
        self.assertIn("thinking must remain", value["steps"][2]["content"][0]["text"])
        self.assertEqual(value["source"], source["source"])
        self.assertEqual(value["outcome"], source["outcome"])
        self.assertEqual(
            [item["source_entry_id"] for item in value["steps"]],
            [item["source_entry_id"] for item in source["steps"]],
        )
        self.assertEqual(len(projected["mapping"]["recognized_fields_removed"]), 4)
        self.assertTrue(projected["mapping"]["exclusion_complete_for_recognized_schema"])

    def test_exclude_preserves_and_reports_unknown_reasoning_shape(self) -> None:
        source = trace()
        source["steps"][1]["content"].append(
            {"type": "reasoning", "text": "known text", "signature": "unknown-extra-field"}
        )
        source["steps"][1]["assistant"]["reasoning"] = {"nested": "unknown"}
        projected = project_historical_thinking(source, policy="exclude")
        assistant = projected["manager_trace"]["steps"][1]
        self.assertEqual(assistant["content"][-1]["signature"], "unknown-extra-field")
        self.assertEqual(assistant["assistant"]["reasoning"], {"nested": "unknown"})
        self.assertEqual(len(projected["mapping"]["unrecognized_reasoning_like_fields"]), 2)
        self.assertFalse(projected["mapping"]["exclusion_complete_for_recognized_schema"])

    def test_unknown_policy_fails_closed(self) -> None:
        with self.assertRaisesRegex(ProjectionError, "historical thinking policy"):
            project_historical_thinking(trace(), policy="drop-everything")

    def test_strict_duplicates_are_removed_without_losing_source_blocks_or_metadata(self) -> None:
        projection = project_trace_for_manager(trace())
        steps = projection["manager_trace"]["steps"]
        assistant = steps[1]
        result = steps[2]
        self.assertEqual(projection["projection_version"], PROJECTION_VERSION)
        self.assertEqual([item["type"] for item in assistant["content"]], ["thinking", "text", "tool_call"])
        self.assertNotIn("assistant", assistant)
        self.assertNotIn("assistant_derived_nonduplicate", assistant)
        self.assertNotIn("partial_arguments", assistant["content"][2])
        self.assertNotIn("aggregated", result["tool_result"]["details"])
        self.assertEqual(projection["mapping"]["strict_duplicate_fields_omitted"][1]["raw_path"], "steps[a].assistant.thinking")
        self.assertEqual(assistant["stop_reason"], "toolUse")
        self.assertEqual(assistant["usage"], {"input": 2, "output": 1})
        integrity = projection["mapping"]["integrity"]
        self.assertTrue(integrity["content_block_count_preserved"])
        self.assertEqual(integrity["thinking_blocks"], 1)
        self.assertEqual(integrity["tool_call_blocks"], 1)
        self.assertEqual(integrity["tool_result_steps"], 1)
        self.assertEqual(integrity["source_truncation"]["status"], "observed")

    def test_nonidentical_derived_fields_are_preserved_and_marked(self) -> None:
        projection = project_trace_for_manager(trace(divergent=True))
        assistant = projection["manager_trace"]["steps"][1]
        result = projection["manager_trace"]["steps"][2]
        self.assertEqual(assistant["assistant_derived_nonduplicate"]["thinking"], ["different derived thinking"])
        self.assertEqual(assistant["content"][2]["partial_arguments"], '{"command":"different"}')
        self.assertEqual(result["tool_result"]["details"]["aggregated"], "details differ from output")
        preserved = projection["mapping"]["nonduplicate_fields_preserved"]
        self.assertGreaterEqual(len(preserved), 3)

    def test_json_type_difference_and_nonfirst_result_text_are_not_misprojected(self) -> None:
        source = trace()
        source["steps"][1]["content"][2]["arguments"] = {"enabled": True}
        source["steps"][1]["content"][2]["partial_arguments"] = '{"enabled":1}'
        source["steps"][1]["assistant"]["tool_calls"][0]["arguments"] = {"enabled": True}
        source["steps"][1]["assistant"]["tool_calls"][0]["partial_arguments"] = '{"enabled":1}'
        source["steps"][2]["content"] = [{"type": "metadata", "label": "kept"}, {"type": "text", "text": "completed output"}]
        source["control_events"] = [{"type": "compaction", "source_entry_id": "c"}]
        source["tool_pairing"] = {"missing_results": []}
        projection = project_trace_for_manager(source)
        call = projection["manager_trace"]["steps"][1]["content"][2]
        self.assertEqual(call["partial_arguments"], '{"enabled":1}')
        aggregate_mapping = next(item for item in projection["mapping"]["strict_duplicate_fields_omitted"] if item["raw_path"].endswith("aggregated"))
        self.assertTrue(aggregate_mapping["represented_by"].endswith("content[1].text"))
        self.assertEqual(projection["manager_trace"]["control_events"], source["control_events"])
        self.assertIn("control_events", projection["mapping"]["top_level_fields_retained"])

    def test_projected_steps_keep_tool_pair_closure_for_compaction(self) -> None:
        steps = project_trace_for_manager(trace())["manager_trace"]["steps"]
        segments = action_observation_segments(
            steps,
            message_count=lambda _messages: 1,
            messages_for_segment=lambda segment: [{"role": "user", "content": str(segment)}],
            max_input_tokens=1,
        )
        self.assertEqual(len(segments), 1)
        self.assertEqual([step["source_entry_id"] for step in expand_evidence_fragments(steps, ["a"])], ["a", "r"])
