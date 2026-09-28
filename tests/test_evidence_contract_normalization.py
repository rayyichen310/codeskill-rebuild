"""Boundary checks for model evidence layout and observed action/result pairing."""

from __future__ import annotations

from copy import deepcopy
import unittest

from codeskill_rebuild.pipeline import (
    normalize_extraction_evidence,
    validate_event_extraction_with_evidence,
    validate_task_candidate_with_evidence,
)
from tests.test_code_examples import (
    S01_ACTION_ID,
    S01_RESULT_ID,
    candidate_with_example,
    python_example,
    trace_with_command,
)


def _pair(action_id: str, result_id: str, call_id: str) -> list[dict]:
    return [
        {"source_entry_id": action_id, "role": "assistant", "assistant": {
            "tool_calls": [{"tool_call_id": call_id, "tool_name": "exec", "arguments": {"command": "true"}}],
        }},
        {"source_entry_id": result_id, "role": "toolResult", "tool_result": {"tool_call_id": call_id}},
    ]


class EvidenceContractTest(unittest.TestCase):
    def test_e03_interleaved_actions_and_results_are_paired_in_actual_order(self) -> None:
        trace = {
            "source": {"canonical_instance_id": "build-pmars"},
            "steps": [
                {"source_entry_id": "user", "role": "user"},
                {"source_entry_id": "trigger", "role": "toolResult", "tool_result": {"tool_call_id": "probe"}},
                *_pair("e03-action-29", "e03-result-30", "check-lzma"),
                *_pair("e03-action-31", "e03-result-32", "extract-archives"),
            ],
        }
        value = {
            "action": "generate",
            "skill": {"title": "Handle discovered archives", "granularity": "event-driven",
                      "when_to_apply": "After observing source archives", "rules": ["Check and extract the archives."]},
            "evidence": {"trigger_step_ids": ["trigger"],
                         "response_step_ids": ["e03-action-29", "e03-action-31"],
                         "outcome_step_ids": ["e03-result-30", "e03-result-32"],
                         "rule_evidence": [{"rule_index": 0, "step_ids": ["trigger", "e03-action-29", "e03-result-30", "e03-action-31", "e03-result-32"]}]},
        }
        checked = validate_event_extraction_with_evidence(value, trace, benchmark="terminal-bench")
        self.assertEqual(checked["evidence"]["outcome_step_ids"], ["e03-result-30", "e03-result-32"])
        wrong_call = deepcopy(value)
        wrong_trace = deepcopy(trace)
        wrong_trace["steps"][-1]["tool_result"]["tool_call_id"] = "unrelated-later-test"
        with self.assertRaisesRegex(ValueError, "matching response tool call"):
            validate_event_extraction_with_evidence(wrong_call, wrong_trace, benchmark="terminal-bench")
        out_of_order = deepcopy(trace)
        out_of_order["steps"][3]["tool_result"]["tool_call_id"] = "extract-archives"
        with self.assertRaisesRegex(ValueError, "matching response tool call"):
            validate_event_extraction_with_evidence(value, out_of_order, benchmark="terminal-bench")
        wrong_role = deepcopy(trace)
        wrong_role["steps"][4]["role"] = "user"
        with self.assertRaisesRegex(ValueError, "assistant action"):
            validate_event_extraction_with_evidence(value, wrong_role, benchmark="terminal-bench")

    def test_k02_example_pair_may_be_split_across_valid_rule_evidence(self) -> None:
        trace = trace_with_command()
        trace["steps"] = [*_pair("prior-action", "prior-result", "prior-call"), *trace["steps"], *_pair("later-action", "later-result", "later-call")]
        trace["historical_compaction"]["raw_message_step_ids"] = [step["source_entry_id"] for step in trace["steps"]]
        value = candidate_with_example(trace, python_example(trace))
        value["skill"]["rules"] = ["Inspect the prior result and select extraction.", "Check the result and later state."]
        value["evidence"]["rule_evidence"] = [
            {"rule_index": 0, "sources": [{"canonical_instance_id": "build-pmars", "step_ids": ["prior-action", "prior-result", S01_ACTION_ID]}]},
            {"rule_index": 1, "sources": [{"canonical_instance_id": "build-pmars", "step_ids": [S01_RESULT_ID, "later-action", "later-result"]}]},
        ]
        checked = validate_task_candidate_with_evidence(value, trace, benchmark="terminal-bench")
        self.assertEqual(checked["skill"]["code_examples"][0]["source_binding"]["action_step_id"], S01_ACTION_ID)
        self.assertEqual(checked["skill"]["code_examples"][0]["source_binding"]["result_step_id"], S01_RESULT_ID)
        wrong_pair = deepcopy(value)
        wrong_pair["skill"]["code_examples"][0]["source"]["result_step_id"] = "later-result"
        with self.assertRaisesRegex(ValueError, "does not share the cited tool_call_id"):
            validate_task_candidate_with_evidence(wrong_pair, trace, benchmark="terminal-bench")

    def test_nested_and_duplicate_source_evidence_normalize_without_mutating_input(self) -> None:
        trace = trace_with_command()
        value = candidate_with_example(trace, python_example(trace))
        value["skill"]["evidence"] = value.pop("evidence")
        sources = value["skill"]["evidence"]["rule_evidence"][0]["sources"]
        sources[0]["step_ids"] = [S01_ACTION_ID]
        sources.append({"canonical_instance_id": "build-pmars", "step_ids": [S01_RESULT_ID]})
        original = deepcopy(value)
        checked = validate_task_candidate_with_evidence(value, trace, benchmark="terminal-bench")
        self.assertEqual(value, original)
        self.assertEqual(checked["evidence"]["rule_evidence"][0]["sources"][0]["step_ids"], [S01_ACTION_ID, S01_RESULT_ID])
        self.assertEqual([item["kind"] for item in checked["evidence_normalization"]["changes"]], ["move_nested_evidence", "merge_same_source_steps"])
        self.assertNotEqual(checked["evidence_normalization"]["input_model_json_sha256"], checked["evidence_normalization"]["normalized_model_json_sha256"])
        same_both = deepcopy(original)
        same_both["evidence"] = deepcopy(same_both["skill"]["evidence"])
        normalized, record = normalize_extraction_evidence(same_both, granularity="task")
        self.assertNotIn("evidence", normalized["skill"])
        self.assertEqual(record["changes"][0]["kind"], "deduplicate_identical_nested_evidence")

    def test_conflicts_unknown_ids_and_invalid_code_still_reject(self) -> None:
        trace = trace_with_command()
        value = candidate_with_example(trace, python_example(trace))
        conflict = deepcopy(value)
        conflict["skill"]["evidence"] = {"rule_evidence": []}
        with self.assertRaisesRegex(ValueError, "conflicts"):
            validate_task_candidate_with_evidence(conflict, trace, benchmark="terminal-bench")
        duplicate_conflict = deepcopy(value)
        duplicate_conflict["evidence"]["rule_evidence"][0]["sources"].append(
            {"canonical_instance_id": "build-pmars", "step_ids": [S01_RESULT_ID], "unsupported_note": "different fact"}
        )
        with self.assertRaisesRegex(ValueError, "duplicate source entries conflict"):
            validate_task_candidate_with_evidence(duplicate_conflict, trace, benchmark="terminal-bench")
        unknown_source = deepcopy(value)
        unknown_source["evidence"]["rule_evidence"][0]["sources"][0]["canonical_instance_id"] = "missing-source"
        with self.assertRaisesRegex(ValueError, "unknown source IDs"):
            validate_task_candidate_with_evidence(unknown_source, trace, benchmark="terminal-bench")
        transposed_id = deepcopy(value)
        transposed_id["skill"]["code_examples"][0]["source"]["result_step_id"] = "4b4a-not-present"
        with self.assertRaisesRegex(ValueError, "unknown source IDs"):
            validate_task_candidate_with_evidence(transposed_id, trace, benchmark="terminal-bench")
        wrong_rule_pair_trace = deepcopy(trace)
        wrong_rule_pair_trace["steps"].append({"source_entry_id": "unrelated-result", "role": "toolResult", "tool_result": {"tool_call_id": "unrelated-call"}})
        wrong_rule_pair_trace["historical_compaction"]["raw_message_step_ids"].append("unrelated-result")
        wrong_rule_pair = deepcopy(value)
        wrong_rule_pair["evidence"]["rule_evidence"][0]["sources"][0]["step_ids"] = [S01_ACTION_ID, "unrelated-result"]
        with self.assertRaisesRegex(ValueError, "tool call ID in an ordered pair"):
            validate_task_candidate_with_evidence(wrong_rule_pair, wrong_rule_pair_trace, benchmark="terminal-bench")
        wrong_fragment = deepcopy(value)
        wrong_fragment["skill"]["code_examples"][0]["adaptations"][0]["source_fragment"] = "invented source fragment"
        with self.assertRaisesRegex(ValueError, "source_fragment"):
            validate_task_candidate_with_evidence(wrong_fragment, trace, benchmark="terminal-bench")
        wrong_generated_fragment = deepcopy(value)
        wrong_generated_fragment["skill"]["code_examples"][0]["adaptations"][0]["generated_fragment"] = "invented generated fragment"
        with self.assertRaisesRegex(ValueError, "generated_fragment"):
            validate_task_candidate_with_evidence(wrong_generated_fragment, trace, benchmark="terminal-bench")
        invalid_python = deepcopy(value)
        invalid_python["skill"]["code_examples"][0]["generated_code"] = "import tarfile\nwith broken syntax"
        with self.assertRaisesRegex(ValueError, "syntax validation"):
            validate_task_candidate_with_evidence(invalid_python, trace, benchmark="terminal-bench")


if __name__ == "__main__":
    unittest.main()
