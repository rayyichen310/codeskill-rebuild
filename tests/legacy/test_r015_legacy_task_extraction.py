"""Tests for the historical, manager-only Task extraction implementation."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codeskill_rebuild.types import canonical_json, sha256_file, sha256_text, write_json
from scripts.run_r015_c_only import _validate_driver_payload_refs
from scripts.run_r015_c_only_harbor_driver import COnlyHarborDriverError
from scripts.legacy.r015_task_extraction import (
    _description_extraction, _single_task_candidate_extraction, _task_extraction,
)


class LegacyTaskExtractionTest(unittest.TestCase):


    def test_task_extraction_skips_without_a_previous_source(self) -> None:
        """The current anchor alone cannot satisfy the 2--3 trajectory group."""
        context = {"task_id": "c", "trial_id": "r1:C:c"}
        traces = [
            ("c", {"text_manager_eligible": True}, {"task_id": "c"}),
        ]
        candidates, evidence = _task_extraction(
            context=context,
            input_value={},
            traces=traces,
            task_candidate_records=[],
            executor=object(),
        )
        self.assertEqual(candidates, [])
        self.assertEqual(evidence["status"], "no_current_task_candidate")

    def test_description_validation_failure_keeps_response_and_final_journal_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            response_path = root / "response.json"
            journal_path = root / "description.json"
            write_json(response_path, {"classification": "complete_fixture"})
            write_json(journal_path, {"kind": "r012_pre_call_journal", "status": "prepared_before_manager_call"})

            class Executor:
                @staticmethod
                def _finish_journal(path: Path, *, status: str, value: dict[str, object]) -> None:
                    journal = json.loads(path.read_text(encoding="utf-8"))
                    journal.update({"status": status, **value})
                    write_json(path, journal)

            call = {"call_id": "call-0001", "json": {"action": "no_related_group", "reason": "not a description"}}
            journal = {
                "path": str(journal_path),
                "sha256": sha256_file(journal_path),
                "response": {"call_id": "call-0001", "path": str(response_path), "sha256": sha256_file(response_path)},
            }
            manager_call = patch(
                "scripts.legacy.r015_task_extraction._manager_call",
                return_value=(call, journal, None),
            )
            with manager_call as called:
                records, evidence = _description_extraction(
                    context={"task_id": "task", "trial_id": "r1:C:task"},
                    trace={
                        "text_manager_eligible": True,
                        "instruction": "Repair the service.",
                        "source": {"task_name": "terminal-bench/task", "instance_id": "task"},
                        "outcome": {"reward": 0},
                        "steps": [{"source_entry_id": "12345678-step", "role": "user"}],
                    },
                    current_ref={"path": "trace.json", "sha256": "trace"},
                    executor=Executor(),
                )
            self.assertEqual(records, [])
            self.assertEqual(evidence["status"], "description_extraction_failed")
            self.assertEqual(evidence["response"]["call_id"], "call-0001")
            self.assertEqual(evidence["journal"]["sha256"], sha256_file(journal_path))
            self.assertEqual(json.loads(journal_path.read_text(encoding="utf-8"))["status"], "description_output_rejected")
            called.assert_called_once()

    def test_prefix_expanded_description_is_saved_canonically(self) -> None:
        context = {"task_id": "task", "trial_id": "r1:C:task"}
        trace = {
            "text_manager_eligible": True,
            "instruction": "Repair the service.",
            "source": {"task_name": "terminal-bench/task", "instance_id": "task"},
            "outcome": {"reward": 1},
            "steps": [{"source_entry_id": "12345678-step", "role": "user"}],
        }
        current_ref = {
            "round_id": 1,
            "task_id": "task",
            "trial_id": "r1:C:task",
            "session_id": "session-task",
            "complete": True,
            "path": "trace.json",
            "sha256": "a" * 64,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            response_path = root / "response.json"
            journal_path = root / "description.json"
            write_json(response_path, {"kind": "description-response"})
            write_json(journal_path, {"kind": "r012_pre_call_journal", "status": "prepared_before_manager_call"})

            class Executor:
                @staticmethod
                def _finish_journal(path: Path, *, status: str, value: dict[str, object]) -> None:
                    journal_value = json.loads(path.read_text(encoding="utf-8"))
                    journal_value.update({"status": status, **value})
                    write_json(path, journal_value)

            call = {
                "call_id": "call-0001",
                "json": {
                    "task_family": "repair a local service",
                    "observed_obstacle": "The service failed its check.",
                    "attempted_procedure": "The agent inspected and repaired it.",
                    "observed_outcome": "The check passed.",
                    "source_step_ids": ["12345678"],
                },
            }
            journal = {
                "path": str(journal_path),
                "sha256": sha256_file(journal_path),
                "response": {
                    "call_id": "call-0001",
                    "path": str(response_path),
                    "sha256": sha256_file(response_path),
                },
            }
            with patch(
                "scripts.legacy.r015_task_extraction._manager_call",
                return_value=(call, journal, None),
            ):
                descriptions, extraction_evidence = _description_extraction(
                    context=context,
                    trace=trace,
                    current_ref=current_ref,
                    executor=Executor(),
                )
            self.assertEqual(extraction_evidence["status"], "validated")
            self.assertEqual(
                descriptions[0]["value"]["source_step_id_expansions"],
                {"12345678": "12345678-step"},
            )

    def test_task_candidate_schema_failures_finalize_the_manager_journal(self) -> None:
        trace = {
            "text_manager_eligible": True,
            "instruction": "Repair the service.",
            "source": {"task_name": "terminal-bench/task", "instance_id": "task"},
            "outcome": {"reward": 1},
            "steps": [
                {"source_entry_id": "action-task", "role": "assistant"},
                {"source_entry_id": "result-task", "role": "toolResult"},
            ],
        }
        base_output = {
            "action": "generate",
            "skill": {
                "title": "Repair service SOP",
                "granularity": "general",
                "when_to_apply": "When a local service fails validation.",
                "rules": ["Inspect the service, repair it, and rerun validation."],
            },
            "candidate_context": {
                "task_goal": "Repair the service",
                "whole_task_outcome": "completed",
                "hard_constraints": [],
                "environment_assumptions": [],
                "observed_results": ["validation passed"],
                "known_limitations": [],
            },
            "evidence": {
                "rule_evidence": [
                    {
                        "rule_index": 0,
                        "sources": [
                            {
                                "canonical_instance_id": "task",
                                "step_ids": ["action-task", "result-task"],
                            }
                        ],
                    }
                ]
            },
        }
        cases = {
            "blank_title": {"title": ""},
            "blank_when_to_apply": {"when_to_apply": ""},
            "empty_rules": {"rules": []},
        }
        for name, skill_change in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                response_path = root / "response.json"
                journal_path = root / "task-sop-candidate.json"
                write_json(response_path, {"kind": "live_manager_call"})
                write_json(journal_path, {"kind": "r012_pre_call_journal", "status": "prepared_before_manager_call"})

                class Executor:
                    @staticmethod
                    def _finish_journal(path: Path, *, status: str, value: dict[str, object]) -> None:
                        journal_value = json.loads(path.read_text(encoding="utf-8"))
                        journal_value.update({"status": status, **value})
                        write_json(path, journal_value)

                output = deepcopy(base_output)
                output["skill"].update(skill_change)
                if name == "empty_rules":
                    output["evidence"]["rule_evidence"] = []
                call = {"call_id": "call-0001", "json": output}
                journal = {
                    "path": str(journal_path),
                    "sha256": sha256_file(journal_path),
                    "response": {
                        "call_id": "call-0001",
                        "path": str(response_path),
                        "sha256": sha256_file(response_path),
                    },
                }
                with patch(
                    "scripts.legacy.r015_task_extraction._manager_call",
                    return_value=(call, journal, None),
                ) as manager_call:
                    records, evidence = _single_task_candidate_extraction(
                        context={"task_id": "task", "trial_id": "r1:C:task"},
                        trace=trace,
                        current_ref={
                            "round_id": 1,
                            "task_id": "task",
                            "trial_id": "r1:C:task",
                            "session_id": "session-task",
                        },
                        executor=Executor(),
                    )
                self.assertEqual(records, [])
                self.assertEqual(evidence["status"], "task_candidate_extraction_failed")
                self.assertEqual(evidence["response"]["call_id"], "call-0001")
                self.assertEqual(evidence["journal"]["sha256"], sha256_file(journal_path))
                self.assertEqual(
                    json.loads(journal_path.read_text(encoding="utf-8"))["status"],
                    "task_candidate_output_rejected",
                )
                manager_call.assert_called_once()

    def test_task_extraction_pairs_current_sop_with_earlier_candidates(self) -> None:
        """D02 ranks isolated SOPs, then Fig.6 preserves candidate identities."""
        context = {"task_id": "c", "trial_id": "r1:C:c"}

        def trace(task_id: str) -> dict[str, object]:
            return {
                "text_manager_eligible": True,
                "instruction": f"Repair {task_id}.",
                "source": {
                    "instance_id": task_id,
                    "task_name": f"terminal-bench/{task_id}",
                },
                "steps": [
                    {"source_entry_id": f"action-{task_id}", "role": "assistant"},
                    {"source_entry_id": f"result-{task_id}", "role": "toolResult"},
                ],
                "outcome": {"reward": 1},
            }

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        current_ref = {"round_id": 1, "task_id": "c", "trial_id": "r1:C:c", "session_id": "session-c", "complete": True, "path": "c.json", "sha256": "c" * 64}
        prior_ref = {"round_id": 1, "task_id": "a", "trial_id": "r1:C:a", "session_id": "session-a", "complete": True, "path": "a.json", "sha256": "a" * 64}
        a_response = root / "a-description.json"
        c_response = root / "c-description.json"
        write_json(a_response, {"kind": "description-a"})
        write_json(c_response, {"kind": "description-c"})
        prior_ref["path"] = str(root / "a-trace.json")
        current_ref["path"] = str(root / "c-trace.json")
        write_json(Path(prior_ref["path"]), {"r015_binding": {"round_id": 1, "task_id": "a", "trial_id": "r1:C:a", "session_id": "session-a"}})
        write_json(Path(current_ref["path"]), {"r015_binding": {"round_id": 1, "task_id": "c", "trial_id": "r1:C:c", "session_id": "session-c"}})
        prior_ref["sha256"] = sha256_file(Path(prior_ref["path"]))
        current_ref["sha256"] = sha256_file(Path(current_ref["path"]))
        later_ref = {"round_id": 1, "task_id": "b", "trial_id": "r1:C:b", "session_id": "session-b", "complete": True, "path": str(root / "b-trace.json")}
        write_json(Path(later_ref["path"]), {"r015_binding": {"round_id": 1, "task_id": "b", "trial_id": "r1:C:b", "session_id": "session-b"}})
        later_ref["sha256"] = sha256_file(Path(later_ref["path"]))
        later_response = root / "b-description.json"
        write_json(later_response, {"kind": "description-b"})
        image_ref = {
            "round_id": 1,
            "task_id": "code-from-image",
            "trial_id": "r1:C:code-from-image",
            "session_id": "session-image",
            "complete": True,
            "path": str(root / "image-trace.json"),
        }
        write_json(
            Path(image_ref["path"]),
            {
                "r015_binding": {
                    "round_id": 1,
                    "task_id": "code-from-image",
                    "trial_id": "r1:C:code-from-image",
                    "session_id": "session-image",
                },
                "source": {"canonical_instance_id": "code-from-image"},
            },
        )
        image_ref["sha256"] = sha256_file(Path(image_ref["path"]))
        refs_by_task = {"a": prior_ref, "b": later_ref, "c": current_ref}
        def description(task_id: str, response: Path) -> dict[str, object]:
            value = {"task_family": "repair", "observed_obstacle": f"obstacle-{task_id}", "attempted_procedure": "inspect", "observed_outcome": "completed", "source_step_ids": [f"result-{task_id}"]}
            from codeskill_rebuild.types import canonical_json, sha256_text
            return {"task_id": task_id, "round_id": 1, "path": str(response), "sha256": sha256_file(response), "response_sha256": sha256_file(response), "value": value, "value_sha256": sha256_text(canonical_json(value)), "trajectory_ref": refs_by_task[task_id]}
        def task_candidate(task_id: str, response: Path) -> dict[str, object]:
            paper_skill = {
                "title": f"{task_id} repair SOP",
                "granularity": "general",
                "when_to_apply": "When a bounded terminal repair needs validation.",
                "rules": ["Inspect the failure, apply the repair, and observe validation."],
            }
            model_output = {
                "action": "generate",
                "skill": paper_skill,
                "candidate_context": {
                    "task_goal": f"Repair {task_id}",
                    "whole_task_outcome": "completed",
                    "hard_constraints": [],
                    "environment_assumptions": [],
                    "observed_results": ["validation completed"],
                    "known_limitations": [],
                },
                "evidence": {"rule_evidence": [{"rule_index": 0, "sources": [{"canonical_instance_id": task_id, "step_ids": [f"action-{task_id}", f"result-{task_id}"]}]}]},
            }
            skill = {
                **paper_skill,
                "granularity": "task",
                "benchmark": "terminal-bench",
                "provenance": {"source_instance_ids": [task_id], "source_instance_ids_raw": [task_id], "parent_skill_ids": []},
            }
            call_id = f"candidate-{task_id}"
            api_response = {
                "choices": [
                    {
                        "message": {"content": json.dumps(model_output)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10},
            }
            write_json(
                response,
                {
                    "kind": "live_manager_call",
                    "purpose": f"r015_c_only_task_sop_candidate:r1:C:{task_id}",
                    "http_status": 200,
                    "raw_response": json.dumps(api_response),
                    "parsed_response": api_response,
                    "usage": api_response["usage"],
                    "finish_reason": "stop",
                },
            )
            return {
                "candidate_id": f"task-sop-{task_id}",
                "candidate_fingerprint": sha256_text(canonical_json(skill)),
                "status": "validated",
                "source": "current_round_c_only",
                "round_id": 1,
                "task_id": task_id,
                "trial_id": refs_by_task[task_id]["trial_id"],
                "session_id": refs_by_task[task_id]["session_id"],
                "trajectory_ref": refs_by_task[task_id],
                "skill": skill,
                "candidate_context": model_output["candidate_context"],
                "evidence": model_output["evidence"],
                "official_task_outcome": {"reward": 1},
                "raw": {"manager_call_id": call_id, "response": {"call_id": call_id, "path": str(response), "sha256": sha256_file(response)}, "model_output": model_output, "visible_step_ids": [f"action-{task_id}", f"result-{task_id}"]},
            }
        image_trace = trace("code-from-image")
        image_trace["text_manager_eligible"] = False
        image_trace["input_modalities"] = ["text", "image"]
        traces = [("a", trace("a"), prior_ref), ("code-from-image", image_trace, image_ref), ("b", trace("b"), later_ref), ("c", trace("c"), current_ref)]
        prior_a_candidate = task_candidate("a", a_response)
        prior_b_candidate = task_candidate("b", later_response)
        current_task_candidate = task_candidate("c", c_response)
        state_path = root / "state.json"
        input_value = {"state": {"path": str(state_path)}, "round_material": {"task_candidate_pool": [prior_a_candidate, prior_b_candidate]}}
        write_json(state_path, {"rounds": {"1": {"assignments": {}, "completed_tasks": {}, "task_candidate_pool": []}}})
        no_source_candidates, no_source_evidence = _task_extraction(
            context=context,
            input_value={"state": input_value["state"], "round_material": {"task_candidate_pool": []}},
            traces=[("c", trace("c"), current_ref)],
            task_candidate_records=[current_task_candidate],
            executor=object(),
        )
        self.assertEqual(no_source_candidates, [])
        self.assertEqual(no_source_evidence["status"], "no_eligible_sources")
        write_json(
            state_path,
            {
                "rounds": {
                    "1": {
                        "assignments": {},
                        "completed_tasks": {
                            "a": {"outcome": "completed", "trajectory": prior_ref},
                            "b": {"outcome": "completed", "trajectory": later_ref},
                        },
                        "task_candidate_pool": [prior_a_candidate, prior_b_candidate],
                    }
                }
            },
        )
        class FakeEncoder:
            repo_id = "fixture-minilm"
            resolved_revision = "fixture-revision"
            _model = None
            def index_skill(self, value: dict[str, object]) -> tuple[list[float], dict[str, object]]:
                return ([1.0, 0.0] if value["title"] == "c repair SOP" else [0.8, 0.2]), {"title": value["title"]}

        class Executor:
            encoder = FakeEncoder()
        call = {
            "call_id": "call-0001",
            "json": {
                "action": "generate",
                "skill": {
                    "title": "Shared repair workflow",
                    "granularity": "general",
                    "when_to_apply": "When a multi-step terminal repair has a reproducible validation loop.",
                    "rules": ["Inspect the failure, apply the smallest repair, and rerun the relevant validation."],
                },
                "evidence": {
                    "rule_evidence": [
                        {
                            "rule_index": 0,
                            "sources": [
                                {"canonical_instance_id": "c", "step_ids": ["action-c", "result-c"]},
                                {"canonical_instance_id": "a", "step_ids": ["action-a", "result-a"]},
                            ],
                        }
                    ]
                },
            },
        }
        pairing_response_path = root / "pairing-response.json"
        task_response_path = root / "response.json"
        write_json(pairing_response_path, {"kind": "live_manager_call", "purpose": "controlled pairing"})
        write_json(task_response_path, {"kind": "live_manager_call", "purpose": "controlled extraction"})
        pairing_journal_path = root / "pairing-journal.json"
        task_journal_path = root / "task-journal.json"
        write_json(pairing_journal_path, {"kind": "r012_pre_call_journal", "status": "prepared_before_manager_call"})
        write_json(task_journal_path, {"kind": "r012_pre_call_journal", "status": "prepared_before_manager_call"})
        pairing_call = {"call_id": "pair-0001", "json": {"action": "select", "selected_instance_ids": ["c", "a"], "reason": "same repair family", "shared_subprocedure": "inspect then repair", "instance_evidence": [{"canonical_instance_id": "c", "description_evidence": "repair"}, {"canonical_instance_id": "a", "description_evidence": "repair"}]}}
        pairing_journal = {"response": {"call_id": "pair-0001", "path": str(pairing_response_path), "sha256": sha256_file(pairing_response_path)}, "path": str(pairing_journal_path), "sha256": sha256_file(pairing_journal_path)}
        task_journal = {"response": {"call_id": "call-0001", "path": str(task_response_path), "sha256": sha256_file(task_response_path)}, "path": str(task_journal_path), "sha256": sha256_file(task_journal_path)}
        def finish_journal(path: Path, *, status: str, value: dict[str, object]) -> None:
            journal = json.loads(path.read_text(encoding="utf-8"))
            journal.update({"status": status, **value})
            write_json(path, journal)
        Executor._finish_journal = staticmethod(finish_journal)
        with patch("scripts.legacy.r015_task_extraction._manager_call", side_effect=[(pairing_call, pairing_journal, None), (call, task_journal, None)]):
            candidates, evidence = _task_extraction(
                context=context,
                input_value=input_value,
                traces=traces,
                task_candidate_records=[current_task_candidate],
                executor=Executor(),
            )
        self.assertEqual(evidence["status"], "generated")
        self.assertEqual(evidence["pairing"]["value"]["selected_instance_ids"], ["c", "a"])
        self.assertEqual(
            evidence["value"]["evidence"]["rule_evidence"][0]["sources"],
            [
                {"canonical_instance_id": "c", "step_ids": ["action-c", "result-c"]},
                {"canonical_instance_id": "a", "step_ids": ["action-a", "result-a"]},
            ],
        )
        self.assertEqual(candidates[0]["pairing"]["source_task_ids"], ["c", "a"])
        self.assertEqual(candidates[0]["pairing"]["source_candidate_ids"], [current_task_candidate["candidate_id"], prior_a_candidate["candidate_id"]])
        self.assertEqual([ref["task_id"] for ref in candidates[0]["pairing"]["trajectory_refs"]], ["c", "a"])
        self.assertEqual({item["task_id"] for item in evidence["ranking"]["ranked_candidates"]}, {"a", "b"})
        # D02 evidence must carry the final journal bytes, not the hash of the
        # mutable prepared journal returned by _manager_call.
        _validate_driver_payload_refs(evidence, field="d02-evidence")
        final_pairing_journal = json.loads(pairing_journal_path.read_text(encoding="utf-8"))
        self.assertEqual(final_pairing_journal["status"], "pairing_selected")

        no_related_response = root / "no-related-response.json"
        no_related_journal_path = root / "no-related-journal.json"
        write_json(no_related_response, {"kind": "live_manager_call", "purpose": "controlled no-related pairing"})
        write_json(no_related_journal_path, {"kind": "r012_pre_call_journal", "status": "prepared_before_manager_call"})
        no_related_journal = {"response": {"call_id": "pair-0002", "path": str(no_related_response), "sha256": sha256_file(no_related_response)}, "path": str(no_related_journal_path), "sha256": sha256_file(no_related_journal_path)}
        no_related_call = {"call_id": "pair-0002", "json": {"action": "no_related_group", "reason": "controlled no-related result"}}
        with patch("scripts.legacy.r015_task_extraction._manager_call", return_value=(no_related_call, no_related_journal, None)):
            no_related_candidates, no_related_evidence = _task_extraction(
                context=context,
                input_value=input_value,
                traces=traces,
                task_candidate_records=[current_task_candidate],
                executor=Executor(),
            )
        self.assertEqual(no_related_candidates, [])
        self.assertEqual(no_related_evidence["status"], "no_related_group")
        _validate_driver_payload_refs(no_related_evidence, field="d02-no-related-evidence")
        self.assertEqual(json.loads(no_related_journal_path.read_text(encoding="utf-8"))["status"], "pairing_no_related_group")

        tampered_a = deepcopy(prior_a_candidate)
        tampered_a["raw"]["model_output"]["skill"]["rules"] = ["Use a rewritten rule not present in the saved response."]
        tampered_a["skill"]["rules"] = ["Use a rewritten rule not present in the saved response."]
        tampered_a["candidate_fingerprint"] = sha256_text(canonical_json(tampered_a["skill"]))
        tampered_a["candidate_id"] = f"task-sop-{tampered_a['candidate_fingerprint'][:16]}"
        tampered_input = deepcopy(input_value)
        tampered_input["round_material"]["task_candidate_pool"][0] = tampered_a
        tampered_state = json.loads(state_path.read_text(encoding="utf-8"))
        tampered_state["rounds"]["1"]["task_candidate_pool"][0] = tampered_a
        write_json(state_path, tampered_state)
        with self.assertRaisesRegex(COnlyHarborDriverError, "differs from its saved manager response"):
            _task_extraction(
                context=context,
                input_value=tampered_input,
                traces=traces,
                task_candidate_records=[current_task_candidate],
                executor=Executor(),
            )

        write_json(
            state_path,
            {
                "rounds": {
                    "1": {
                        "assignments": {},
                        "completed_tasks": {
                            "a": {"outcome": "completed", "trajectory": prior_ref},
                            "b": {"outcome": "completed", "trajectory": later_ref},
                        },
                        "task_candidate_pool": [prior_a_candidate, prior_b_candidate],
                    }
                }
            },
        )

        future_state = json.loads(state_path.read_text(encoding="utf-8"))
        future_state["rounds"]["1"]["completed_tasks"].pop("b")
        write_json(state_path, future_state)
        with self.assertRaisesRegex(COnlyHarborDriverError, "not an earlier completed current-round task"):
            _task_extraction(
                context=context,
                input_value=input_value,
                traces=traces,
                task_candidate_records=[current_task_candidate],
                executor=Executor(),
            )
