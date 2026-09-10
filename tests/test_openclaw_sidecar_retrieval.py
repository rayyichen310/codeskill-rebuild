from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from codeskill_rebuild.bank import SkillBank
from codeskill_rebuild.openclaw_overlay import DurableOverlay, EventSelectionSettings, OverlayEventSkillBudgetError
from codeskill_rebuild.types import canonical_json
from codeskill_rebuild.openclaw_sidecar_retrieval import FrozenBankSelectors, SidecarRetrievalError
from codeskill_rebuild.r012_execution import profile_sha256
from codeskill_rebuild.trial_schedule import InstanceBankFreeze
from codeskill_rebuild.types import sha256_file


def skill(title: str, granularity: str) -> dict[str, object]:
    return {
        "title": title,
        "granularity": granularity,
        "when_to_apply": "when this exact condition appears",
        "rules": ["perform the recorded check"],
        "benchmark": "terminal-bench",
    }


class FakeEncoder:
    def load(self) -> dict[str, object]:
        return {"kind": "fake"}

    def index_skill(self, value: dict[str, object]) -> tuple[list[float], dict[str, object]]:
        return [1.0, 0.0], {"text": str(value["title"]), "kind": "fake-index"}

    def encode_query(self, query_type: str, fields: dict[str, str]) -> tuple[list[float], dict[str, object]]:
        return [1.0, 0.0], {"query_type": query_type, "fields": fields, "kind": "fake-query"}


class FrozenBankSidecarRetrievalTest(unittest.TestCase):
    profile = {
        "kind": "r012_execution_profile",
        "event_selection": {
            "profile_ref": "approved-development-profile",
            "selection_rule_ref": "approved-event-rule",
            "max_matching_skills": 2,
            "skill_token_budget": 500,
        },
        "evolution": {
            "full_lifecycle_arms": ["full"],
            "explicit_selection_manifest_required": True,
            "candidate_selection_mode": "all_actually_supplied",
        },
        "sidecar_injection": {
            "event_skill_token_budget_scope": "complete_payload_active_event_blocks_delta",
            "arms": {
                "baseline": {"enable_task": False, "enable_event": False},
                "retrieval_only": {"enable_task": True, "enable_event": False},
                "full": {"enable_task": True, "enable_event": True},
            },
        },
    }

    def _state_and_config(self, directory: Path, *, arm: str = "full", skill_budget: int = 500) -> tuple[Path, dict[str, object]]:
        directory.mkdir(parents=True, exist_ok=True)
        profile = deepcopy(self.profile)
        profile["event_selection"]["skill_token_budget"] = skill_budget
        bank = SkillBank.empty("terminal-bench")
        bank.apply(
            operation_id="source-task",
            decision="add",
            candidate=skill("source task", "task"),
            source_instance_ids=["terminal-bench/target"],
            evidence={"kind": "fixture"},
        )
        bank.apply(
            operation_id="safe-task",
            decision="add",
            candidate=skill("safe task", "task"),
            source_instance_ids=["other"],
            evidence={"kind": "fixture"},
        )
        bank.apply(
            operation_id="source-event",
            decision="add",
            candidate=skill("source event", "event"),
            source_instance_ids=["target"],
            evidence={"kind": "fixture"},
        )
        bank.apply(
            operation_id="safe-event",
            decision="add",
            candidate=skill("safe event", "event"),
            source_instance_ids=["other"],
            evidence={"kind": "fixture"},
        )
        coordinator = InstanceBankFreeze({"baseline": bank, "retrieval_only": bank, "full": bank}, repeat_ids=("one",))
        assignment = next(item for item in coordinator.freeze("terminal-bench/target") if item["arm"] == arm)
        state = {
            "kind": "r012_instance_lifecycle_state",
            "profile": profile,
            "profile_sha256": profile_sha256(profile),
            "coordinator": coordinator.to_dict(),
        }
        state_path = directory / "lifecycle.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        config: dict[str, object] = {
            "trialId": assignment["trial_id"],
            "retrieval": {
                "trialId": assignment["trial_id"],
                "instanceId": "terminal-bench/target",
                "lifecycleStatePath": str(state_path),
                "lifecycleStateSha256": sha256_file(state_path),
                "profileSha256": profile_sha256(profile),
                "bankSnapshotSha256": assignment["frozen_bank"]["state_sha256"],
                "encoder": {"kind": "minilm", "repoId": "sentence-transformers/all-MiniLM-L6-v2", "revision": "fixture-revision"},
                "taskSelection": {"selectionRuleRef": "explicit-task-rule", "threshold": 0.0, "maxMatchingSkills": 2},
                "eventSelection": {
                    "profileRef": "approved-development-profile",
                    "selectionRuleRef": "approved-event-rule",
                    "threshold": 0.0,
                    "maxMatchingSkills": 2,
                    "skillTokenBudget": skill_budget,
                    "budgetScope": "complete_payload_active_event_blocks_delta",
                },
            },
        }
        return state_path, config

    def test_real_selector_uses_only_the_frozen_bank_and_excludes_same_instance_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _state, config = self._state_and_config(Path(tmp))
            selectors = FrozenBankSelectors.from_config(config, encoder=FakeEncoder())
            task = selectors.select_task({"role": "user", "content": "repair target"}, [{"role": "user", "content": "repair target"}])
            event = selectors.select_event(
                {"assistant_index": 1, "tool_result_indices": [2]},
                [
                    {"role": "user", "content": "repair target"},
                    {"role": "assistant", "content": "ran test", "tool_calls": [{"id": "call-1"}]},
                    {"role": "tool", "tool_call_id": "call-1", "content": "test failure"},
                ],
            )
        self.assertEqual([item["title"] for item in task["skills"]], ["safe task"])
        self.assertEqual([item["title"] for item in event["skills"]], ["safe event"])
        self.assertEqual(task["query"]["bank_snapshot"]["sequence"], 4)
        self.assertEqual(event["query"]["selection_rule"]["limit"], 2)

    def test_frozen_per_arm_controls_distinguish_no_skills_retrieval_only_and_full_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = FrozenBankSelectors.from_config(self._state_and_config(root / "a", arm="baseline")[1], encoder=FakeEncoder())
            retrieval_only = FrozenBankSelectors.from_config(self._state_and_config(root / "b", arm="retrieval_only")[1], encoder=FakeEncoder())
            full = FrozenBankSelectors.from_config(self._state_and_config(root / "c", arm="full")[1], encoder=FakeEncoder())
            with self.assertRaisesRegex(SidecarRetrievalError, "disables task"):
                baseline.select_task({"role": "user", "content": "repair target"}, [{"role": "user", "content": "repair target"}])
            task = retrieval_only.select_task({"role": "user", "content": "repair target"}, [{"role": "user", "content": "repair target"}])
            with self.assertRaisesRegex(SidecarRetrievalError, "disables event"):
                retrieval_only.select_event({"assistant_index": 0, "tool_result_indices": []}, [{"role": "assistant", "content": "tool call"}])
        self.assertEqual((baseline.rules.enable_task, baseline.rules.enable_event), (False, False))
        self.assertEqual((retrieval_only.rules.enable_task, retrieval_only.rules.enable_event), (True, False))
        self.assertEqual((full.rules.enable_task, full.rules.enable_event), (True, True))
        self.assertEqual([item["title"] for item in task["skills"]], ["safe task"])

    def test_durable_overlay_injects_frozen_bank_results_at_its_actual_request_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _state, config = self._state_and_config(directory)
            selectors = FrozenBankSelectors.from_config(config, encoder=FakeEncoder())
            overlay = DurableOverlay(
                trial_id=str(config["trialId"]),
                state_path=directory / "overlay.json",
                evidence_dir=directory / "evidence",
                token_counter=lambda _payload: 1,
                task_selector=selectors.select_task,
                event_selector=selectors.select_event,
                event_selection_settings=EventSelectionSettings(max_matching_skills=2, profile_ref="approved-development-profile"),
            )
            first, _first_record = overlay.prepare({"messages": [{"role": "user", "content": "repair target"}]})
            second, second_record = overlay.prepare(
                {
                    "messages": [
                        {"role": "user", "content": "repair target"},
                        {"role": "assistant", "content": "ran test", "tool_calls": [{"id": "call-1"}]},
                        {"role": "tool", "tool_call_id": "call-1", "content": "test failure"},
                    ]
                }
            )
        self.assertIn("safe task", first["messages"][0]["content"])
        event_blocks = [item["content"] for item in second["messages"] if item.get("role") == "user" and "EVENT PRIOR" in str(item.get("content"))]
        self.assertEqual(len(event_blocks), 1)
        self.assertIn("safe event", event_blocks[0])
        self.assertEqual(second_record["event_selection"][0]["query"]["kind"], "r013_frozen_bank_minilm_retrieval")

    def test_event_skill_budget_uses_complete_payload_delta_and_fails_closed_or_passes_at_boundary(self) -> None:
        def prepare_with_budget(directory: Path, budget: int):
            _state, config = self._state_and_config(directory, skill_budget=budget)
            selectors = FrozenBankSelectors.from_config(config, encoder=FakeEncoder())
            overlay = DurableOverlay(
                trial_id=str(config["trialId"]),
                state_path=directory / "overlay.json",
                evidence_dir=directory / "evidence",
                token_counter=lambda payload: len(canonical_json(payload)),
                task_selector=selectors.select_task,
                event_selector=selectors.select_event,
                event_selection_settings=EventSelectionSettings(
                    max_matching_skills=selectors.rules.event_max_matching_skills,
                    profile_ref=selectors.rules.event_profile_ref,
                    skill_token_budget=selectors.rules.event_skill_token_budget,
                    skill_token_budget_scope=selectors.rules.event_skill_token_budget_scope,
                ),
            )
            overlay.prepare({"messages": [{"role": "user", "content": "repair target"}], "tools": [{"type": "function", "function": {"name": "test"}}]})
            return overlay, {
                "messages": [
                    {"role": "user", "content": "repair target"},
                    {"role": "assistant", "content": "ran test", "tool_calls": [{"id": "call-1"}]},
                    {"role": "tool", "tool_call_id": "call-1", "content": "test failure"},
                ],
                "tools": [{"type": "function", "function": {"name": "test"}}],
            }

        with tempfile.TemporaryDirectory() as tmp:
            overlay, event_payload = prepare_with_budget(Path(tmp) / "measure", 10000)
            _forwarded, measured = overlay.prepare(event_payload)
            delta = measured["event_skill_token_budget"]["active_event_block_tokens"]
        with tempfile.TemporaryDirectory() as tmp:
            overlay, event_payload = prepare_with_budget(Path(tmp) / "over", 1)
            with self.assertRaisesRegex(OverlayEventSkillBudgetError, "frozen event budget"):
                overlay.prepare(event_payload)
            state = json.loads(overlay.state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["events"], [])
        with tempfile.TemporaryDirectory() as tmp:
            overlay, event_payload = prepare_with_budget(Path(tmp) / "boundary", delta)
            _forwarded, at_boundary = overlay.prepare(event_payload)
            _retry_forwarded, retry = overlay.prepare(event_payload)
        budget_evidence = at_boundary["event_skill_token_budget"]
        self.assertEqual(budget_evidence["active_event_block_tokens"], delta)
        self.assertEqual(budget_evidence["budget"], delta)
        self.assertEqual(budget_evidence["scope"], "complete_payload_active_event_blocks_delta")
        self.assertEqual(retry["event_selection"], [])
        self.assertEqual(retry["event_skill_token_budget"]["active_event_block_tokens"], delta)

    def test_rejects_profile_outside_the_frozen_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _state, config = self._state_and_config(Path(tmp))
            config["retrieval"]["eventSelection"]["profileRef"] = "unapproved-other-profile"  # type: ignore[index]
            with self.assertRaisesRegex(SidecarRetrievalError, "differs from the frozen R012 profile"):
                FrozenBankSelectors.from_config(config, encoder=FakeEncoder())

    def test_rejects_event_budget_scope_outside_the_frozen_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _state, config = self._state_and_config(Path(tmp))
            config["retrieval"]["eventSelection"]["budgetScope"] = "per_new_event"  # type: ignore[index]
            with self.assertRaisesRegex(SidecarRetrievalError, "budgetScope differs from the frozen R012 profile"):
                FrozenBankSelectors.from_config(config, encoder=FakeEncoder())

    def test_rejects_invalid_frozen_bank_snapshot_before_any_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path, config = self._state_and_config(Path(tmp))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            assignment = next(
                item
                for item in next(iter(state["coordinator"]["instances"].values()))["assignments"].values()
                if item["trial_id"] == config["trialId"]
            )
            assignment["frozen_bank"]["state_sha256"] = "not-a-bank-hash"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            config["retrieval"]["lifecycleStateSha256"] = sha256_file(state_path)  # type: ignore[index]
            with self.assertRaisesRegex(SidecarRetrievalError, "invalid state_sha256"):
                FrozenBankSelectors.from_config(config, encoder=FakeEncoder())

    def test_rejects_session_trial_that_is_not_a_frozen_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _state, config = self._state_and_config(Path(tmp))
            config["trialId"] = "target:full:other"  # type: ignore[index]
            config["retrieval"]["trialId"] = "target:full:other"  # type: ignore[index]
            with self.assertRaisesRegex(SidecarRetrievalError, "not a frozen lifecycle assignment"):
                FrozenBankSelectors.from_config(config, encoder=FakeEncoder())

    def test_rejects_an_arm_without_frozen_sidecar_injection_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path, config = self._state_and_config(Path(tmp))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            del state["profile"]["sidecar_injection"]["arms"]["baseline"]
            state["profile_sha256"] = profile_sha256(state["profile"])
            assignment = next(
                item
                for item in next(iter(state["coordinator"]["instances"].values()))["assignments"].values()
                if item["trial_id"] == config["trialId"]
            )
            assignment["arm"] = "baseline"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            config["retrieval"]["lifecycleStateSha256"] = sha256_file(state_path)  # type: ignore[index]
            config["retrieval"]["profileSha256"] = profile_sha256(state["profile"])  # type: ignore[index]
            with self.assertRaisesRegex(SidecarRetrievalError, "sidecar_injection.arms.baseline"):
                FrozenBankSelectors.from_config(config, encoder=FakeEncoder())


if __name__ == "__main__":
    unittest.main()
