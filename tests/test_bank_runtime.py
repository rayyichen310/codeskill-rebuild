from __future__ import annotations

import json
import unittest
import tempfile
from copy import deepcopy
from pathlib import Path

from codeskill_rebuild.bank import BankError, SkillBank
from codeskill_rebuild.context import plan_context
from codeskill_rebuild.pipeline import (
    internal_skill_to_paper,
    maintenance_from_skills_messages as maintenance_messages,
    paper_skill_to_internal,
    validate_budget_summary,
    validate_event_evidence_repair,
    validate_event_extraction_with_evidence,
    validate_maintenance_from_skills as validate_maintenance,
    validate_pairing,
    validate_task_candidate_with_evidence,
    validate_task_extraction_with_evidence,
)
from codeskill_rebuild.runtime import build_initial_messages, inject_event_message
from codeskill_rebuild.types import contract_from_files, write_contract_snapshot


def candidate(title: str = "Check compiler diagnostics") -> dict:
    return {"title": title, "granularity": "event", "when_to_apply": "When a build command exits unsuccessfully", "rules": ["Read the first failing diagnostic before changing configuration."], "benchmark": "terminal-bench"}


class BankAndRuntimeTest(unittest.TestCase):
    def test_single_task_candidate_requires_context_and_local_action_result_evidence(self) -> None:
        trace = {
            "source": {"instance_id": "source-alpha-0001"},
            "steps": [
                {"source_entry_id": "11111111-action", "role": "assistant", "assistant": {"tool_calls": [{"tool_call_id": "call-alpha"}]}},
                {"source_entry_id": "22222222-result", "role": "toolResult", "tool_result": {"tool_call_id": "call-alpha"}},
            ],
        }
        value = {
            "action": "generate",
            "skill": {
                "title": "Bounded repair SOP",
                "granularity": "general",
                "when_to_apply": "When a bounded repair needs an observed validation loop.",
                "rules": ["Inspect the failure, apply the repair, and observe validation."],
            },
            "candidate_context": {
                "task_goal": "Repair the target",
                "whole_task_outcome": "completed",
                "hard_constraints": [],
                "environment_assumptions": [],
                "observed_results": ["validation completed"],
                "known_limitations": [],
            },
            "evidence": {"rule_evidence": [{"rule_index": 0, "sources": [{"canonical_instance_id": "source-alpha-0001", "step_ids": ["11111111-action", "22222222-result"]}]}]},
        }
        checked = validate_task_candidate_with_evidence(value, trace, benchmark="terminal-bench")
        self.assertEqual(checked["candidate_context"]["whole_task_outcome"], "completed")
        with self.assertRaisesRegex(ValueError, "candidate_context"):
            validate_task_candidate_with_evidence({key: item for key, item in value.items() if key != "candidate_context"}, trace, benchmark="terminal-bench")
        missing_result = deepcopy(value)
        missing_result["evidence"]["rule_evidence"][0]["sources"][0]["step_ids"] = ["11111111-action"]
        with self.assertRaisesRegex(ValueError, "action followed by an observed tool result"):
            validate_task_candidate_with_evidence(missing_result, trace, benchmark="terminal-bench")

    def test_task_extraction_requires_each_rule_to_have_action_result_evidence_from_every_source(self) -> None:
        traces = [
            {
                "source": {"instance_id": "source-alpha-0001"},
                "steps": [
                    {"source_entry_id": "11111111-action", "role": "assistant", "assistant": {"tool_calls": [{"tool_call_id": "call-alpha"}]}},
                    {"source_entry_id": "22222222-result", "role": "toolResult", "tool_result": {"tool_call_id": "call-alpha"}},
                ],
            },
            {
                "source": {"instance_id": "source-beta-0002"},
                "steps": [
                    {"source_entry_id": "33333333-action", "role": "assistant", "assistant": {"tool_calls": [{"tool_call_id": "call-beta"}]}},
                    {"source_entry_id": "44444444-result", "role": "toolResult", "tool_result": {"tool_call_id": "call-beta"}},
                ],
            },
        ]
        value = {
            "action": "generate",
            "skill": {
                "title": "Validate a repair loop",
                "granularity": "general",
                "when_to_apply": "When a terminal repair needs an observed validation loop.",
                "rules": ["Apply the repair and verify its observed result."],
            },
            "evidence": {
                "rule_evidence": [
                    {
                        "rule_index": 0,
                        "sources": [
                            {"canonical_instance_id": "source-a", "step_ids": ["11111111", "22222222"]},
                            {"canonical_instance_id": "source-beta-0002", "step_ids": ["33333333-action", "44444444-result"]},
                        ],
                    }
                ]
            },
        }
        checked = validate_task_extraction_with_evidence(value, traces, benchmark="terminal-bench")
        self.assertEqual(
            checked["evidence"]["rule_evidence"][0]["sources"],
            [
                {"canonical_instance_id": "source-alpha-0001", "step_ids": ["11111111-action", "22222222-result"]},
                {"canonical_instance_id": "source-beta-0002", "step_ids": ["33333333-action", "44444444-result"]},
            ],
        )
        missing_source = deepcopy(value)
        missing_source["evidence"]["rule_evidence"][0]["sources"].pop()
        with self.assertRaisesRegex(ValueError, "per selected instance"):
            validate_task_extraction_with_evidence(missing_source, traces, benchmark="terminal-bench")
        missing_result = deepcopy(value)
        missing_result["evidence"]["rule_evidence"][0]["sources"][0]["step_ids"] = ["11111111-action"]
        with self.assertRaisesRegex(ValueError, "assistant action followed by"):
            validate_task_extraction_with_evidence(missing_result, traces, benchmark="terminal-bench")
        reversed_steps = deepcopy(traces)
        reversed_steps[0]["steps"].reverse()
        with self.assertRaisesRegex(ValueError, "assistant action followed by"):
            validate_task_extraction_with_evidence(value, reversed_steps, benchmark="terminal-bench")
        mismatched_call = deepcopy(traces)
        mismatched_call[0]["steps"][1]["tool_result"]["tool_call_id"] = "other-call"
        with self.assertRaisesRegex(ValueError, "share a tool call ID"):
            validate_task_extraction_with_evidence(value, mismatched_call, benchmark="terminal-bench")
        with self.assertRaisesRegex(ValueError, "absent from supplied original fragments"):
            validate_task_extraction_with_evidence(
                value,
                traces,
                benchmark="terminal-bench",
                visible_step_ids_by_source={
                    "source-alpha-0001": ["11111111-action"],
                    "source-beta-0002": ["33333333-action", "44444444-result"],
                },
            )

    def test_add_merge_drop_and_idempotence(self) -> None:
        bank = SkillBank.empty("terminal-bench")
        add = bank.apply(operation_id="op-add", decision="add", candidate=candidate(), source_instance_ids=["source-a"], evidence={"call": "1"})
        first = add["result_skill_id"]
        again = bank.apply(operation_id="op-add", decision="add", candidate=candidate(), source_instance_ids=["source-a"], evidence={"call": "1"})
        self.assertEqual(add, again)
        self.assertEqual(len(bank.skills), 1)
        merged = bank.apply(operation_id="op-merge", decision="merge", candidate=candidate("Inspect build failure"), source_instance_ids=["source-b"], evidence={"call": "2"}, merge_target_id=first)
        self.assertEqual(bank.skills[0]["status"], "superseded")
        active = bank.eligible(instance_id="new", granularity="event")
        self.assertEqual(active[0]["provenance"]["source_instance_ids"], ["source-a", "source-b"])
        bank.apply(operation_id="op-drop", decision="drop", candidate=candidate("Weak note"), source_instance_ids=["source-c"], evidence={"call": "3"})
        self.assertEqual(len(bank.skills), 2)
        self.assertEqual(merged["decision"], "merge")

    def test_source_and_future_skills_are_excluded(self) -> None:
        bank = SkillBank.empty("terminal-bench")
        bank.apply(operation_id="a", decision="add", candidate=candidate(), source_instance_ids=["same-task"], evidence={})
        self.assertEqual(bank.eligible(instance_id="same-task", granularity="event"), [])
        self.assertEqual(bank.eligible(instance_id="other", granularity="event", frozen_sequence=0), [])

    def test_terminal_bench_full_and_canonical_ids_share_provenance_exclusion(self) -> None:
        bank = SkillBank.empty("terminal-bench")
        original = bank.apply(
            operation_id="add-full-id",
            decision="add",
            candidate=candidate("Original full ID"),
            source_instance_ids=["terminal-bench/fix-git"],
            evidence={},
        )
        evolved = candidate("Evolved canonical ID")
        evolved["provenance"] = {
            "source_instance_ids": ["fix-git"],
            "source_instance_ids_raw": ["terminal-bench/fix-git"],
            "parent_skill_ids": [original["result_skill_id"]],
        }
        target = bank.apply(
            operation_id="other-source",
            decision="add",
            candidate=candidate("Other source"),
            source_instance_ids=["git-leak-recovery"],
            evidence={},
        )
        merged = bank.apply(
            operation_id="merge-evolved",
            decision="merge",
            candidate=evolved,
            source_instance_ids=[],
            evidence={},
            merge_target_id=target["result_skill_id"],
        )
        active = next(skill for skill in bank.skills if skill.get("skill_id") == merged["result_skill_id"])
        self.assertEqual(active["provenance"]["source_instance_ids"], ["fix-git", "git-leak-recovery"])
        self.assertIn("terminal-bench/fix-git", active["provenance"]["source_instance_ids_raw"])
        self.assertEqual(bank.eligible(instance_id="terminal-bench/fix-git", granularity="event"), [])
        self.assertEqual(bank.eligible(instance_id="fix-git", granularity="event"), [])

    def test_snapshots_are_immutable_and_retrieve_at_frozen_sequence(self) -> None:
        bank = SkillBank.empty("terminal-bench")
        added = bank.apply(operation_id="add", decision="add", candidate=candidate(), source_instance_ids=["source-a"], evidence={})
        frozen = bank.snapshot(1)
        bank.apply(operation_id="merge", decision="merge", candidate=candidate("Clearer diagnostic"), source_instance_ids=["source-b"], evidence={}, merge_target_id=added["result_skill_id"])
        self.assertEqual(len(frozen["skills"]), 1)
        self.assertEqual(frozen["skills"][0]["status"], "active")
        self.assertEqual(len(bank.eligible(instance_id="other", granularity="event", frozen_sequence=1)), 1)
        self.assertEqual(bank.operations[-1]["after_state_sha256"], bank.snapshot(2)["state_sha256"])

    def test_evolved_candidate_ancestry_survives_later_merge(self) -> None:
        bank = SkillBank.empty("terminal-bench")
        original = bank.apply(operation_id="original", decision="add", candidate=candidate("Original"), source_instance_ids=["source-a"], evidence={})
        target = bank.apply(operation_id="target", decision="add", candidate=candidate("Target"), source_instance_ids=["source-b"], evidence={})
        evolved = candidate("Evolved original")
        evolved["provenance"] = {"source_instance_ids": ["source-a"], "parent_skill_ids": [original["result_skill_id"]]}
        merged = bank.apply(operation_id="merge-evolved", decision="merge", candidate=evolved, source_instance_ids=[], evidence={}, merge_target_id=target["result_skill_id"])
        active = next(skill for skill in bank.skills if skill.get("skill_id") == merged["result_skill_id"])
        self.assertEqual(active["provenance"]["source_instance_ids"], ["source-a", "source-b"])
        self.assertEqual(bank.eligible(instance_id="source-a", granularity="event"), [])
        eligible_for_b = bank.eligible(instance_id="source-b", granularity="event")
        self.assertNotIn(merged["result_skill_id"], [skill["skill_id"] for skill in eligible_for_b])

    def test_atomic_save_recovers_immutable_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bank.json"
            bank = SkillBank.empty("terminal-bench")
            added = bank.apply_and_save(path, operation_id="a", decision="add", candidate=candidate(), source_instance_ids=["a"], evidence={})
            bank.apply_and_save(path, operation_id="b", decision="merge", candidate=candidate("Merged"), source_instance_ids=["b"], evidence={}, merge_target_id=added["result_skill_id"])
            recovered = SkillBank.load(path)
        self.assertEqual(recovered.snapshot(1)["skills"][0]["status"], "active")
        self.assertEqual(recovered.snapshot(2)["skills"][0]["status"], "superseded")

    def test_invalid_skill_rejected(self) -> None:
        bank = SkillBank.empty("terminal-bench")
        invalid = candidate()
        invalid["rules"] = []
        with self.assertRaises(BankError):
            bank.apply(operation_id="bad", decision="add", candidate=invalid, source_instance_ids=[], evidence={})

    def test_initial_and_event_injection_order(self) -> None:
        skill = candidate()
        skill.update({"skill_id": "s1", "version": 1})
        initial, record = build_initial_messages("Solve it", [skill])
        self.assertTrue(record["injected"])
        self.assertIn("CODESKILL TASK", initial[0]["content"])
        conversation = initial + [
            {"role": "assistant", "tool_calls": [{"id": "call-1"}, {"id": "call-2"}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "first result"},
        ]
        incomplete, pending = inject_event_message(conversation, skill, set())
        self.assertFalse(pending["injected"])
        self.assertEqual(pending["pending_tool_call_ids"], ["call-2"])
        self.assertEqual(len(incomplete), len(conversation))
        conversation.append({"role": "tool", "tool_call_id": "call-2", "content": "failure"})
        updated, event = inject_event_message(conversation, skill, set())
        self.assertTrue(event["injected"])
        self.assertEqual(updated[-2]["role"], "tool")
        self.assertEqual(updated[-1]["role"], "user")

    def test_context_is_blocked_not_silently_trimmed(self) -> None:
        plan = plan_context(rendered="x" * 100, step_ids=["a", "b"], budget_tokens=1)
        self.assertEqual(plan.state, "context_blocked")
        self.assertEqual(plan.omitted_step_ids, ["a", "b"])

    def test_heuristic_within_budget_is_not_labeled_full(self) -> None:
        plan = plan_context(rendered="small", step_ids=["a"], budget_tokens=100)
        self.assertEqual(plan.state, "estimated_within_budget")

    def test_paper_granularity_adapter_is_explicit(self) -> None:
        source = {"title": "A", "granularity": "general", "when_to_apply": "A condition", "rules": ["Do thing"]}
        internal = paper_skill_to_internal(source, benchmark="terminal-bench", expected_granularity="task")
        self.assertEqual(internal["granularity"], "task")
        with self.assertRaises(ValueError):
            paper_skill_to_internal(source, benchmark="terminal-bench", expected_granularity="event")

    def test_fig9_payload_converts_internal_granularity_for_candidate_and_retrieval(self) -> None:
        candidate_internal = {**candidate("Candidate"), "skill_id": "candidate-id", "version": 1}
        retrieved_internal = {**candidate("Retrieved"), "skill_id": "retrieved-id", "version": 2, "status": "active"}
        messages = maintenance_messages(candidate_internal, [retrieved_internal], paper_prompt="fig9")
        payload = json.loads(messages[1]["content"])
        self.assertEqual(messages[0]["content"], "fig9")
        self.assertEqual(payload["candidate_skill"]["granularity"], "event-driven")
        self.assertEqual(payload["retrieved_skills"][0]["granularity"], "event-driven")
        self.assertEqual(payload["retrieved_skills"][0]["skill_id"], "retrieved-id")
        self.assertEqual(candidate_internal["granularity"], "event")
        self.assertEqual(retrieved_internal["granularity"], "event")

    def test_internal_to_paper_granularity_rejects_unknown_labels(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported granularity"):
            internal_skill_to_paper({"granularity": "event-driven"})

    def test_pairing_cannot_select_an_unranked_or_anchorless_group(self) -> None:
        with self.assertRaises(ValueError):
            validate_pairing(
                {"action": "select", "selected_instance_ids": ["other", "unknown"], "reason": "bad"},
                anchor_id="anchor",
                candidate_ids={"other"},
            )

    def test_r011_pairing_requires_description_evidence_for_every_selected_instance(self) -> None:
        valid = {
            "action": "select",
            "selected_instance_ids": ["anchor", "other"],
            "shared_subprocedure": "Inspect a local failure, take a corrective action, then verify its outcome.",
            "instance_evidence": [
                {"canonical_instance_id": "anchor", "description_evidence": "It describes inspection followed by recovery and verification."},
                {"canonical_instance_id": "other", "description_evidence": "It describes the same observed-recovery sequence."},
            ],
            "reason": "Both descriptions support the same substantive subprocedure.",
        }
        checked = validate_pairing(valid, anchor_id="anchor", candidate_ids={"other"}, require_shared_evidence=True)
        self.assertEqual(checked["instance_evidence"][1]["canonical_instance_id"], "other")
        invalid = dict(valid)
        invalid["instance_evidence"] = valid["instance_evidence"][:1]
        with self.assertRaisesRegex(ValueError, "one instance_evidence"):
            validate_pairing(invalid, anchor_id="anchor", candidate_ids={"other"}, require_shared_evidence=True)

    def test_event_sidecar_requires_a_local_trigger_then_response_and_outcome(self) -> None:
        trace = {
            "steps": [
                {"source_entry_id": "u", "role": "user"},
                {"source_entry_id": "a1", "role": "assistant"},
                {"source_entry_id": "r1", "role": "toolResult"},
                {"source_entry_id": "a2", "role": "assistant"},
                {"source_entry_id": "r2", "role": "toolResult"},
            ]
        }
        generated = {
            "action": "generate",
            "skill": {"title": "Inspect a local failure", "granularity": "event-driven", "when_to_apply": "A tool reports a failure", "rules": ["Inspect the observation before retrying."]},
            "evidence": {
                "trigger_step_ids": ["r1"],
                "response_step_ids": ["a2"],
                "outcome_step_ids": ["r2"],
                "rule_evidence": [{"rule_index": 0, "step_ids": ["r1", "a2", "r2"]}],
            },
        }
        checked = validate_event_extraction_with_evidence(generated, trace, benchmark="terminal-bench")
        self.assertEqual(checked["evidence"]["trigger_step_ids"], ["r1"])
        with self.assertRaisesRegex(ValueError, "absent from supplied original fragments"):
            validate_event_extraction_with_evidence(
                generated,
                trace,
                benchmark="terminal-bench",
                visible_step_ids={"r1", "a2"},
            )
        generated["evidence"]["trigger_step_ids"] = ["u"]
        with self.assertRaisesRegex(ValueError, "initial task request"):
            validate_event_extraction_with_evidence(generated, trace, benchmark="terminal-bench")

    def test_event_evidence_uses_the_same_unique_prefix_resolution(self) -> None:
        trace = {
            "steps": [
                {"source_entry_id": "00000000-user", "role": "user"},
                {"source_entry_id": "11111111-trigger", "role": "toolResult"},
                {"source_entry_id": "22222222-response", "role": "assistant"},
                {"source_entry_id": "33333333-outcome", "role": "toolResult"},
            ]
        }
        generated = {
            "action": "generate",
            "skill": {
                "title": "Repair an observed local failure",
                "granularity": "event-driven",
                "when_to_apply": "A local tool reports a repairable failure",
                "rules": ["Inspect the failure, repair it, and verify the result."],
            },
            "evidence": {
                "trigger_step_ids": ["11111111"],
                "response_step_ids": ["22222222"],
                "outcome_step_ids": ["33333333"],
                "rule_evidence": [{"rule_index": 0, "step_ids": ["11111111", "22222222", "33333333"]}],
            },
        }
        checked = validate_event_extraction_with_evidence(generated, trace, benchmark="terminal-bench")
        self.assertEqual(checked["evidence"]["trigger_step_ids"], ["11111111-trigger"])
        self.assertEqual(
            checked["evidence"]["source_id_expansions"]["outcome_step_ids"],
            {"33333333": "33333333-outcome"},
        )

    def test_r011_event_repair_rejects_any_skill_rewrite(self) -> None:
        trace = {
            "steps": [
                {"source_entry_id": "u", "role": "user"},
                {"source_entry_id": "a1", "role": "assistant"},
                {"source_entry_id": "r1", "role": "toolResult"},
                {"source_entry_id": "a2", "role": "assistant"},
                {"source_entry_id": "r2", "role": "toolResult"},
            ]
        }
        original = {
            "action": "generate",
            "skill": {
                "title": "Inspect a local failure",
                "granularity": "event-driven",
                "when_to_apply": "A tool reports a failure",
                "rules": ["Inspect the observation before retrying."],
            },
        }
        repaired = {
            **original,
            "evidence": {
                "trigger_step_ids": ["r1"],
                "response_step_ids": ["a2"],
                "outcome_step_ids": ["r2"],
                "rule_evidence": [{"rule_index": 0, "step_ids": ["r1", "a2", "r2"]}],
            },
        }
        checked = validate_event_evidence_repair(repaired, trace, original_model_output=original, benchmark="terminal-bench")
        self.assertEqual(checked["action"], "generate")
        rewritten = {**repaired, "skill": {**original["skill"], "title": "A rewritten title"}}
        with self.assertRaisesRegex(ValueError, "preserve the original skill JSON exactly"):
            validate_event_evidence_repair(rewritten, trace, original_model_output=original, benchmark="terminal-bench")

    def test_budget_summary_covers_every_step_but_keeps_a_separate_verbatim_subset(self) -> None:
        checked = validate_budget_summary(
            {"summary": "Observed a tool call and its result.", "covered_step_ids": ["a", "r"], "verbatim_evidence_step_ids": ["a", "r"]},
            segment_step_ids=["a", "r"],
        )
        self.assertEqual(checked["verbatim_evidence_step_ids"], ["a", "r"])
        with self.assertRaisesRegex(ValueError, "every segment step"):
            validate_budget_summary(
                {"summary": "missing", "covered_step_ids": ["a"], "verbatim_evidence_step_ids": ["a"]},
                segment_step_ids=["a", "r"],
            )

    def test_description_rejects_instance_identifier_as_task_family(self) -> None:
        from codeskill_rebuild.pipeline import validate_description

        trace = {
            "source": {"canonical_instance_id": "fix-git", "task_name": "terminal-bench/fix-git"},
            "steps": [{"source_entry_id": "s"}],
        }
        value = {
            "task_family": "fix-git",
            "observed_obstacle": "An observed conflict",
            "attempted_procedure": "An observed command",
            "observed_outcome": "An observed result",
            "source_step_ids": ["s"],
        }
        with self.assertRaisesRegex(ValueError, "reusable activity label"):
            validate_description(value, trace)

    def test_source_id_prefixes_expand_only_when_unique_and_keep_the_mapping(self) -> None:
        from codeskill_rebuild.pipeline import validate_description

        trace = {
            "source": {"canonical_instance_id": "task", "task_name": "terminal-bench/task"},
            "steps": [
                {"source_entry_id": "12345678-aaaa", "role": "assistant"},
                {"source_entry_id": "87654321-bbbb", "role": "toolResult"},
            ],
        }
        value = {
            "task_family": "repair a local service",
            "observed_obstacle": "The service failed its check.",
            "attempted_procedure": "The agent inspected and repaired it.",
            "observed_outcome": "The check passed.",
            "source_step_ids": ["12345678", "87654321-bbbb"],
        }
        checked = validate_description(value, trace)
        self.assertEqual(checked["source_step_ids"], ["12345678-aaaa", "87654321-bbbb"])
        self.assertEqual(checked["source_step_id_expansions"], {"12345678": "12345678-aaaa"})
        self.assertEqual(validate_description(checked, trace), checked)
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            validate_description(
                {**checked, "source_step_id_expansions": {"12345678": "87654321-bbbb"}},
                trace,
            )
        ambiguous = deepcopy(trace)
        ambiguous["steps"].append({"source_entry_id": "12345678-cccc", "role": "toolResult"})
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            validate_description(value, ambiguous)
        with self.assertRaisesRegex(ValueError, "unknown"):
            validate_description({**value, "source_step_ids": ["missing-id"]}, trace)

    def test_pairing_resolves_selected_and_evidence_prefixes_consistently(self) -> None:
        anchor = "anchor-instance-0001"
        other = "candidate-instance-0002"
        value = {
            "action": "select",
            "selected_instance_ids": ["anchor-i", "candidate-instance-0002"],
            "shared_subprocedure": "Inspect, repair, and verify.",
            "instance_evidence": [
                {"canonical_instance_id": "anchor-i", "description_evidence": "Observed repair."},
                {"canonical_instance_id": "candidate-instance-0002", "description_evidence": "Observed repair."},
            ],
            "reason": "Both descriptions show the same procedure.",
        }
        checked = validate_pairing(value, anchor_id=anchor, candidate_ids={other}, require_shared_evidence=True)
        self.assertEqual(checked["selected_instance_ids"], [anchor, other])
        self.assertEqual(checked["instance_evidence"][0]["canonical_instance_id"], anchor)
        self.assertEqual(
            checked["source_id_expansions"],
            {
                "selected_instance_ids": {"anchor-i": anchor},
                "instance_evidence": {"anchor-i": anchor},
            },
        )

    def test_maintenance_rejects_unretrieved_target_and_validates_merged_schema(self) -> None:
        with self.assertRaisesRegex(ValueError, "one retrieved skill"):
            validate_maintenance(
                {"action": "merge", "reason": "overlap", "merge_target_skill_id": "unknown", "skill": {"granularity": "event-driven"},
                 "evidence": {"source_skill_ids": ["candidate"], "source_example_ids": []}},
                candidate=candidate(),
                retrieved_skill_ids={"skill-known"},
            )
        checked = validate_maintenance(
            {
                "action": "merge",
                "reason": "same local capability",
                "merge_target_skill_id": "skill-known",
                "evidence": {"source_skill_ids": ["candidate", "skill-known"], "source_example_ids": []},
                "skill": {
                    "title": "Merged diagnostics",
                    "granularity": "event-driven",
                    "when_to_apply": "When a command emits a failure diagnostic",
                    "rules": ["Inspect the observed diagnostic before changing configuration."],
                },
            },
            candidate=candidate(),
            retrieved_skill_ids={"skill-known"},
        )
        self.assertEqual(checked["skill"]["granularity"], "event")
        self.assertEqual(validate_maintenance({"action": "drop", "reason": "redundant",
                                               "evidence": {"source_skill_ids": ["candidate"], "source_example_ids": []}},
                                              candidate=candidate(), retrieved_skill_ids=set())["action"], "drop")

    def test_contract_version_and_snapshot_come_from_the_spec_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = root / "REPRODUCTION_SPEC.md"
            decisions = root / "RESEARCH_DECISIONS.md"
            spec.write_text("# test\n\n- 文件版本：v9.7；fixture\n", encoding="utf-8")
            decisions.write_text("Rfixture", encoding="utf-8")
            contract = contract_from_files(spec, decisions)
            self.assertEqual(contract["version"], "v9.7")
            copies = write_contract_snapshot(root / "run", spec, decisions, contract)
            self.assertEqual((root / "run" / "contract" / "REPRODUCTION_SPEC.md").read_text(encoding="utf-8"), spec.read_text(encoding="utf-8"))
            self.assertEqual(copies["reproduction_spec"]["sha256"], contract["reproduction_spec_sha256"])
