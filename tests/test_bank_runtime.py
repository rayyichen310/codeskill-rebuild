from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from codeskill_rebuild.bank import BankError, SkillBank
from codeskill_rebuild.context import plan_context
from codeskill_rebuild.pipeline import (
    paper_skill_to_internal,
    validate_budget_summary,
    validate_event_evidence_repair,
    validate_event_extraction_with_evidence,
    validate_maintenance,
    validate_pairing,
)
from codeskill_rebuild.runtime import build_initial_messages, inject_event_message
from codeskill_rebuild.types import contract_from_files, write_contract_snapshot


def candidate(title: str = "Check compiler diagnostics") -> dict:
    return {"title": title, "granularity": "event", "when_to_apply": "When a build command exits unsuccessfully", "rules": ["Read the first failing diagnostic before changing configuration."], "benchmark": "terminal-bench"}


class BankAndRuntimeTest(unittest.TestCase):
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

    def test_maintenance_rejects_unretrieved_target_and_validates_merged_schema(self) -> None:
        with self.assertRaisesRegex(ValueError, "one retrieved skill"):
            validate_maintenance(
                {"action": "merge", "reason": "overlap", "merge_target_skill_id": "unknown", "skill": {"granularity": "event-driven"}},
                candidate=candidate(),
                retrieved_skill_ids={"skill-known"},
            )
        checked = validate_maintenance(
            {
                "action": "merge",
                "reason": "same local capability",
                "merge_target_skill_id": "skill-known",
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
        self.assertEqual(validate_maintenance({"action": "drop", "reason": "redundant"}, candidate=candidate(), retrieved_skill_ids=set())["action"], "drop")

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
