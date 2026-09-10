from __future__ import annotations

import unittest
import json

from codeskill_rebuild.bank import SkillBank
from codeskill_rebuild.event_extraction import EventExtractionSchedule
from codeskill_rebuild.evolution import EvolutionEvidenceError, select_evolution_candidate, supplied_skills_for_evolution
from codeskill_rebuild.pairing_audit import no_related_group_audit
from codeskill_rebuild.runtime import render_skill
from codeskill_rebuild.trial_schedule import InstanceBankFreeze, TrialScheduleError


EVENT = {
    "skill_id": "event-1",
    "version": 1,
    "title": "Inspect output",
    "granularity": "event",
    "when_to_apply": "After a tool result",
    "rules": ["Check the output before retrying."],
    "benchmark": "terminal-bench",
}
TASK_2 = {**EVENT, "skill_id": "task-2", "version": 3, "granularity": "task", "title": "Inspect task context"}
TASK_3 = {**EVENT, "skill_id": "task-3", "version": 2, "granularity": "task", "title": "Preserve verification"}


def trace(instance_id: str = "source") -> dict:
    return {
        "source": {"canonical_instance_id": instance_id},
        "instruction": "Fix the task.",
        "text_manager_eligible": True,
        "steps": [
            {"source_entry_id": "u", "role": "user", "content": [{"type": "text", "text": "task"}]},
            {"source_entry_id": "a", "role": "assistant", "content": [{"type": "text", "text": "inspect"}]},
            {"source_entry_id": "r", "role": "toolResult", "content": [{"type": "text", "text": "result"}]},
        ],
        "outcome": {"official_reward": "0"},
    }


class R012LifecycleTest(unittest.TestCase):
    def test_event_extraction_stops_on_duplicate_or_skip_and_retries_are_separate(self) -> None:
        schedule = EventExtractionSchedule(trace())
        first = schedule.record_initial_result(
            {"action": "generate", "skill": {"title": "one"}}, model_call_id="call-1", evidence={"request": "one"}
        )
        self.assertEqual(schedule.next_initial_attempt_ordinal, 2)
        duplicate = schedule.record_initial_result(
            {"action": "generate", "skill": {"title": "one"}}, model_call_id="call-2", evidence={"request": "two"}
        )
        self.assertEqual(duplicate["outcome"], "duplicate")
        self.assertEqual(schedule.stop_reason, "duplicate")
        retry = schedule.record_retry(retry_of=first, retry_kind="format_repair", model_call_id="call-repair", evidence={})
        self.assertEqual(retry["initial_attempt_count_unchanged"], 2)
        self.assertEqual(schedule.next_initial_attempt_ordinal, None)
        skipped = EventExtractionSchedule(trace("skip-source"))
        skipped.record_initial_result({"action": "skip", "reason": "no local event"}, model_call_id="call-3", evidence={})
        self.assertEqual(skipped.stop_reason, "skip")
        self.assertEqual(skipped.manifest()["maximum_initial_attempts"], 3)

    def test_second_event_attempt_gets_compact_prior_content_and_step_refs(self) -> None:
        schedule = EventExtractionSchedule(trace())
        generated = {
            "action": "generate",
            "skill": {"title": "Inspect result", "when_to_apply": "After a tool result", "rules": ["Inspect it."], "granularity": "event", "benchmark": "terminal-bench"},
            "evidence": {"trigger_step_ids": ["r"], "response_step_ids": ["a"], "outcome_step_ids": ["r"]},
        }
        schedule.record_initial_result(generated, model_call_id="call-1", evidence={})
        payload = json.loads(schedule.messages_for_next_initial_attempt(runtime_prompt="prompt")[1]["content"])
        self.assertEqual(payload["previous_event_candidate_ids"], schedule.prior_candidate_ids)
        self.assertEqual(payload["previous_event_candidates"][0]["content"]["title"], "Inspect result")
        self.assertEqual(payload["previous_event_candidates"][0]["step_references"]["trigger_step_ids"], ["r"])

    def test_evolution_uses_retired_event_only_when_a_forwarded_request_proves_injection(self) -> None:
        block = "[CODESKILL EVENT PRIOR KNOWLEDGE]\n" + render_skill(EVENT)
        record = {
            "trial_id": "trial-1",
            "proxy_outcome": "stream_forwarded",
            "attempt_ordinal": 2,
            "forwarded_request_ordinal": 2,
            "forwarded_request": {"messages": [{"role": "user", "content": block}]},
            "event_selection": [
                {
                    "injected_skills": [
                        {
                            "skill": EVENT,
                            "phase": "event",
                            "anchor_id": "retired-anchor",
                            "block_sha256": __import__("hashlib").sha256(block.encode()).hexdigest(),
                        }
                    ]
                }
            ],
            "retired_event_priors": {"events": [{"skill_id": "event-1", "version": 1}]},
        }
        supplied = supplied_skills_for_evolution([record], trial_id="trial-1")
        self.assertEqual([item["skill"]["skill_id"] for item in supplied], ["event-1"])
        evolution = select_evolution_candidate(
            supplied=supplied,
            selected_skill_id="event-1",
            selected_version=1,
            new_trace_evidence={"trace_path": "new.json", "step_ids": ["r"]},
        )
        self.assertEqual(evolution["action"], "evolve")
        with self.assertRaisesRegex(EvolutionEvidenceError, "actually supplied"):
            select_evolution_candidate(
                supplied=supplied,
                selected_skill_id="retrieved-only",
                selected_version=1,
                new_trace_evidence={"step_ids": ["r"]},
            )

    def test_evolution_recovers_multi_task_injection_after_transport_failure_then_retry(self) -> None:
        task_block = "[CODESKILL TASK PRIOR KNOWLEDGE]\n" + render_skill(TASK_2) + "\n\n" + render_skill(TASK_3)
        task_selection = {
            "block": task_block,
            "block_sha256": __import__("hashlib").sha256(task_block.encode()).hexdigest(),
            "injected_skills": [
                {"skill": TASK_2, "phase": "task", "rendered_skill_sha256": __import__("hashlib").sha256(render_skill(TASK_2).encode()).hexdigest()},
                {"skill": TASK_3, "phase": "task", "rendered_skill_sha256": __import__("hashlib").sha256(render_skill(TASK_3).encode()).hexdigest()},
            ],
        }
        records = [
            {
                "trial_id": "trial-retry",
                "attempt_ordinal": 1,
                "forwarded_request_ordinal": 1,
                "proxy_outcome": "transport_open_error",
                "forwarded_request": {"messages": [{"role": "user", "content": "task\n\n" + task_block}]},
                "task_selection": task_selection,
                "event_selection": [],
            },
            {
                "trial_id": "trial-retry",
                "attempt_ordinal": 2,
                "forwarded_request_ordinal": 2,
                "proxy_outcome": "stream_forwarded",
                "forwarded_request": {"messages": [{"role": "user", "content": "task\n\n" + task_block}]},
                "event_selection": [],
            },
        ]
        supplied = supplied_skills_for_evolution(records, trial_id="trial-retry")
        self.assertEqual([item["skill"]["skill_id"] for item in supplied], ["task-2", "task-3"])
        self.assertEqual([item["injection_evidence"][0]["attempt_ordinal"] for item in supplied], [2, 2])

    def test_all_arms_and_repeats_freeze_before_any_update(self) -> None:
        banks = {arm: SkillBank.empty("terminal-bench") for arm in ("no-skill", "extraction", "full")}
        coordinator = InstanceBankFreeze(banks, repeat_ids=("repeat-1", "repeat-2"))
        assignments = coordinator.freeze("terminal-bench/target")
        self.assertEqual(len(assignments), 6)
        self.assertEqual({item["frozen_bank"]["sequence"] for item in assignments}, {0})
        coordinator.arm_banks["full"].apply(
            operation_id="after-freeze",
            decision="add",
            candidate={key: EVENT[key] for key in ("title", "granularity", "when_to_apply", "rules", "benchmark")},
            source_instance_ids=["source"],
            evidence={},
        )
        self.assertEqual(coordinator.trial_bank("target:full:repeat-1").sequence, 0)
        for assignment in assignments[:-1]:
            coordinator.finish(assignment["trial_id"], result_evidence={"status": "finished"})
        with self.assertRaisesRegex(TrialScheduleError, "before every arm"):
            coordinator.release_updates(
                "target", ordered_trial_ids=[item["trial_id"] for item in assignments], apply_update=lambda *_args: None
            )
        coordinator.finish(assignments[-1]["trial_id"], result_evidence={"status": "finished"})
        applied: list[str] = []
        result = coordinator.release_updates(
            "target",
            ordered_trial_ids=[item["trial_id"] for item in assignments],
            apply_update=lambda trial_id, _bank, _assignment: applied.append(trial_id) or {"released": trial_id},
        )
        self.assertEqual(applied, [item["trial_id"] for item in assignments])
        self.assertEqual(len(result), 6)

    def test_failed_release_keeps_live_banks_unchanged_and_blocks_replay_or_next_instance(self) -> None:
        coordinator = InstanceBankFreeze({"full": SkillBank.empty("terminal-bench")}, repeat_ids=("repeat-1", "repeat-2"))
        assignments = coordinator.freeze("first")
        for assignment in assignments:
            coordinator.finish(assignment["trial_id"], result_evidence={})
        original = coordinator.arm_banks["full"].to_dict()
        calls: list[str] = []

        def fail_on_second(trial_id: str, bank: SkillBank, _assignment: dict) -> dict:
            calls.append(trial_id)
            if len(calls) == 2:
                raise RuntimeError("transport interrupted after first completed call")
            bank.apply(
                operation_id=trial_id,
                decision="add",
                candidate={key: EVENT[key] for key in ("title", "granularity", "when_to_apply", "rules", "benchmark")},
                source_instance_ids=["source"],
                evidence={},
            )
            return {"call": trial_id}

        with self.assertRaisesRegex(TrialScheduleError, "automatic replay"):
            coordinator.release_updates("first", ordered_trial_ids=[item["trial_id"] for item in assignments], apply_update=fail_on_second)
        self.assertEqual(coordinator.arm_banks["full"].to_dict(), original)
        self.assertEqual(coordinator.instances["first"]["completed_release_evidence"][0]["trial_id"], assignments[0]["trial_id"])
        with self.assertRaisesRegex(TrialScheduleError, "automatic replay"):
            coordinator.release_updates("first", ordered_trial_ids=[item["trial_id"] for item in assignments], apply_update=lambda *_args: {})
        with self.assertRaisesRegex(TrialScheduleError, "unreleased or blocked"):
            coordinator.freeze("second")

    def test_no_related_group_audit_preserves_raw_evidence_without_classifying_it(self) -> None:
        audit = no_related_group_audit(
            anchor_trace=trace("anchor"),
            anchor_description={"task_family": "inspection", "source_step_ids": ["a"]},
            candidates=[{"trace": trace("candidate"), "description": {"task_family": "verification", "source_step_ids": ["r"]}}],
            pairing_result={"action": "no_related_group", "reason": "descriptions do not establish a shared procedure"},
        )
        self.assertIsNone(audit["automated_classification"])
        self.assertEqual(audit["anchor"]["description_cited_steps"][0]["source_entry_id"], "a")
        self.assertIn("u", audit["candidates"][0]["uncited_raw_step_ids"])


if __name__ == "__main__":
    unittest.main()
