"""Historical manager derivation and policy binding checks."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from codeskill_rebuild.types import canonical_json, sha256_file, sha256_text, write_json
from scripts import run_r015_c_only_harbor_driver as driver
from scripts.legacy import r015_task_extraction as legacy_task


class LegacyR015DerivationProvenanceTest(unittest.TestCase):


    def _trace(self) -> dict[str, object]:
        return {
            "source": {
                "instance_id": "task",
                "canonical_instance_id": "task",
                "task_name": "terminal-bench/task",
            },
            "instruction": "Repair the service.",
            "text_manager_eligible": True,
            "steps": [
                {"source_entry_id": "action", "role": "assistant", "content": []},
                {"source_entry_id": "result", "role": "toolResult", "content": []},
            ],
            "outcome": {"reward": 1},
        }

    def _policy(self, name: str) -> dict[str, str]:
        return driver._historical_thinking_policy_identity(
            {"driver_config": {"historical_thinking_policy": name}}
        )

    def _bound_call(
        self,
        root: Path,
        *,
        trace: dict[str, object],
        policy: str,
        model_output: dict[str, object],
        journal_status: str,
        call_id: str,
    ) -> tuple[dict[str, object], dict[str, object]]:
        call_dir = root / "model_calls" / call_id
        call_dir.mkdir(parents=True)
        messages = [
            {"role": "system", "content": "Extract only supported material."},
            {"role": "user", "content": json.dumps({"trajectory": trace})},
        ]
        context_path = root / f"{call_id}-context.json"
        projected = driver.project_historical_thinking(trace, policy=policy)["manager_trace"]
        write_json(
            context_path,
            {
                "historical_thinking_policy": policy,
                "historical_thinking_policy_version": driver.HISTORICAL_THINKING_POLICY_VERSION,
                "messages_sha256": driver._hash_json(messages),
                "sources": [
                    {
                        "source": deepcopy(trace["source"]),
                        "trace_sha256": driver._hash_json(trace),
                        "manager_view_sha256": driver._hash_json(projected),
                    }
                ],
            },
        )
        context_ref = driver._ref(context_path)
        metadata = {
            "trajectory_context": context_ref,
            "historical_thinking_policy": policy,
            "historical_thinking_policy_version": driver.HISTORICAL_THINKING_POLICY_VERSION,
        }
        purpose = f"fixture:{call_id}"
        request_path = call_dir / "request.json"
        response_path = call_dir / "response.json"
        journal_path = root / f"{call_id}-journal.json"
        write_json(
            request_path,
            {
                "kind": "live_manager_request",
                "purpose": purpose,
                "call_metadata": metadata,
                "request": {"messages": messages},
            },
        )
        api_response = {
            "choices": [
                {
                    "message": {"content": json.dumps(model_output)},
                    "finish_reason": "stop",
                }
            ]
        }
        write_json(
            response_path,
            {
                "kind": "live_manager_call",
                "raw_response": json.dumps(api_response),
                "parsed_response": api_response,
                "finish_reason": "stop",
            },
        )
        write_json(
            journal_path,
            {
                "call_id": call_id,
                "purpose": purpose,
                "status": journal_status,
                "call_metadata": metadata,
                "messages": messages,
                "messages_sha256": driver._hash_json(messages),
            },
        )
        response_ref = {
            "call_id": call_id,
            "path": str(response_path),
            "sha256": sha256_file(response_path),
        }
        derivation = {
            "request": driver._ref(request_path),
            "journal": driver._ref(journal_path),
            "trajectory_context": context_ref,
        }
        return response_ref, derivation

    def _candidate_output(self) -> dict[str, object]:
        return {
            "action": "generate",
            "skill": {
                "title": "Repair service SOP",
                "granularity": "general",
                "when_to_apply": "When a bounded service repair needs validation.",
                "rules": ["Inspect the failure, apply the repair, and rerun validation."],
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
                            {"canonical_instance_id": "task", "step_ids": ["action", "result"]}
                        ],
                    }
                ]
            },
        }

    def _candidate_record(
        self,
        *,
        trace: dict[str, object],
        response: dict[str, object],
        derivation: dict[str, object],
        wrapper_policy: str,
    ) -> dict[str, object]:
        model_output = self._candidate_output()
        checked = legacy_task.validate_task_candidate_with_evidence(
            model_output,
            trace,
            benchmark="terminal-bench",
            visible_step_ids=["action", "result"],
        )
        skill = driver._add_provenance(deepcopy(checked["skill"]), ["task"])
        trajectory_ref = {
            "round_id": 1,
            "task_id": "task",
            "trial_id": "r1:C:task",
            "session_id": "session-task",
            "path": "fixture-trace.json",
            "sha256": "a" * 64,
        }
        return {
            "candidate_id": "task-sop-fixture",
            "candidate_fingerprint": sha256_text(canonical_json(skill)),
            "status": "validated",
            "source": "current_round_c_only",
            "round_id": 1,
            "task_id": "task",
            "trial_id": "r1:C:task",
            "session_id": "session-task",
            "trajectory_ref": trajectory_ref,
            "skill": skill,
            "candidate_context": deepcopy(checked["candidate_context"]),
            "evidence": deepcopy(checked["evidence"]),
            "official_task_outcome": deepcopy(trace["outcome"]),
            "raw": {
                "manager_call_id": response["call_id"],
                "response": response,
                "model_output": model_output,
                "visible_step_ids": ["action", "result"],
                "historical_thinking_policy": self._policy(wrapper_policy),
                "derivation": derivation,
            },
        }

    def test_task_candidate_requires_actual_exclude_request_not_relabelled_keep(self) -> None:
        trace = self._trace()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_output = self._candidate_output()
            response, derivation = self._bound_call(
                root,
                trace=trace,
                policy="exclude",
                model_output=model_output,
                journal_status="task_candidate_generated",
                call_id="call-0001",
            )
            record = self._candidate_record(
                trace=trace, response=response, derivation=derivation, wrapper_policy="exclude"
            )
            validated = legacy_task._validate_task_candidate_record(
                record,
                field="candidate",
                expected_round=1,
                expected_task_id="task",
                expected_trace=trace,
                expected_trajectory_ref=record["trajectory_ref"],
                expected_historical_thinking_policy=self._policy("exclude"),
            )
            self.assertEqual(validated["candidate_id"], "task-sop-fixture")

            keep_response, keep_derivation = self._bound_call(
                root,
                trace=trace,
                policy="keep",
                model_output=model_output,
                journal_status="task_candidate_generated",
                call_id="call-0002",
            )
            relabelled = self._candidate_record(
                trace=trace,
                response=keep_response,
                derivation=keep_derivation,
                wrapper_policy="exclude",
            )
            with self.assertRaisesRegex(driver.COnlyHarborDriverError, "producing request used a different"):
                legacy_task._validate_task_candidate_record(
                    relabelled,
                    field="candidate",
                    expected_round=1,
                    expected_task_id="task",
                    expected_trace=trace,
                    expected_trajectory_ref=relabelled["trajectory_ref"],
                    expected_historical_thinking_policy=self._policy("exclude"),
                )

    def test_description_requires_actual_exclude_request_not_relabelled_keep(self) -> None:
        trace = self._trace()
        description = {
            "task_family": "service repair",
            "observed_obstacle": "validation failed",
            "attempted_procedure": "inspect and repair",
            "observed_outcome": "validation passed",
            "source_step_ids": ["result"],
        }
        trajectory_ref = {
            "round_id": 1,
            "task_id": "task",
            "trial_id": "r1:C:task",
            "session_id": "session-task",
            "path": "fixture-trace.json",
            "sha256": "a" * 64,
        }
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for actual_policy, call_id, should_pass in (
                ("exclude", "call-0001", True),
                ("keep", "call-0002", False),
            ):
                response, derivation = self._bound_call(
                    root,
                    trace=trace,
                    policy=actual_policy,
                    model_output=description,
                    journal_status="description_validated",
                    call_id=call_id,
                )
                record = {
                    "task_id": "task",
                    "round_id": 1,
                    "path": response["path"],
                    "sha256": response["sha256"],
                    "response_sha256": response["sha256"],
                    "manager_call_id": call_id,
                    "manager_response_path": response["path"],
                    "manager_response_sha256": response["sha256"],
                    "value": description,
                    "value_sha256": driver._hash_json(description),
                    "trajectory_ref": trajectory_ref,
                    "historical_thinking_policy": self._policy("exclude"),
                    "derivation": derivation,
                }
                if should_pass:
                    validated = legacy_task._validate_description_record(
                        record,
                        field="description",
                        expected_round=1,
                        expected_task_id="task",
                        expected_trajectory_ref=trajectory_ref,
                        expected_trace=trace,
                        expected_historical_thinking_policy=self._policy("exclude"),
                    )
                    self.assertEqual(validated["value"], description)
                else:
                    with self.assertRaisesRegex(driver.COnlyHarborDriverError, "producing request used a different"):
                        legacy_task._validate_description_record(
                            record,
                            field="description",
                            expected_round=1,
                            expected_task_id="task",
                            expected_trajectory_ref=trajectory_ref,
                            expected_trace=trace,
                            expected_historical_thinking_policy=self._policy("exclude"),
                        )
