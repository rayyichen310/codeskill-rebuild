from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from codeskill_rebuild.bank import SkillBank
from codeskill_rebuild.r012_execution import (
    R012EvolutionMaintenanceExecutor,
    R012ExecutionError,
    evolution_messages,
    inspect_full_lifecycle_evidence,
    profile_sha256,
    validate_selection_manifest,
)
from codeskill_rebuild.runtime import render_skill
from codeskill_rebuild.trial_schedule import InstanceBankFreeze, TrialScheduleError
from codeskill_rebuild.types import read_json, write_json


BASE_CANDIDATE = {
    "title": "Inspect diagnostics before retrying",
    "granularity": "event",
    "when_to_apply": "After a command returns an unexpected diagnostic",
    "rules": ["Read the diagnostic before changing inputs."],
    "benchmark": "terminal-bench",
}


PROFILE = {
    "kind": "r012_execution_profile",
    "event_selection": {
        "profile_ref": "fixture-explicit-profile",
        "selection_rule_ref": "fixture-reviewed-rule",
        "max_matching_skills": 2,
        "skill_token_budget": 500,
    },
    "evolution": {
        "full_lifecycle_arms": ["full"],
        "explicit_selection_manifest_required": True,
        "candidate_selection_mode": "all_actually_supplied",
    },
}


class FakeManager:
    def __init__(self, outputs: list[dict]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    def call_json(self, **kwargs: object) -> dict:
        self.calls.append(dict(kwargs))
        if not self.outputs:
            raise AssertionError("unexpected manager call")
        return {"call_id": f"call-{len(self.calls):04d}", "json": self.outputs.pop(0), "preflight": {"fixture": True}}


class FakeEncoder:
    def index_skill(self, skill: dict) -> tuple[list[float], dict]:
        return [1.0], {"fixture": skill["title"]}


def profile_selection(trial_id: str, base: dict, *, action: str = "evaluate_all_supplied") -> dict:
    value = {
        "kind": "r012_evolution_selection_manifest",
        "instance_id": "target",
        "profile_sha256": profile_sha256(PROFILE),
        "release_order": [trial_id],
        "selections": [],
    }
    if action == "evaluate_all_supplied":
        value["selections"].append(
            {"trial_id": trial_id, "action": "evaluate_all_supplied", "reason": "fixture asks Fig.8 to inspect every supplied skill"}
        )
    else:
        value["selections"].append({"trial_id": trial_id, "action": "skip", "reason": "fixture explicit skip"})
    return value


def supplied_record(trial_id: str, base: dict) -> dict:
    block = "[CODESKILL EVENT PRIOR KNOWLEDGE]\n" + render_skill(base)
    return {
        "trial_id": trial_id,
        "attempt_ordinal": 1,
        "forwarded_request_ordinal": 1,
        "proxy_outcome": "stream_forwarded",
        "forwarded_request": {"messages": [{"role": "user", "content": block}]},
        "event_selection": [
            {
                "injected_skills": [
                    {
                        "skill": base,
                        "anchor_id": "anchor",
                        "block_sha256": hashlib.sha256(block.encode()).hexdigest(),
                    }
                ]
            }
        ],
    }


def no_supplied_record(trial_id: str, *, outcome: str) -> dict:
    """A durable proxy attempt with no injected skill block."""
    return {
        "trial_id": trial_id,
        "attempt_ordinal": 1,
        "forwarded_request_ordinal": 1,
        "proxy_outcome": outcome,
        "forwarded_request": {"messages": [{"role": "user", "content": "controlled task without a prior"}]},
        "event_selection": [],
    }


class R012ExecutionTest(unittest.TestCase):
    def _frozen_full_trial(self) -> tuple[InstanceBankFreeze, str, dict]:
        bank = SkillBank.empty("terminal-bench")
        added = bank.apply(
            operation_id="base-skill",
            decision="add",
            candidate=BASE_CANDIDATE,
            source_instance_ids=["source"],
            evidence={"fixture": True},
        )
        base = next(skill for skill in bank.skills if skill["skill_id"] == added["result_skill_id"])
        coordinator = InstanceBankFreeze({"full": bank}, repeat_ids=("repeat",))
        assignment = coordinator.freeze("target")[0]
        trial_id = assignment["trial_id"]
        coordinator.finish(
            trial_id,
            result_evidence={
                "proxy_attempt_records": [supplied_record(trial_id, base)],
                "trajectory_evidence": {
                    "source": {"canonical_instance_id": "target"},
                    "steps": [{"source_entry_id": "tool-result", "role": "toolResult", "content": "failure"}],
                    "outcome": {"status": "partial failure"},
                },
            },
        )
        return coordinator, trial_id, base

    def _frozen_full_no_supplied_trial(self, *, outcome: str, include_trajectory: bool) -> tuple[InstanceBankFreeze, str]:
        coordinator = InstanceBankFreeze({"full": SkillBank.empty("terminal-bench")}, repeat_ids=("repeat",))
        trial_id = coordinator.freeze("target")[0]["trial_id"]
        evidence = {"proxy_attempt_records": [no_supplied_record(trial_id, outcome=outcome)]}
        if include_trajectory:
            evidence["trajectory_evidence"] = {
                "source": {"canonical_instance_id": "target"},
                "steps": [{"source_entry_id": "tool-result", "role": "toolResult", "content": "completed without a prior"}],
                "outcome": {"status": "completed"},
            }
        coordinator.finish(trial_id, result_evidence=evidence)
        return coordinator, trial_id

    def test_evolution_then_maintenance_is_journaled_and_applied_after_all_trials_finish(self) -> None:
        coordinator, trial_id, base = self._frozen_full_trial()
        manager = FakeManager(
            [
                {
                    "action": "evolve",
                    "target_skill_id": base["skill_id"],
                    "reason": "new evidence adds a reusable check",
                    "skill": {
                        "title": "Inspect diagnostics before retrying",
                        "granularity": "event-driven",
                        "when_to_apply": "After a command returns an unexpected diagnostic",
                        "rules": ["Read the diagnostic before changing inputs.", "Check the concrete failure before retrying."],
                    },
                },
                {"action": "add", "reason": "the revised check is useful"},
            ]
        )
        selection = validate_selection_manifest(
            profile_selection(trial_id, base),
            instance_id="target",
            trial_ids=[trial_id],
            expected_profile_sha256=profile_sha256(PROFILE),
        )
        selections = selection["selections"]
        with tempfile.TemporaryDirectory() as tmp:
            executor = R012EvolutionMaintenanceExecutor(
                manager=manager,
                encoder=FakeEncoder(),
                journal_root=Path(tmp) / "journals",
                instance_id="target",
                profile=PROFILE,
                selections=selections,
                evolution_prompt="evolve",
                maintenance_prompt="maintain",
            )
            released = coordinator.release_updates(
                "target", ordered_trial_ids=[trial_id], apply_update=executor.apply_update
            )
            journals = sorted((Path(tmp) / "journals").rglob("*.json"))
            journal_values = [read_json(path) for path in journals]
        self.assertEqual(len(manager.calls), 2)
        self.assertEqual(len(released), 1)
        self.assertEqual(coordinator.instances["target"]["release_state"], "released")
        self.assertEqual(len([skill for skill in coordinator.arm_banks["full"].skills if skill["status"] == "active"]), 2)
        self.assertEqual({item["status"] for item in journal_values}, {"evolution_validated", "maintenance_applied_to_staged_bank"})
        evolution_payload = json.loads(manager.calls[0]["messages"][1]["content"])
        self.assertEqual(evolution_payload["provided_skills"][0]["skill"]["granularity"], "event-driven")
        self.assertEqual(evolution_payload["provided_skills"][0]["skill"]["skill_id"], base["skill_id"])
        self.assertEqual(evolution_payload["new_trajectory_evidence"]["source"]["canonical_instance_id"], "target")
        operation = coordinator.arm_banks["full"].operations[-1]
        self.assertEqual(operation["evidence"]["kind"], "r012_evolution_then_maintenance")
        self.assertEqual(operation["evidence"]["evolution"]["call_id"], "call-0001")
        self.assertEqual(set(operation["candidate"]["provenance"]["source_instance_ids"]), {"source", "target"})
        self.assertIn(base["skill_id"], operation["candidate"]["provenance"]["parent_skill_ids"])

    def test_fig8_payload_projects_internal_granularity_without_mutating_supplied_evidence(self) -> None:
        injected = supplied_record(
            "target:full:repeat",
            {**BASE_CANDIDATE, "skill_id": "supplied-id", "version": 1},
        )["event_selection"][0]["injected_skills"][0]
        supplied = {"skill": injected["skill"], "injection_evidence": [{"attempt_ordinal": 1}]}
        messages = evolution_messages(
            supplied=[supplied],
            trajectory_evidence={"source": {"canonical_instance_id": "target"}, "steps": []},
            paper_prompt="fig8",
        )
        payload = json.loads(messages[1]["content"])
        self.assertEqual(payload["provided_skills"][0]["skill"]["granularity"], "event-driven")
        self.assertEqual(supplied["skill"]["granularity"], "event")
        self.assertEqual(payload["provided_skills"][0]["injection_evidence"], [{"attempt_ordinal": 1}])

    def test_manager_cannot_evolve_an_unsupplied_target(self) -> None:
        coordinator, trial_id, base = self._frozen_full_trial()
        selection = validate_selection_manifest(
            profile_selection(trial_id, base),
            instance_id="target",
            trial_ids=[trial_id],
            expected_profile_sha256=profile_sha256(PROFILE),
        )
        manager = FakeManager(
            [
                {
                    "action": "evolve",
                    "target_skill_id": "retrieved-only",
                    "reason": "invalid fixture target",
                    "skill": {
                        "title": "Invalid target",
                        "granularity": "event-driven",
                        "when_to_apply": "After a result",
                        "rules": ["Do not accept it."],
                    },
                }
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            executor = R012EvolutionMaintenanceExecutor(
                manager=manager,
                encoder=FakeEncoder(),
                journal_root=Path(tmp) / "journals",
                instance_id="target",
                profile=PROFILE,
                selections=selection["selections"],
                evolution_prompt="evolve",
                maintenance_prompt="maintain",
            )
            with self.assertRaisesRegex(TrialScheduleError, "automatic replay"):
                coordinator.release_updates("target", ordered_trial_ids=[trial_id], apply_update=executor.apply_update)
        self.assertEqual(len(manager.calls), 1)
        self.assertEqual(coordinator.instances["target"]["release_state"], "blocked_after_callback_error")

    def test_full_lifecycle_skip_manifest_cannot_bypass_a_supplied_partial_failure(self) -> None:
        coordinator, trial_id, base = self._frozen_full_trial()
        selection = validate_selection_manifest(
            profile_selection(trial_id, base, action="skip"),
            instance_id="target",
            trial_ids=[trial_id],
            expected_profile_sha256=profile_sha256(PROFILE),
        )
        manager = FakeManager([])
        with tempfile.TemporaryDirectory() as tmp:
            executor = R012EvolutionMaintenanceExecutor(
                manager=manager,
                encoder=None,
                journal_root=Path(tmp) / "journals",
                instance_id="target",
                profile=PROFILE,
                selections=selection["selections"],
            )
            with self.assertRaises(TrialScheduleError) as raised:
                coordinator.release_updates("target", ordered_trial_ids=[trial_id], apply_update=executor.apply_update)
        self.assertIsInstance(raised.exception.__cause__, R012ExecutionError)
        self.assertIn("must evaluate_all_supplied", str(raised.exception.__cause__))
        self.assertEqual(manager.calls, [])
        self.assertEqual(coordinator.instances["target"]["release_state"], "blocked_after_callback_error")

    def test_full_lifecycle_no_supplied_proxy_evidence_skips_without_manager(self) -> None:
        coordinator, trial_id = self._frozen_full_no_supplied_trial(outcome="stream_forwarded", include_trajectory=True)
        selection = validate_selection_manifest(
            profile_selection(trial_id, {}, action="evaluate_all_supplied"),
            instance_id="target",
            trial_ids=[trial_id],
            expected_profile_sha256=profile_sha256(PROFILE),
        )
        manager = FakeManager([])
        with tempfile.TemporaryDirectory() as tmp:
            executor = R012EvolutionMaintenanceExecutor(
                manager=manager,
                encoder=None,
                journal_root=Path(tmp) / "journals",
                instance_id="target",
                profile=PROFILE,
                selections=selection["selections"],
            )
            released = coordinator.release_updates("target", ordered_trial_ids=[trial_id], apply_update=executor.apply_update)
        self.assertEqual(manager.calls, [])
        result = released[0]["update_result"]
        self.assertEqual(result["kind"], "r012_no_evolution_without_supplied_skill")
        self.assertEqual(result["proxy_outcomes"], ["stream_forwarded"])

    def test_full_lifecycle_infra_evidence_without_trajectory_skips_without_manager(self) -> None:
        coordinator, trial_id = self._frozen_full_no_supplied_trial(outcome="transport_open_error", include_trajectory=False)
        selection = validate_selection_manifest(
            profile_selection(trial_id, {}, action="evaluate_all_supplied"),
            instance_id="target",
            trial_ids=[trial_id],
            expected_profile_sha256=profile_sha256(PROFILE),
        )
        manager = FakeManager([])
        with tempfile.TemporaryDirectory() as tmp:
            executor = R012EvolutionMaintenanceExecutor(
                manager=manager,
                encoder=None,
                journal_root=Path(tmp) / "journals",
                instance_id="target",
                profile=PROFILE,
                selections=selection["selections"],
            )
            released = coordinator.release_updates("target", ordered_trial_ids=[trial_id], apply_update=executor.apply_update)
        self.assertEqual(manager.calls, [])
        self.assertEqual(released[0]["update_result"]["proxy_outcomes"], ["transport_open_error"])

    def test_explicit_pre_request_infra_failure_allows_no_supplied_release(self) -> None:
        trial_id = "target:full:repeat"
        evidence = {
            "kind": "r012_finished_trial_evidence",
            "classification": "infra_failure",
            "infra_failure": {
                "trial_id": trial_id,
                "instance_id": "target",
                "error_type": "AgentSetupError",
                "error": "the agent image failed before its first proxy request",
                "raw_evidence": {"harbor_log": "harbor/trial.log", "return_code": 1},
            },
            "proxy_attempt_records": [],
            "trajectory_evidence": None,
        }
        inspected = inspect_full_lifecycle_evidence(
            trial_id=trial_id,
            instance_id="target",
            result_evidence=evidence,
        )
        self.assertEqual(inspected["classification"], "infra_failure")
        self.assertEqual(inspected["supplied"], [])

    def test_unclassified_empty_proxy_evidence_still_fails_closed(self) -> None:
        with self.assertRaisesRegex(R012ExecutionError, "at least one copied proxy attempt"):
            inspect_full_lifecycle_evidence(
                trial_id="target:full:repeat",
                instance_id="target",
                result_evidence={"proxy_attempt_records": [], "trajectory_evidence": None},
            )

    def test_each_new_arm_a_candidate_uses_fig9_add_merge_or_drop(self) -> None:
        candidate = {
            **BASE_CANDIDATE,
            "title": "Use a fresh observation before retrying",
            "provenance": {
                "source_instance_ids": ["source-instance"],
                "source_instance_ids_raw": ["terminal-bench/source-instance"],
                "parent_skill_ids": [],
            },
        }
        for action in ("add", "merge", "drop"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                bank = SkillBank.empty("terminal-bench")
                existing = bank.apply(
                    operation_id="existing-skill",
                    decision="add",
                    candidate=BASE_CANDIDATE,
                    source_instance_ids=["existing-source"],
                    evidence={"fixture": True},
                )
                response = {"action": action, "reason": f"fixture {action} decision"}
                if action == "merge":
                    response["merge_target_skill_id"] = existing["result_skill_id"]
                    response["skill"] = {
                        "title": "Merged fresh-observation check",
                        "granularity": "event-driven",
                        "when_to_apply": "After a command returns a diagnostic",
                        "rules": ["Use the concrete diagnostic before retrying."],
                    }
                manager = FakeManager([response])
                executor = R012EvolutionMaintenanceExecutor(
                    manager=manager,
                    encoder=FakeEncoder(),
                    journal_root=Path(tmp) / "journals",
                    instance_id="target",
                    profile=PROFILE,
                    selections={},
                    maintenance_prompt="fixture Fig.9 prompt",
                )
                applied = executor.apply_extracted_candidate_maintenance(
                    trial_id="target:C:development",
                    bank=bank,
                    candidate=candidate,
                    candidate_ordinal=1,
                    candidate_evidence={"kind": "fixture_arm_a_candidate", "source_arm": "A"},
                )
                self.assertEqual(applied["operation"]["decision"], action)
                self.assertEqual(applied["operation"]["evidence"]["kind"], "r015_arm_a_candidate_fig9_maintenance")
                self.assertTrue(manager.calls[0]["purpose"].startswith("r015_arm_a_candidate_maintenance:target:C:development:001:"))
                journal = read_json(executor._journal_path("target:C:development", "extracted-candidate-maintenance-001"))
                self.assertEqual(journal["status"], "maintenance_applied_to_staged_bank")

    def test_visible_maintenance_mode_keeps_decision_ids_in_journal(self) -> None:
        candidate = {**BASE_CANDIDATE,
                     "provenance": {"source_instance_ids": ["source-instance"],
                                    "source_instance_ids_raw": ["source-instance"],
                                    "parent_skill_ids": []}}
        for action in ("add", "merge", "drop"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                bank = SkillBank.empty("terminal-bench")
                existing = bank.apply(operation_id="existing", decision="add",
                                      candidate=BASE_CANDIDATE,
                                      source_instance_ids=["existing-source"], evidence={"fixture": True})
                target_id = existing["result_skill_id"]
                references = {"source_skill_ids": ["candidate", *([target_id] if action == "merge" else [])],
                              "source_example_ids": []}
                decision = {"action": action, "reason": f"visible {action}", "evidence": references}
                if action == "merge":
                    decision.update({"merge_target_skill_id": target_id,
                                     "skill": {"title": "Merged diagnostic", "granularity": "event-driven",
                                               "when_to_apply": "After a diagnostic.",
                                               "rules": ["Read the diagnostic before retrying."]},
                                     "code_example_changes": []})
                manager = FakeManager([decision])
                executor = R012EvolutionMaintenanceExecutor(
                    manager=manager, encoder=FakeEncoder(), journal_root=Path(tmp) / "journals",
                    instance_id="target", profile=PROFILE, selections={},
                    maintenance_prompt="visible Fig.9", use_visible_maintenance=True)
                applied = executor.apply_extracted_candidate_maintenance(
                    trial_id="target:C:visible", bank=bank, candidate=candidate,
                    candidate_ordinal=1, candidate_evidence={"fixture": True})
                payload = json.loads(manager.calls[0]["messages"][1]["content"])
                self.assertEqual(payload["candidate_skill"]["skill_id"], "candidate")
                self.assertNotIn("provenance", json.dumps(payload))
                self.assertEqual(applied["operation"]["evidence"]["maintenance"]["validated"]["evidence"],
                                 references)
                journal = read_json(executor._journal_path("target:C:visible",
                                                           "extracted-candidate-maintenance-001"))
                self.assertEqual(journal["validated"]["evidence"], references)

    def test_existing_pre_call_journal_blocks_replay_before_manager_contact(self) -> None:
        coordinator, trial_id, base = self._frozen_full_trial()
        selection = validate_selection_manifest(
            profile_selection(trial_id, base),
            instance_id="target",
            trial_ids=[trial_id],
            expected_profile_sha256=profile_sha256(PROFILE),
        )
        manager = FakeManager([])
        with tempfile.TemporaryDirectory() as tmp:
            executor = R012EvolutionMaintenanceExecutor(
                manager=manager,
                encoder=FakeEncoder(),
                journal_root=Path(tmp) / "journals",
                instance_id="target",
                profile=PROFILE,
                selections=selection["selections"],
                evolution_prompt="evolve",
                maintenance_prompt="maintain",
            )
            write_json(executor._journal_path(trial_id, "evolution"), {"status": "prepared_before_manager_call"})
            with self.assertRaisesRegex(TrialScheduleError, "automatic replay"):
                coordinator.release_updates("target", ordered_trial_ids=[trial_id], apply_update=executor.apply_update)
        self.assertEqual(manager.calls, [])

    def test_merge_unions_base_target_and_current_instance_provenance(self) -> None:
        coordinator, trial_id, base = self._frozen_full_trial()
        bank = coordinator.arm_banks["full"]
        other = bank.apply(
            operation_id="other-skill",
            decision="add",
            candidate={**BASE_CANDIDATE, "title": "Check a second diagnostic path"},
            source_instance_ids=["other-source"],
            evidence={"fixture": True},
        )
        # The frozen assignment must also contain the second skill, so rebuild
        # the fixture after setting up the bank rather than leaking a live edit.
        coordinator = InstanceBankFreeze({"full": bank}, repeat_ids=("repeat",))
        assignment = coordinator.freeze("target")[0]
        trial_id = assignment["trial_id"]
        base = next(skill for skill in bank.skills if skill["skill_id"] == base["skill_id"])
        coordinator.finish(
            trial_id,
            result_evidence={
                "proxy_attempt_records": [supplied_record(trial_id, base)],
                "trajectory_evidence": {"source": {"canonical_instance_id": "target"}, "steps": [{"source_entry_id": "r"}], "outcome": {}},
            },
        )
        selection = validate_selection_manifest(
            profile_selection(trial_id, base),
            instance_id="target",
            trial_ids=[trial_id],
            expected_profile_sha256=profile_sha256(PROFILE),
        )
        manager = FakeManager(
            [
                {
                    "action": "evolve",
                    "target_skill_id": base["skill_id"],
                    "reason": "new evidence",
                    "skill": {
                        "title": "Inspect diagnostics before retrying",
                        "granularity": "event-driven",
                        "when_to_apply": "After a command returns an unexpected diagnostic",
                        "rules": ["Read every relevant diagnostic before retrying."],
                    },
                },
                {
                    "action": "merge",
                    "merge_target_skill_id": other["result_skill_id"],
                    "reason": "combine both diagnostic checks",
                    "skill": {
                        "title": "Inspect related diagnostics before retrying",
                        "granularity": "event-driven",
                        "when_to_apply": "After a command returns a diagnostic",
                        "rules": ["Read every relevant diagnostic before retrying."],
                    },
                },
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            executor = R012EvolutionMaintenanceExecutor(
                manager=manager,
                encoder=FakeEncoder(),
                journal_root=Path(tmp) / "journals",
                instance_id="target",
                profile=PROFILE,
                selections=selection["selections"],
                evolution_prompt="evolve",
                maintenance_prompt="maintain",
            )
            coordinator.release_updates("target", ordered_trial_ids=selection["release_order"], apply_update=executor.apply_update)
        operation = coordinator.arm_banks["full"].operations[-1]
        merged = next(skill for skill in coordinator.arm_banks["full"].skills if skill["skill_id"] == operation["result_skill_id"])
        self.assertEqual(set(merged["provenance"]["source_instance_ids"]), {"source", "other-source", "target"})
        self.assertTrue({base["skill_id"], other["result_skill_id"]} <= set(merged["provenance"]["parent_skill_ids"]))
        for instance_id in ("source", "other-source", "target"):
            eligible_ids = {skill["skill_id"] for skill in coordinator.arm_banks["full"].eligible(instance_id=instance_id, granularity="event")}
            self.assertNotIn(merged["skill_id"], eligible_ids)

    def test_serialized_freeze_state_preserves_blocked_and_released_gates(self) -> None:
        coordinator, trial_id, _base = self._frozen_full_trial()
        restored = InstanceBankFreeze.from_dict(coordinator.to_dict())
        self.assertEqual(restored._assignment(trial_id)["status"], "finished")
        restored.release_updates("target", ordered_trial_ids=[trial_id], apply_update=lambda *_args: {"skip": True})
        again = InstanceBankFreeze.from_dict(restored.to_dict())
        with self.assertRaisesRegex(TrialScheduleError, "automatic replay"):
            again.release_updates("target", ordered_trial_ids=[trial_id], apply_update=lambda *_args: {})

    def test_profile_and_selection_require_explicit_values_without_candidate_cap_defaults(self) -> None:
        with self.assertRaisesRegex(R012ExecutionError, "event_selection and evolution"):
            R012EvolutionMaintenanceExecutor(
                manager=None,
                encoder=None,
                journal_root=Path("unused"),
                instance_id="target",
                profile={"kind": "r012_execution_profile"},
                selections={},
            )
        self.assertNotIn("max_candidates", PROFILE["evolution"])


if __name__ == "__main__":
    unittest.main()
