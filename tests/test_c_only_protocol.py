from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from codeskill_rebuild.c_only_protocol import COnlyProtocol, COnlyProtocolError
from codeskill_rebuild.types import canonical_json, read_json, sha256_file, sha256_text, write_json


def _skill(title: str, source: str, *, granularity: str = "event") -> dict[str, object]:
    return {
        "title": title,
        "granularity": granularity,
        "when_to_apply": f"When working on {source}.",
        "rules": ["Inspect the local evidence before changing files."],
        "benchmark": "terminal-bench",
        "provenance": {
            "source_instance_ids": [source],
            "source_instance_ids_raw": [f"terminal-bench/{source}"],
            "parent_skill_ids": [],
        },
    }


def _trajectory(round_id: int, task_id: str, suffix: str) -> dict[str, object]:
    path = Path(tempfile.gettempdir()) / "r015-c-only-protocol-fixtures" / f"r{round_id}-{task_id}-{suffix}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "r015_binding": {
                    "round_id": round_id,
                    "task_id": task_id,
                    "trial_id": f"r{round_id}:C:{task_id}",
                    "session_id": f"session-r{round_id}-{task_id}",
                },
                "round_id": round_id,
                "task_id": task_id,
                "suffix": suffix,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return {
        "round_id": round_id,
        "task_id": task_id,
        "trial_id": f"r{round_id}:C:{task_id}",
        "session_id": f"session-r{round_id}-{task_id}",
        "complete": True,
        "path": str(path),
        "sha256": sha256_file(path),
    }


def _task_candidate(round_id: int, task_id: str, trajectory: dict[str, object], suffix: str) -> dict[str, object]:
    skill = _skill(f"{task_id} SOP", task_id, granularity="task")
    response_path = Path(tempfile.gettempdir()) / "r015-c-only-protocol-fixtures" / f"r{round_id}-{task_id}-{suffix}-candidate-response.json"
    response_path.parent.mkdir(parents=True, exist_ok=True)
    response_path.write_text(json.dumps({"task_id": task_id, "suffix": suffix}, sort_keys=True), encoding="utf-8")
    return {
        "candidate_id": f"task-sop-{task_id}-{suffix}",
        "candidate_fingerprint": sha256_text(canonical_json(skill)),
        "status": "validated",
        "source": "current_round_c_only",
        "round_id": round_id,
        "task_id": task_id,
        "trial_id": trajectory["trial_id"],
        "session_id": trajectory["session_id"],
        "trajectory_ref": deepcopy(trajectory),
        "skill": skill,
        "candidate_context": {
            "task_goal": f"Complete {task_id}",
            "whole_task_outcome": "completed",
            "hard_constraints": [],
            "environment_assumptions": [],
            "observed_results": ["validation completed"],
            "known_limitations": [],
        },
        "evidence": {"rule_evidence": []},
        "official_task_outcome": {"classification": "completed"},
        "raw": {"response": {"path": str(response_path), "sha256": sha256_file(response_path)}},
    }


def _config(tasks: list[str]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "r015_c_only_two_round_protocol",
        "protocol_id": "fixture-c-only-v1",
        "baseline_manifest": {"path": "baseline.json", "sha256": ""},
        "tasks": [
            {
                "order": index,
                "task_name": f"terminal-bench/{task}",
                "canonical_instance_id": task,
                "task_digest": f"sha256:{index:064x}",
                "baseline_outcome": {"classification": "completed", "reward": 1.0},
            }
            for index, task in enumerate(tasks, start=1)
        ],
        "protocol": {
            "condition": "C-only",
            "round_count": 2,
            "rounds_sequential": True,
            "fresh_bank_each_round": True,
            "fresh_trajectory_pool_each_round": True,
            "fresh_description_pool_each_round": True,
            "one_trial_per_task_per_round": True,
            "publish_after_each_task": True,
            "baseline_reference_only": True,
            "formal_start_requires_user_confirmation": True,
            "task_skill_pairing": {"min_distinct_completed_tasks": 2, "max_distinct_completed_tasks": 3, "early_action": "skip"},
            "event_extraction": {"max_initial_attempts": 3, "stop_on": ["skip", "duplicate"]},
        },
        "retrieval_profile": {
            "kind": "r012_execution_profile",
            "profile_id": "fixture-r012",
            "event_selection": {
                "profile_ref": "fixture-event-profile",
                "selection_rule_ref": "fixture-event-rule",
                "max_matching_skills": 1,
                "skill_token_budget": 100,
            },
            "sidecar_injection": {
                "event_skill_token_budget_scope": "complete_payload_active_event_blocks_delta",
                "arms": {
                    "A": {"enable_task": False, "enable_event": False},
                    "B": {"enable_task": True, "enable_event": True},
                    "C": {"enable_task": True, "enable_event": True},
                },
                "task_selection": {"threshold": 0.45, "max_matching_skills": 2, "selection_rule_ref": "fixture-task-rule"},
                "event_selection": {"threshold": 0.5},
            },
            "evolution": {
                "full_lifecycle_arms": ["C"],
                "explicit_selection_manifest_required": True,
                "candidate_selection_mode": "all_actually_supplied",
            },
        },
        "runtime_alignment": {
            "baseline_observed": {"model_id": "model", "provider_id": "provider", "endpoint": "endpoint", "thinking": "high", "reasoning_effort": "max", "context_tokens": 270000},
            "prepared_target": {"model_id": "model", "provider_id": "provider", "endpoint": "endpoint", "thinking": "high", "reasoning_effort": "max", "context_tokens": 270000},
        },
    }


class COnlyProtocolTest(unittest.TestCase):
    def _create(self, tasks: list[str], *, direct_events: bool = False) -> tuple[tempfile.TemporaryDirectory[str], COnlyProtocol, Path, Path, Path]:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        baseline_path = root / "baseline.json"
        baseline = {
            "schema_version": 1,
            "kind": "r015_legacy_coding_baseline_manifest",
            "source": {"skills_imported": False, "trajectories_imported": False, "baseline_only": True},
            "tasks": [{"order": i, "canonical_instance_id": task, "task_name": f"terminal-bench/{task}"} for i, task in enumerate(tasks, start=1)],
        }
        write_json(baseline_path, baseline)
        config_path = root / "config.json"
        config = _config(tasks)
        if direct_events:
            config["protocol"]["event_extraction"] = {"stop_on": []}
        config["baseline_manifest"]["path"] = str(baseline_path)
        config["baseline_manifest"]["sha256"] = sha256_file(baseline_path)
        write_json(config_path, config)
        state_path = root / "state.json"
        protocol = COnlyProtocol.initialize(config_path, baseline_path, state_path)
        return tmp, protocol, config_path, baseline_path, state_path

    @staticmethod
    def _raw(round_id: int, task_id: str) -> dict[str, object]:
        return {
            "condition": "C-only",
            "round_id": round_id,
            "task_id": task_id,
            "trial_id": f"r{round_id}:C:{task_id}",
            "session_id": f"session-r{round_id}-{task_id}",
            "official_harbor_trial": True,
            "official_trial": True,
        }

    def _complete_event_task(
        self,
        protocol: COnlyProtocol,
        task_id: str,
        round_id: int = 1,
        suffix: str | None = None,
        *,
        publication_decision: str = "add",
        merge_target_id: str | None = None,
        final_skill: dict[str, object] | None = None,
    ) -> dict[str, object]:
        suffix = suffix or ("a" if task_id == "a" else "b")
        assignment = protocol.freeze_task(task_id)
        trajectory = _trajectory(round_id, task_id, suffix)
        protocol.record_trial(task_id, outcome="completed", trajectory=trajectory, raw_evidence=self._raw(round_id, task_id))
        skill = _skill(f"{task_id} event", task_id)
        fixture_root = Path(tempfile.gettempdir()) / "r015-c-only-protocol-fixtures"
        description_value = {
            "task_family": "diagnostic repair",
            "observed_obstacle": "the fixture exposed a reproducible failure",
            "attempted_procedure": "inspect the local evidence and apply a bounded repair",
            "observed_outcome": "the fixture completed",
            "source_step_ids": ["fixture-step"],
        }
        description_response = fixture_root / f"description-r{round_id}-{task_id}-{suffix}.json"
        write_json(description_response, {"kind": "fixture-description-response", "task_id": task_id, "suffix": suffix})
        description = {
            "task_id": task_id,
            "round_id": round_id,
            "path": str(description_response),
            "sha256": sha256_file(description_response),
            "response_sha256": sha256_file(description_response),
            "value": description_value,
            "value_sha256": sha256_text(canonical_json(description_value)),
            "trajectory_ref": trajectory,
        }
        fig9_response = fixture_root / f"fig9-r{round_id}-{task_id}-{suffix}.json"
        write_json(fig9_response, {"kind": "fixture-fig9-response", "task_id": task_id, "suffix": suffix})
        fig9_hash = sha256_file(fig9_response)
        candidate_fingerprint = sha256_text(canonical_json(skill))
        extraction = protocol.extract_after_task(
            task_id,
            candidates=[{"skill": skill}],
            trajectory_ref=trajectory,
            extraction_evidence={"manager": "event-extraction"},
            description_records=[description],
        )
        candidate = extraction["candidates"][0]
        candidate_for_operation = deepcopy(final_skill) if final_skill is not None else candidate["skill"]
        candidate_for_operation_fingerprint = sha256_text(canonical_json(candidate_for_operation))
        merge_target = None
        if publication_decision == "merge":
            if not merge_target_id:
                raise AssertionError("merge fixture requires a target skill ID")
            merge_target = protocol.frozen_bank(task_id)._find_active(merge_target_id)
        publication = protocol.publish_after_task(
            task_id,
            operations=[
                {
                    "operation_id": f"fig9-{round_id}-{task_id}",
                    "source_kind": "extraction",
                    "candidate_id": candidate["candidate_id"],
                    "original_candidate": candidate["skill"],
                    "candidate": candidate_for_operation,
                    "decision": publication_decision,
                    "source_instance_ids": [task_id],
                    "merge_target_id": merge_target_id,
                    "evidence": {
                        "kind": "fig9",
                        "manager_response_sha256": fig9_hash,
                        "manager_response_path": str(fig9_response),
                        "fig9_response_sha256": fig9_hash,
                        "fig9_response_path": str(fig9_response),
                        "original_candidate_fingerprint": candidate_fingerprint,
                        "merged_candidate_fingerprint": candidate_for_operation_fingerprint,
                        "extraction_candidate_fingerprint": candidate_fingerprint,
                        "merge_target_skill_fingerprint": sha256_text(canonical_json(merge_target)) if merge_target is not None else None,
                        "merge_target_skill": deepcopy(merge_target) if merge_target is not None else None,
                    },
                }
            ],
        )
        protocol.finish_task(task_id)
        return {"assignment": assignment, "trajectory": trajectory, "extraction": extraction, "publication": publication}

    def test_driver_publication_binding_accepts_add_merge_and_drop(self) -> None:
        """Fig.9 final candidates are bound separately from extraction inputs."""
        tmp, protocol, _config_path, _baseline_path, _state_path = self._create(["a", "b", "c"])
        self.addCleanup(tmp.cleanup)
        first = self._complete_event_task(protocol, "a", suffix="add")
        protocol.freeze_task("b")
        target = protocol.frozen_bank("b").snapshot()["skills"][0]
        merged = _skill("merged b event", "b")
        merged["provenance"]["parent_skill_ids"] = []
        second = self._complete_event_task(
            protocol,
            "b",
            suffix="merge",
            publication_decision="merge",
            merge_target_id=target["skill_id"],
            final_skill=merged,
        )
        merge_operation = second["publication"]["operations"][0]
        self.assertEqual(merge_operation["decision"], "merge")
        self.assertNotEqual(merge_operation["original_candidate_fingerprint"], merge_operation["candidate_fingerprint"])
        protocol.freeze_task("c")
        live = protocol.frozen_bank("c").snapshot()["skills"]
        active = [skill for skill in live if skill["status"] == "active"]
        self.assertEqual(len(active), 1)
        self.assertEqual(set(active[0]["provenance"]["source_instance_ids"]), {"a", "b"})

        drop_protocol_tmp, drop_protocol, _config_path, _baseline_path, _state_path = self._create(["drop"])
        self.addCleanup(drop_protocol_tmp.cleanup)
        dropped = self._complete_event_task(drop_protocol, "drop", suffix="drop", publication_decision="drop")
        self.assertEqual(dropped["publication"]["operations"][0]["decision"], "drop")
        self.assertEqual(drop_protocol._round()["bank"]["skills"], [])

    def test_first_task_is_empty_and_only_c_is_published_to_next_task(self) -> None:
        tmp, protocol, _config_path, _baseline_path, _state_path = self._create(["a", "b"])
        self.addCleanup(tmp.cleanup)
        evidence = self._complete_event_task(protocol, "a")
        self.assertEqual(evidence["assignment"]["condition"], "C-only")
        self.assertEqual(evidence["assignment"]["frozen_bank"]["skills"], [])
        next_assignment = protocol.freeze_task("b")
        self.assertEqual(next_assignment["frozen_bank_state_sha256"], evidence["publication"]["after_bank_state_sha256"])
        self.assertEqual(len(protocol.eligible_skills("b", "event")), 1)
        self.assertEqual(protocol.eligible_skills("a", "event"), [])
        state_text = json.dumps(protocol.state, ensure_ascii=False)
        self.assertNotIn('"arm"', state_text)
        self.assertNotIn('"source_arm"', state_text)
        self.assertNotIn('"A"', json.dumps(protocol.state["rounds"]["1"]["assignments"], ensure_ascii=False))
        self.assertNotIn('"B"', json.dumps(protocol.state["rounds"]["1"]["assignments"], ensure_ascii=False))

    def test_task_skill_pairing_skips_early_and_accepts_two_current_round_tasks(self) -> None:
        tmp, protocol, _config_path, _baseline_path, _state_path = self._create(["a", "b", "c"])
        self.addCleanup(tmp.cleanup)
        assignment = protocol.freeze_task("a")
        trajectory_a = _trajectory(1, "a", "a")
        protocol.record_trial("a", outcome="completed", trajectory=trajectory_a, raw_evidence=self._raw(1, "a"))
        task_candidate_a = _task_candidate(1, "a", trajectory_a, "a")
        with self.assertRaisesRegex(COnlyProtocolError, "skip until two"):
            protocol.extract_after_task(
                "a",
                candidates=[{"skill": _skill("a task", "a", granularity="task"), "pairing": {"source_task_ids": ["a", "a"], "trajectory_refs": [trajectory_a, trajectory_a]}}],
                trajectory_ref=trajectory_a,
                extraction_evidence={"manager": "task-extraction"},
                task_candidate_records=[task_candidate_a],
            )
        protocol.extract_after_task("a", candidates=[], trajectory_ref=trajectory_a, extraction_evidence={"manager": "task-extraction"}, task_candidate_records=[task_candidate_a], decision="skip", reason="fewer than two current-round tasks")
        protocol.publish_after_task("a", operations=[])
        protocol.finish_task("a")
        self.assertEqual(len(protocol._round()["task_candidate_pool"]), 1)
        self.assertEqual(protocol.eligible_skills("a", "task"), [])
        protocol.freeze_task("b")
        trajectory_b = _trajectory(1, "b", "b")
        protocol.record_trial("b", outcome="completed", trajectory=trajectory_b, raw_evidence=self._raw(1, "b"))
        task_candidate_b = _task_candidate(1, "b", trajectory_b, "b")
        merged_skill = _skill("paired task", "b", granularity="task")
        merged_skill["provenance"]["source_instance_ids"] = ["a", "b"]
        merged_skill["provenance"]["source_instance_ids_raw"] = ["terminal-bench/a", "terminal-bench/b"]
        extraction = protocol.extract_after_task(
            "b",
            candidates=[{"skill": merged_skill, "pairing": {"source_task_ids": ["a", "b"], "source_candidate_ids": [task_candidate_a["candidate_id"], task_candidate_b["candidate_id"]], "source_candidate_fingerprints": [task_candidate_a["candidate_fingerprint"], task_candidate_b["candidate_fingerprint"]], "trajectory_refs": [trajectory_a, trajectory_b]}}],
            trajectory_ref=trajectory_b,
            extraction_evidence={"manager": "task-extraction"},
            task_candidate_records=[task_candidate_b],
        )
        self.assertEqual(extraction["candidates"][0]["pairing"]["source_task_ids"], ["a", "b"])
        self.assertEqual(extraction["candidates"][0]["pairing"]["source_candidate_ids"], [task_candidate_a["candidate_id"], task_candidate_b["candidate_id"]])

    def test_event_attempts_stop_after_skip_and_never_exceed_three(self) -> None:
        tmp, protocol, _config_path, _baseline_path, _state_path = self._create(["a"])
        self.addCleanup(tmp.cleanup)
        protocol.freeze_task("a")
        trajectory = _trajectory(1, "a", "a")
        protocol.record_trial("a", outcome="completed", trajectory=trajectory, raw_evidence=self._raw(1, "a"))
        protocol.record_event_attempt("a", attempt_no=1, outcome="generated", candidate=_skill("a event", "a"), raw_response={"action": "generated"})
        protocol.record_event_attempt("a", attempt_no=2, outcome="skip", candidate=None, raw_response={"action": "skip"})
        with self.assertRaisesRegex(COnlyProtocolError, "stopped"):
            protocol.record_event_attempt("a", attempt_no=3, outcome="generated", candidate=_skill("late", "a"), raw_response={"action": "generated"})
        with self.assertRaisesRegex(COnlyProtocolError, "1 through 3|stopped"):
            protocol.record_event_attempt("a", attempt_no=4, outcome="generated", candidate=_skill("too late", "a"), raw_response={"action": "generated"})
        protocol.extract_after_task("a", candidates=[], trajectory_ref=trajectory, extraction_evidence={"manager": "event-extraction"}, decision="skip", reason="event extraction stopped")

    def test_per_event_config_records_eight_and_keeps_later_events_after_skip(self) -> None:
        tmp, protocol, _config_path, _baseline_path, _state_path = self._create(["a"], direct_events=True)
        self.addCleanup(tmp.cleanup)
        protocol.freeze_task("a")
        trajectory = _trajectory(1, "a", "event-eight")
        protocol.record_trial("a", outcome="completed", trajectory=trajectory, raw_evidence=self._raw(1, "a"))
        candidates = []
        for ordinal in range(1, 9):
            outcome = "skip" if ordinal == 2 else "invalid" if ordinal == 3 else "generated"
            candidate = _skill(f"event {ordinal}", "a") if outcome == "generated" else None
            recorded = protocol.record_event_attempt("a", attempt_no=ordinal, outcome=outcome,
                                                      candidate=candidate, raw_response={"ordinal": ordinal})
            self.assertFalse(recorded["stopped"])
            if candidate is not None:
                candidates.append({"candidate_id": f"event-{ordinal}", "skill": candidate})
        protocol.record_event_attempt("a", attempt_no=9, outcome="invalid",
                                      candidate=None, raw_response={"ordinal": 9})
        extraction = protocol.extract_after_task("a", candidates=candidates,
            trajectory_ref=trajectory, extraction_evidence={"event": "per-event"})
        self.assertEqual(len(extraction["event_attempts"]), 9)
        self.assertEqual(len(extraction["candidates"]), 6)
        operations = []
        for ordinal, candidate in enumerate(extraction["candidates"], start=1):
            response = Path(tmp.name) / f"fig9-{ordinal}.json"
            write_json(response, {"action": "add", "ordinal": ordinal})
            response_hash = sha256_file(response)
            fingerprint = sha256_text(canonical_json(candidate["skill"]))
            operations.append({"operation_id": f"event-op-{ordinal}",
                "source_kind": "extraction", "candidate_id": candidate["candidate_id"],
                "original_candidate": candidate["skill"], "candidate": candidate["skill"],
                "decision": "add", "source_instance_ids": ["a"],
                "evidence": {"manager_response_sha256": response_hash,
                    "manager_response_path": str(response),
                    "fig9_response_sha256": response_hash,
                    "fig9_response_path": str(response),
                    "original_candidate_fingerprint": fingerprint,
                    "merged_candidate_fingerprint": fingerprint,
                    "extraction_candidate_fingerprint": fingerprint}})
        publication = protocol.publish_after_task("a", operations=operations)
        self.assertEqual(len(publication["operations"]), 6)
        self.assertEqual(protocol._round()["bank"]["sequence"], 6)

    def test_direct_event_attempts_follow_all_segments(self) -> None:
        tmp, protocol, _config_path, _baseline_path, _state_path = self._create(["a"], direct_events=True)
        self.addCleanup(tmp.cleanup)
        protocol.freeze_task("a")
        trajectory = _trajectory(1, "a", "event-four")
        protocol.record_trial("a", outcome="completed", trajectory=trajectory, raw_evidence=self._raw(1, "a"))
        for ordinal in range(1, 5):
            protocol.record_event_attempt("a", attempt_no=ordinal, outcome="invalid",
                                          candidate=None, raw_response={"ordinal": ordinal})
        protocol.record_event_attempt("a", attempt_no=5, outcome="invalid",
                                      candidate=None, raw_response={"ordinal": 5})

    def test_direct_segment_events_are_recorded_beyond_old_cap(self) -> None:
        tmp, protocol, _config_path, _baseline_path, _state_path = self._create(["a"], direct_events=True)
        self.addCleanup(tmp.cleanup)
        protocol.freeze_task("a")
        trajectory = _trajectory(1, "a", "event-segments")
        protocol.record_trial("a", outcome="completed", trajectory=trajectory, raw_evidence=self._raw(1, "a"))
        for ordinal in range(1, 7):
            protocol.record_event_attempt("a", attempt_no=ordinal, outcome="invalid",
                candidate=None, raw_response={"ordinal": ordinal})
        self.assertEqual(len(protocol._round()["event_attempts"]["a"]), 6)

    def test_per_event_duplicate_does_not_suppress_later_distinct_event(self) -> None:
        tmp, protocol, _config_path, _baseline_path, _state_path = self._create(["a"], direct_events=True)
        self.addCleanup(tmp.cleanup)
        protocol.freeze_task("a")
        trajectory = _trajectory(1, "a", "event-duplicate")
        protocol.record_trial("a", outcome="completed", trajectory=trajectory, raw_evidence=self._raw(1, "a"))
        first = _skill("first event", "a")
        later = _skill("later event", "a")
        for ordinal, outcome, candidate in (
            (1, "generated", first), (2, "duplicate", first), (3, "generated", later)):
            record = protocol.record_event_attempt("a", attempt_no=ordinal,
                outcome=outcome, candidate=candidate, raw_response={"ordinal": ordinal})
            self.assertFalse(record["stopped"])
        extraction = protocol.extract_after_task("a", candidates=[
            {"candidate_id": "first", "skill": first},
            {"candidate_id": "later", "skill": later}],
            trajectory_ref=trajectory, extraction_evidence={"event": "per-event"})
        self.assertEqual(len(extraction["event_attempts"]), 3)
        self.assertEqual(len(extraction["candidates"]), 2)

    def test_resume_is_idempotent_and_round_two_restarts_all_material_empty(self) -> None:
        tmp, protocol, config_path, baseline_path, state_path = self._create(["a", "b"])
        self.addCleanup(tmp.cleanup)
        first = self._complete_event_task(protocol, "a")
        # Replaying each durable boundary returns the exact prior result and
        # does not append a second bank operation or candidate.
        assignment = protocol.freeze_task("b")
        trajectory = _trajectory(1, "b", "b")
        trial = protocol.record_trial("b", outcome="completed", trajectory=trajectory, raw_evidence=self._raw(1, "b"))
        extraction = protocol.extract_after_task("b", candidates=[], trajectory_ref=trajectory, extraction_evidence={"manager": "none"}, decision="skip", reason="no candidate")
        publication = protocol.publish_after_task("b", operations=[])
        completion = protocol.finish_task("b")
        for value in (assignment, trial, extraction, publication, completion):
            protocol.save(state_path)
        restored = COnlyProtocol.load(state_path, config_path, baseline_path)
        self.assertEqual(restored.record_trial("b", outcome="completed", trajectory=trajectory, raw_evidence=self._raw(1, "b")), trial)
        self.assertEqual(restored.extract_after_task("b", candidates=[], trajectory_ref=trajectory, extraction_evidence={"manager": "none"}, decision="skip", reason="no candidate"), extraction)
        self.assertEqual(restored.publish_after_task("b", operations=[]), publication)
        self.assertEqual(restored.finish_task("b"), completion)
        restored.start_next_round()
        restored.save(state_path)
        restored_from_disk = COnlyProtocol.load(state_path, config_path, baseline_path)
        self.assertEqual(restored_from_disk.current_round_id, 2)
        self.assertEqual(restored_from_disk.current_task_id, "a")
        self.assertEqual(restored_from_disk.state["rounds"]["2"]["status"], "active")
        self.assertEqual(restored.current_round_id, 2)
        self.assertEqual(restored.current_task_id, "a")
        round_two = restored.state["rounds"]["2"]
        self.assertEqual(round_two["bank"]["skills"], [])
        self.assertEqual(round_two["bank"]["operations"], [])
        self.assertEqual(round_two["trajectory_pool"], [])
        self.assertEqual(round_two["description_pool"], [])
        self.assertGreater(len(restored.state["rounds"]["1"]["bank"]["skills"]), 0)
        self.assertFalse(restored.state["baseline_manifest"].get("skills_imported", False))

    def test_maintenance_requires_an_actually_supplied_skill_and_preserves_source(self) -> None:
        tmp, protocol, _config_path, _baseline_path, _state_path = self._create(["a", "b"])
        self.addCleanup(tmp.cleanup)
        first = self._complete_event_task(protocol, "a")
        protocol.freeze_task("b")
        trajectory = _trajectory(1, "b", "b")
        frozen_skill = protocol.frozen_bank("b").snapshot()["skills"][0]
        supplied = [{"skill_id": frozen_skill["skill_id"], "version": frozen_skill["version"], "source": "current_round", "round_id": 1}]
        protocol.record_trial("b", outcome="completed", trajectory=trajectory, supplied_skills=supplied, raw_evidence=self._raw(1, "b"))
        protocol.extract_after_task("b", candidates=[], trajectory_ref=trajectory, extraction_evidence={"manager": "none"}, decision="skip", reason="no new extraction")
        maintained = _skill("maintained", "b")
        maintained["provenance"]["parent_skill_ids"] = [supplied[0]["skill_id"]]
        response_dir = Path(tmp.name) / "manager-responses"
        response_dir.mkdir(parents=True, exist_ok=True)
        fig8_response = response_dir / "fig8.json"
        fig9_response = response_dir / "fig9.json"
        write_json(fig8_response, {"kind": "fixture-fig8"})
        write_json(fig9_response, {"kind": "fixture-fig9"})
        fig8_hash = sha256_file(fig8_response)
        fig9_hash = sha256_file(fig9_response)
        maintained_fp = sha256_text(canonical_json(maintained))
        with self.assertRaisesRegex(COnlyProtocolError, "every skill actually supplied"):
            protocol.publish_after_task("b", operations=[{"operation_id": "bad", "source_kind": "maintenance", "supplied_skill_id": "missing", "supplied_skill_version": 1, "candidate": maintained, "decision": "add", "source_instance_ids": ["b"], "evidence": {"kind": "fig9", "manager_response_sha256": "b" * 64}}], manager_decisions=[])
        failure = protocol._round()["publication_failures"][-1]
        self.assertEqual(failure["manager_decisions"], [])
        self.assertEqual(failure["operations"][0]["supplied_skill_id"], "missing")
        result = protocol.publish_after_task(
            "b",
            manager_decisions=[{"skill_id": supplied[0]["skill_id"], "version": 1, "action": "evolve", "reason": "actual supplied evidence", "manager_response_sha256": fig8_hash, "manager_response_path": str(fig8_response)}],
            operations=[{"operation_id": "maintenance-ok", "source_kind": "maintenance", "supplied_skill_id": supplied[0]["skill_id"], "supplied_skill_version": 1, "original_candidate": maintained, "candidate": maintained, "decision": "add", "source_instance_ids": ["b"], "evidence": {"kind": "fig9", "manager_response_sha256": fig9_hash, "manager_response_path": str(fig9_response), "fig8_manager_response_sha256": fig8_hash, "fig8_manager_response_path": str(fig8_response), "fig9_response_sha256": fig9_hash, "fig9_response_path": str(fig9_response), "original_candidate_fingerprint": maintained_fp, "merged_candidate_fingerprint": maintained_fp, "fig8_candidate_fingerprint": maintained_fp, "fig8_candidate_provenance": maintained["provenance"], "ancestor_union": maintained["provenance"]}}],
        )
        self.assertEqual(result["operations"][0]["source_kind"], "maintenance")
        live_skills = protocol._round()["bank"]["skills"]
        self.assertTrue(any("b" in skill["provenance"]["source_instance_ids"] for skill in live_skills))

    def test_duplicate_event_must_reference_a_prior_candidate(self) -> None:
        tmp, protocol, _config_path, _baseline_path, _state_path = self._create(["a"])
        self.addCleanup(tmp.cleanup)
        protocol.freeze_task("a")
        trajectory = _trajectory(1, "a", "a")
        protocol.record_trial("a", outcome="completed", trajectory=trajectory, raw_evidence=self._raw(1, "a"))
        first = _skill("first event", "a")
        protocol.record_event_attempt("a", attempt_no=1, outcome="generated", candidate=first, raw_response={"action": "generated"})
        with self.assertRaisesRegex(COnlyProtocolError, "prior candidate"):
            protocol.record_event_attempt("a", attempt_no=2, outcome="duplicate", candidate=_skill("different event", "a"), raw_response={"action": "duplicate"})
        duplicate = protocol.record_event_attempt("a", attempt_no=2, outcome="duplicate", candidate=first, raw_response={"action": "duplicate"})
        self.assertTrue(duplicate["stopped"])
        with self.assertRaisesRegex(COnlyProtocolError, "stopped"):
            protocol.record_event_attempt("a", attempt_no=3, outcome="generated", candidate=_skill("late event", "a"), raw_response={"action": "generated"})

    def test_pairing_trajectory_refs_cannot_cross_rounds(self) -> None:
        tmp, protocol, _config_path, _baseline_path, _state_path = self._create(["a", "b"])
        self.addCleanup(tmp.cleanup)
        self._complete_event_task(protocol, "a")
        protocol.freeze_task("b")
        trajectory = _trajectory(1, "b", "b")
        protocol.record_trial("b", outcome="completed", trajectory=trajectory, raw_evidence=self._raw(1, "b"))
        with self.assertRaisesRegex(COnlyProtocolError, "bound to C-only round|stay within"):
            protocol.extract_after_task(
                "b",
                candidates=[
                    {
                        "skill": _skill("paired task", "b", granularity="task"),
                        "pairing": {
                            "source_task_ids": ["a", "b"],
                        "trajectory_refs": [_trajectory(2, "a", "wrong-round"), trajectory],
                        },
                    }
                ],
                trajectory_ref=trajectory,
                extraction_evidence={"manager": "task-extraction"},
            )

    def test_trajectory_file_binding_is_verified_in_bytes(self) -> None:
        tmp, protocol, _config_path, _baseline_path, _state_path = self._create(["a"])
        self.addCleanup(tmp.cleanup)
        protocol.freeze_task("a")
        trajectory = _trajectory(1, "a", "binding")
        path = Path(str(trajectory["path"]))
        value = json.loads(path.read_text(encoding="utf-8"))
        value["r015_binding"]["session_id"] = "different-session"
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        trajectory["sha256"] = sha256_file(path)
        with self.assertRaisesRegex(COnlyProtocolError, "r015_binding.session_id"):
            protocol.record_trial("a", outcome="completed", trajectory=trajectory, raw_evidence=self._raw(1, "a"))

    def test_registered_trajectory_pool_entry_must_match_completed_assignment(self) -> None:
        """A valid file cannot be relabelled as another session in the pool."""
        tmp, protocol, config_path, baseline_path, state_path = self._create(["a", "b"])
        self.addCleanup(tmp.cleanup)
        self._complete_event_task(protocol, "a")
        protocol.freeze_task("b")
        tampered = deepcopy(protocol.state)
        tampered["rounds"]["1"]["trajectory_pool"][0]["session_id"] = "different-session"
        write_json(state_path, tampered)
        with self.assertRaisesRegex(COnlyProtocolError, "completed assignment trajectory"):
            COnlyProtocol.load(state_path, config_path, baseline_path)

    def test_task_candidate_pool_is_isolated_and_bound_to_exact_extraction_record(self) -> None:
        tmp, protocol, config_path, baseline_path, state_path = self._create(["a", "b"])
        self.addCleanup(tmp.cleanup)
        protocol.freeze_task("a")
        trajectory = _trajectory(1, "a", "candidate-pool")
        protocol.record_trial("a", outcome="completed", trajectory=trajectory, raw_evidence=self._raw(1, "a"))
        task_candidate = _task_candidate(1, "a", trajectory, "candidate-pool")
        extraction = protocol.extract_after_task(
            "a",
            candidates=[],
            trajectory_ref=trajectory,
            extraction_evidence={"kind": "isolated-task-candidate"},
            task_candidate_records=[task_candidate],
            decision="skip",
            reason="only one task source is available",
        )
        protocol.publish_after_task("a", operations=[])
        protocol.finish_task("a")
        self.assertEqual(extraction["task_candidate_records"][0]["candidate_id"], task_candidate["candidate_id"])
        self.assertEqual(protocol._round()["bank"]["skills"], [])
        protocol.freeze_task("b")
        self.assertEqual(protocol.eligible_skills("b", "task"), [])
        protocol.save(state_path)
        tampered = read_json(state_path)
        tampered["rounds"]["1"]["task_candidate_pool"][0]["candidate_context"]["task_goal"] = "Relabelled goal"
        write_json(state_path, tampered)
        with self.assertRaisesRegex(COnlyProtocolError, "exact SOP candidate"):
            COnlyProtocol.load(state_path, config_path, baseline_path)

    def test_nested_raw_evidence_file_hash_is_checked_on_reload(self) -> None:
        """A durable nested response ref cannot be changed between phases."""
        tmp, protocol, config_path, baseline_path, state_path = self._create(["a"])
        self.addCleanup(tmp.cleanup)
        protocol.freeze_task("a")
        trajectory = _trajectory(1, "a", "nested-ref")
        raw_path = Path(tmp.name) / "raw-session.json"
        write_json(raw_path, {"kind": "raw-session", "trial_id": "r1:C:a"})
        raw = self._raw(1, "a")
        raw["raw_artifacts"] = {
            "session": {"path": str(raw_path), "sha256": sha256_file(raw_path)},
        }
        protocol.record_trial("a", outcome="completed", trajectory=trajectory, raw_evidence=raw)
        protocol.extract_after_task(
            "a",
            candidates=[],
            trajectory_ref=trajectory,
            extraction_evidence={"kind": "nested-ref-fixture"},
            decision="skip",
            reason="no candidate",
        )
        protocol.publish_after_task("a", operations=[])
        protocol.finish_task("a")
        protocol.save(state_path)
        write_json(raw_path, {"kind": "raw-session", "trial_id": "r1:C:a", "changed": True})
        with self.assertRaisesRegex(COnlyProtocolError, "sha256 does not match the evidence file"):
            COnlyProtocol.load(state_path, config_path, baseline_path)


if __name__ == "__main__":
    unittest.main()
