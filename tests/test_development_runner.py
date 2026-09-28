from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from codeskill_rebuild.bank import SkillBank
from codeskill_rebuild.development_runner import (
    DevelopmentArms,
    DevelopmentRunnerError,
    release_development_instance,
)
from codeskill_rebuild.r012_execution import R012EvolutionMaintenanceExecutor, profile_sha256
from codeskill_rebuild.runtime import render_skill
from codeskill_rebuild.trial_schedule import InstanceBankFreeze


PROFILE = {
    "kind": "r012_execution_profile",
    "event_selection": {
        "profile_ref": "fixture-profile",
        "selection_rule_ref": "fixture-rule",
        "max_matching_skills": 2,
        "skill_token_budget": 500,
    },
    "evolution": {
        "full_lifecycle_arms": ["C"],
        "explicit_selection_manifest_required": True,
        "candidate_selection_mode": "all_actually_supplied",
    },
}


def candidate(title: str, *, source: str) -> dict:
    return {
        "skill": {
            "title": title,
            "granularity": "event",
            "when_to_apply": "After an observed command result",
            "rules": ["Use the observed result before retrying."],
            "benchmark": "terminal-bench",
        },
        "source_instance_ids": [source],
        "source_instance_ids_raw": ["terminal-bench/" + source],
        "candidate_record": {"fixture": title},
    }


class FakeEncoder:
    def index_skill(self, skill: dict) -> tuple[list[float], dict]:
        return [1.0, 0.0], {"fixture": skill["title"]}


class FakeManager:
    def __init__(self, base_skill: dict) -> None:
        self.base_skill = base_skill
        self.calls: list[str] = []

    def call_json(self, *, purpose: str, messages: list[dict], retry_of=None, call_metadata=None) -> dict:
        self.calls.append(purpose)
        if purpose.startswith("r012_evolution:"):
            return {
                "call_id": "evolution-fixture",
                "json": {
                    "action": "evolve",
                    "reason": "fixture evolution of the actually supplied skill",
                    "target_skill_id": self.base_skill["skill_id"],
                    "target_skill_version": self.base_skill["version"],
                    "skill": {
                        "title": "Improved supplied skill",
                        "granularity": "event-driven",
                        "when_to_apply": "After an observed command result",
                        "rules": ["Inspect the observed result before retrying."],
                    },
                },
            }
        return {
            "call_id": "maintenance-fixture",
            "json": {"action": "add", "reason": "fixture keeps the evolved supplied skill"},
        }


class DevelopmentRunnerTest(unittest.TestCase):
    def _coordinator(self) -> tuple[InstanceBankFreeze, dict]:
        baseline = SkillBank.empty("terminal-bench")
        extraction = SkillBank.empty("terminal-bench")
        full = SkillBank.empty("terminal-bench")
        full.apply(
            operation_id="historical-source",
            decision="add",
            candidate=candidate("Historical supplied skill", source="historical-source")["skill"],
            source_instance_ids=["historical-source"],
            evidence={"kind": "fixture historical source"},
        )
        coordinator = InstanceBankFreeze({"A": baseline, "B": extraction, "C": full}, repeat_ids=("development",))
        coordinator.freeze("terminal-bench/password-recovery")
        historical = coordinator.trial_bank("password-recovery:C:development").skills[0]
        return coordinator, historical

    @staticmethod
    def _finish(coordinator: InstanceBankFreeze, historical: dict) -> None:
        trial_ids = ["password-recovery:A:development", "password-recovery:B:development", "password-recovery:C:development"]
        for trial_id in trial_ids[:2]:
            coordinator.finish(trial_id, result_evidence={"proxy_attempt_records": []})
        block = "[CODESKILL EVENT PRIOR KNOWLEDGE]\n" + render_skill(historical)
        attempt = {
            "trial_id": trial_ids[2],
            "attempt_ordinal": 1,
            "forwarded_request_ordinal": 1,
            "proxy_outcome": "stream_forwarded",
            "forwarded_request": {"messages": [{"role": "user", "content": block}]},
            "event_selection": [
                {
                    "injected_skills": [
                        {
                            "skill": historical,
                            "phase": "event",
                            "anchor_id": "fixture-anchor",
                            "block_sha256": hashlib.sha256(block.encode()).hexdigest(),
                        }
                    ]
                }
            ],
        }
        coordinator.finish(
            trial_ids[2],
            result_evidence={
                "proxy_attempt_records": [attempt],
                "trajectory_evidence": {
                    "source": {"canonical_instance_id": "password-recovery"},
                    "instruction": "fixture task",
                    "steps": [],
                },
            },
        )

    @staticmethod
    def _manifest() -> dict:
        trial_ids = ["password-recovery:A:development", "password-recovery:B:development", "password-recovery:C:development"]
        return {
            "kind": "r012_evolution_selection_manifest",
            "instance_id": "password-recovery",
            "profile_sha256": profile_sha256(PROFILE),
            "release_order": trial_ids,
            "selections": [
                {"trial_id": trial_ids[0], "action": "skip", "reason": "baseline has no skill lifecycle"},
                {"trial_id": trial_ids[1], "action": "skip", "reason": "extraction arm has no evolution lifecycle"},
                {"trial_id": trial_ids[2], "action": "evaluate_all_supplied", "reason": "C must inspect every actually supplied skill"},
            ],
        }

    def test_freeze_then_all_finish_then_b_direct_and_c_fig9_release_feed_next_instance(self) -> None:
        coordinator, historical = self._coordinator()
        # The frozen B/C snapshots cannot contain a same-instance A candidate.
        self.assertEqual(coordinator.trial_bank("password-recovery:B:development").skills, [])
        self.assertEqual(len(coordinator.trial_bank("password-recovery:C:development").skills), 1)
        self._finish(coordinator, historical)
        with tempfile.TemporaryDirectory() as tmp:
            manager = FakeManager(historical)
            executor = R012EvolutionMaintenanceExecutor(
                manager=manager,
                encoder=FakeEncoder(),
                journal_root=Path(tmp) / "journals",
                instance_id="password-recovery",
                profile=PROFILE,
                selections={item["trial_id"]: {key: value for key, value in item.items() if key != "trial_id"} for item in self._manifest()["selections"]},
                evolution_prompt="fixture evolution prompt",
                maintenance_prompt="fixture maintenance prompt",
            )
            released = release_development_instance(
                coordinator,
                instance_id="terminal-bench/password-recovery",
                repeat_id="development",
                profile=PROFILE,
                selection_manifest=self._manifest(),
                arm_a_candidate_records=[candidate("Arm A shared candidate", source="password-recovery")],
                evolution_executor=executor,
            )
        self.assertEqual(released["kind"], "r015_development_instance_release")
        self.assertTrue(manager.calls[0].startswith("r015_arm_a_candidate_maintenance:password-recovery:C:development:001:"))
        self.assertEqual(manager.calls[1:], ["r012_evolution:password-recovery:C:development", "r012_evolution_maintenance:password-recovery:C:development:" + historical["skill_id"] + ":1"])
        self.assertEqual(
            released["arm_b_direct_extraction_ingestion"]["operations"][0]["evidence"]["kind"],
            "r015_arm_b_deterministic_extraction_only_ingestion",
        )
        self.assertEqual(released["arm_c_new_candidate_fig9_maintenance"]["operations"][0]["kind"], "r015_arm_a_candidate_fig9_maintenance_applied")
        self.assertEqual(
            released["arm_c_new_candidate_fig9_maintenance"]["operations"][0]["operation"]["evidence"]["kind"],
            "r015_arm_a_candidate_fig9_maintenance",
        )
        self.assertEqual(coordinator.instances["password-recovery"]["release_state"], "released")
        # A next instance observes only post-release banks, never the old snapshots.
        next_assignments = coordinator.freeze("portfolio-optimization")
        by_arm = {item["arm"]: item for item in next_assignments}
        self.assertEqual(len(by_arm["B"]["frozen_bank"]["skills"]), 1)
        self.assertGreaterEqual(len(by_arm["C"]["frozen_bank"]["skills"]), 3)
        self.assertEqual(
            by_arm["B"]["frozen_bank"]["skills"][0]["provenance"]["source_instance_ids"],
            ["password-recovery"],
        )

    def test_rejects_a_candidate_with_a_different_source_before_bank_release(self) -> None:
        coordinator, historical = self._coordinator()
        self._finish(coordinator, historical)
        with tempfile.TemporaryDirectory() as tmp:
            executor = R012EvolutionMaintenanceExecutor(
                manager=None,
                encoder=None,
                journal_root=Path(tmp) / "journals",
                instance_id="password-recovery",
                profile=PROFILE,
                selections={item["trial_id"]: {key: value for key, value in item.items() if key != "trial_id"} for item in self._manifest()["selections"]},
            )
            with self.assertRaisesRegex(DevelopmentRunnerError, "must cite only completed instance"):
                release_development_instance(
                    coordinator,
                    instance_id="password-recovery",
                    repeat_id="development",
                    profile=PROFILE,
                    selection_manifest=self._manifest(),
                    arm_a_candidate_records=[candidate("Wrong source", source="portfolio-optimization")],
                    evolution_executor=executor,
                )
        self.assertEqual(coordinator.instances["password-recovery"]["release_state"], "blocked_after_transaction_error")
        self.assertEqual(coordinator.arm_banks["B"].skills, [])

    def test_empty_arm_a_extraction_is_released_and_does_not_block_c_lifecycle(self) -> None:
        coordinator, historical = self._coordinator()
        self._finish(coordinator, historical)
        with tempfile.TemporaryDirectory() as tmp:
            manager = FakeManager(historical)
            executor = R012EvolutionMaintenanceExecutor(
                manager=manager,
                encoder=FakeEncoder(),
                journal_root=Path(tmp) / "journals",
                instance_id="password-recovery",
                profile=PROFILE,
                selections={item["trial_id"]: {key: value for key, value in item.items() if key != "trial_id"} for item in self._manifest()["selections"]},
                evolution_prompt="fixture evolution prompt",
                maintenance_prompt="fixture maintenance prompt",
            )
            released = release_development_instance(
                coordinator,
                instance_id="password-recovery",
                repeat_id="development",
                profile=PROFILE,
                selection_manifest=self._manifest(),
                arm_a_candidate_records=[],
                evolution_executor=executor,
            )
        self.assertEqual(released["arm_b_direct_extraction_ingestion"]["candidate_content_groups"], [])
        self.assertEqual(released["arm_c_new_candidate_fig9_maintenance"]["candidate_content_groups"], [])
        self.assertEqual(coordinator.instances["password-recovery"]["release_state"], "released")
        self.assertEqual(coordinator.arm_banks["B"].skills, [])
        self.assertEqual(manager.calls[0], "r012_evolution:password-recovery:C:development")
        next_assignments = coordinator.freeze("portfolio-optimization")
        by_arm = {item["arm"]: item for item in next_assignments}
        self.assertEqual(by_arm["B"]["frozen_bank"]["skills"], [])
        self.assertGreaterEqual(len(by_arm["C"]["frozen_bank"]["skills"]), 2)

    def test_b_exact_duplicate_is_deterministically_merged_without_a_fig9_call(self) -> None:
        baseline = SkillBank.empty("terminal-bench")
        extraction = SkillBank.empty("terminal-bench")
        duplicate = candidate("Repeated extraction", source="older-source")["skill"]
        extraction.apply(
            operation_id="older-direct-ingestion",
            decision="add",
            candidate=duplicate,
            source_instance_ids=["older-source"],
            evidence={"kind": "fixture"},
        )
        coordinator = InstanceBankFreeze({"A": baseline, "B": extraction, "C": SkillBank.empty("terminal-bench")}, repeat_ids=("development",))
        coordinator.freeze("password-recovery")
        trial_ids = ["password-recovery:A:development", "password-recovery:B:development", "password-recovery:C:development"]
        for trial_id in trial_ids:
            coordinator.finish(trial_id, result_evidence={"proxy_attempt_records": [{"trial_id": trial_id, "event_selection": [], "proxy_outcome": "stream_forwarded", "forwarded_request": {"messages": []}}]})
        with tempfile.TemporaryDirectory() as tmp:
            manager = FakeManager({"skill_id": "unused", "version": 1})
            executor = R012EvolutionMaintenanceExecutor(
                manager=manager,
                encoder=FakeEncoder(),
                journal_root=Path(tmp) / "journals",
                instance_id="password-recovery",
                profile=PROFILE,
                selections={item["trial_id"]: {key: value for key, value in item.items() if key != "trial_id"} for item in self._manifest()["selections"]},
                evolution_prompt="fixture evolution prompt",
                maintenance_prompt="fixture maintenance prompt",
            )
            released = release_development_instance(
                coordinator,
                instance_id="password-recovery",
                repeat_id="development",
                profile=PROFILE,
                selection_manifest=self._manifest(),
                arm_a_candidate_records=[candidate("Repeated extraction", source="password-recovery")],
                evolution_executor=executor,
            )
        b_operation = released["arm_b_direct_extraction_ingestion"]["operations"][0]
        self.assertEqual(b_operation["decision"], "merge")
        self.assertEqual(b_operation["evidence"]["kind"], "r015_arm_b_deterministic_extraction_only_ingestion")
        active = [skill for skill in coordinator.arm_banks["B"].skills if skill["status"] == "active"]
        self.assertEqual(len(active), 1)
        self.assertEqual(set(active[0]["provenance"]["source_instance_ids"]), {"older-source", "password-recovery"})
        self.assertEqual(len([call for call in manager.calls if call.startswith("r015_arm_a_candidate_maintenance:")]), 1)


if __name__ == "__main__":
    unittest.main()
