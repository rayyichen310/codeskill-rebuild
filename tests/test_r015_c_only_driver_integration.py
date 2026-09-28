from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from codeskill_rebuild.c_only_protocol import COnlyProtocol
from codeskill_rebuild.bank import SkillBank
from codeskill_rebuild.manager import ManagerClient, ManagerProfile
from codeskill_rebuild.r012_execution import R012EvolutionMaintenanceExecutor
from codeskill_rebuild.task_graph_model import TaskCallResult
from codeskill_rebuild.runtime import render_skill
from codeskill_rebuild.types import canonical_json, sha256_file, sha256_text, write_json
from scripts.run_r015_c_only import (
    _apply_driver_output,
    _driver_process_path,
    _driver_stage_path,
    _run_driver,
    _write_driver_input,
)
from scripts.run_r015_c_only_harbor_driver import (
    COnlyHarborDriverError,
    _continue_from_completed_trial,
    _run_trial,
    _session_id,
    _supplied_maintenance,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "r015-c-only-coding.json"
BASELINE = ROOT / "docs" / "baselines" / "r015-legacy-coding-baseline-20260913.json"


class _ControlledEncoder:
    repo_id = "controlled-minilm"
    resolved_revision = "controlled-revision"

    def index_description(self, value: dict[str, object]) -> tuple[list[float], dict[str, object]]:
        return [1.0, 0.0], {"kind": "controlled-description-index", "task_family": value["task_family"]}

    def index_skill(self, value: dict[str, object]) -> tuple[list[float], dict[str, object]]:
        return [1.0, 0.0], {"kind": "controlled-skill-index", "granularity": value["granularity"]}


class _ControlledTaskChat:
    """Save exact-shaped offline wire artifacts while real Task stages run."""

    def __init__(self, *, root: Path, model: str, **kwargs):
        del kwargs
        self.root = Path(root)
        self.model = model
        self.base_url = "http://offline.local/v1"
        self.output_tokens = 65536
        self.allowance = 454656
        self.token_counter = lambda messages, *, request_options: 10

    def call(self, *, thread_id, stage, messages, schema, identity):
        task_id = identity["task_id"]
        request = {"thread_id": thread_id, "stage": stage, "messages": messages,
                   "schema": schema, "identity": identity, "model": self.model,
                   "base_url": "http://offline.local/v1", "temperature": 0,
                   "reasoning_effort": "max", "max_tokens": self.output_tokens}
        call_key = sha256_text(canonical_json(request))
        call_dir = self.root / "task-calls" / call_key
        call_dir.mkdir(parents=True, exist_ok=True)
        user = json.loads(messages[1]["content"])
        if stage.startswith("event-generate-"):
            local = next((step["source_entry_id"] for step in user["segment_steps"]
                          if step["role"] == "assistant"), None)
            value = ({"action": "generate", "skill": {
                "title": "Inspect a local failure", "granularity": "event-driven",
                "when_to_apply": "When a local check fails during a repair",
                "rules": ["Inspect the failure and verify the repair."]},
                "evidence": {"step_ids": [local]}}
                if local else {"action": "skip", "reason": "No visible local action"})
        elif stage == "d01":
            value = {"task_family": "shared diagnostic repair",
                     "observed_obstacle": "controlled failure",
                     "attempted_procedure": "inspect and verify",
                     "observed_outcome": "completed",
                     "source_step_ids": ["observe-1"]}
        elif stage.startswith("generation-"):
            action = next((step["source_entry_id"] for step in user["segment_steps"]
                           if step["role"] == "assistant"), None)
            if action is None:
                value = {"action": "skip", "reason": "no original action/result pair"}
            else:
                value = {"action": "generate",
                         "skill": {"title": f"Controlled SOP for {task_id}",
                                   "granularity": "general",
                                   "when_to_apply": "When a bounded repair needs validation.",
                                   "rules": ["Inspect the failure and verify the repair."]},
                         "candidate_context": {"task_goal": "Complete the repair",
                                               "whole_task_outcome": "completed",
                                               "hard_constraints": [],
                                               "environment_assumptions": [],
                                               "observed_results": [],
                                               "known_limitations": []},
                         "evidence": {"step_ids": [action]}}
        elif stage == "d02_pairing":
            prior_ids = [item["canonical_instance_id"] for item in user["candidates"][:2]]
            selected_ids = [task_id, *prior_ids]
            value = {"action": "select", "selected_instance_ids": selected_ids,
                     "reason": "same controlled procedure",
                     "shared_subprocedure": "inspect and verify",
                     "instance_evidence": [
                         {"canonical_instance_id": source,
                          "description_evidence": "same procedure"}
                         for source in selected_ids]}
        elif stage.startswith("fig6_"):
            assert "trajectories" not in user
            assert all("trajectory_ref" not in item and "evidence" not in item
                       for item in user["selected_sops"])
            source_candidates = [item["candidate_id"] for item in user["selected_sops"]]
            value = {"action": "generate",
                     "skill": {"title": "Controlled shared diagnostic workflow",
                               "granularity": "general",
                               "when_to_apply": "When a bounded repair needs validation.",
                               "rules": ["Inspect the failure and verify the repair."]},
                     "evidence": {"source_candidate_ids": source_candidates[:2]}}
        elif stage.startswith("fig9_task_"):
            value = {"action": "add", "reason": "controlled repeatable repair",
                     "evidence": {"source_skill_ids": [user["candidate_skill"]["skill_id"]],
                                  "source_example_ids": []}}
        else:
            raise AssertionError(f"unexpected Task stage {stage}")
        write_json(call_dir / "identity.json", request)
        write_json(call_dir / "wire-request.json", {"messages": messages,
                   "model": self.model, "temperature": 0,
                   "reasoning_effort": "max", "max_tokens": self.output_tokens,
                   "stream": False,
                   "response_format": {"type": "json_schema", "json_schema": schema}})
        write_json(call_dir / "wire-response.json", {
            "choices": [{"message": {"content": json.dumps(value)},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10}})
        return TaskCallResult("ok", value, call_key, call_dir)


def _controlled_manager(run_dir: Path, ledger_path: Path) -> ManagerClient:
    return ManagerClient(
        profile=ManagerProfile(base_url="http://offline.local/v1", model="offline",
                               max_total_calls=None),
        run_dir=run_dir / "manager", contract={}, ledger_path=ledger_path,
        exact_token_counter=lambda messages, *, request_options: 10,
    )


class COnlyDriverIntegrationTest(unittest.TestCase):
    """Exercise the built-in driver boundary with controlled external I/O.

    The test never calls a model or Harbor.  It uses real protocol state,
    real driver extraction/publication functions, real R012 journal finalizing,
    and strict production payload validation around deterministic transports.
    """

    def test_evolution_add_requires_both_original_source_steps_visible_at_driver_boundary(self) -> None:
        trace = {
            "source": {"canonical_instance_id": "build-pmars", "instance_id": "build-pmars"},
            "instruction": "Extract the current archive.",
            "steps": [
                {
                    "source_entry_id": "action",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_call",
                            "tool_call_id": "call",
                            "tool_name": "exec",
                            "arguments": {"command": "python -m tarfile -e source.tar.xz output"},
                        }
                    ],
                    "assistant": {
                        "tool_calls": [
                            {
                                "type": "tool_call",
                                "tool_call_id": "call",
                                "tool_name": "exec",
                                "arguments": {"command": "python -m tarfile -e source.tar.xz output"},
                            }
                        ]
                    },
                },
                {
                    "source_entry_id": "result",
                    "role": "toolResult",
                    "content": [{"type": "text", "text": "extracted\n"}],
                    "tool_result": {
                        "tool_call_id": "call",
                        "tool_name": "exec",
                        "is_error": False,
                        "details": {"exitCode": 0, "aggregated": "extracted\n"},
                    },
                },
            ],
            "historical_compaction": {
                "control_event_ids": [],
                "raw_message_step_ids": ["action", "result"],
            },
            "outcome": {"official_reward": "0", "result_present": True},
        }
        bank = SkillBank.empty("terminal-bench")
        added = bank.apply(
            operation_id="visibility-base",
            decision="add",
            candidate={
                "title": "Inspect archive requirements",
                "granularity": "task",
                "when_to_apply": "When archive handling requirements must be checked.",
                "rules": ["Inspect the current archive requirements before acting."],
                "benchmark": "terminal-bench",
            },
            source_instance_ids=["earlier-source"],
            evidence={"fixture": "method-only supplied skill"},
        )
        base = deepcopy(next(skill for skill in bank.skills if skill.get("skill_id") == added["result_skill_id"]))
        block = "[CODESKILL TASK PRIOR KNOWLEDGE]\n" + render_skill(base)
        trial_id = "r1:build-pmars:C:repeat-1"
        attempts = [
            {
                "trial_id": trial_id,
                "attempt_ordinal": 1,
                "forwarded_request_ordinal": 1,
                "proxy_outcome": "stream_forwarded",
                "forwarded_request": {"messages": [{"role": "user", "content": block}]},
                "task_selection": {
                    "block": block,
                    "block_sha256": hashlib.sha256(block.encode("utf-8")).hexdigest(),
                    "injected_skills": [
                        {
                            "skill": base,
                            "rendered_skill_sha256": hashlib.sha256(render_skill(base).encode("utf-8")).hexdigest(),
                            "anchor_id": "initial-user",
                        }
                    ],
                },
                "event_selection": [],
            }
        ]
        raw_example = {
            "language": "python",
            "purpose": "Extract the current archive selected by the caller.",
            "source": {
                "canonical_instance_id": "build-pmars",
                "action_step_id": "action",
                "tool_call_id": "call",
                "result_step_id": "result",
            },
            "generated_code": """from pathlib import Path
import tarfile

archive_path = Path("source.tar.xz")
with tarfile.open(archive_path, "r:xz") as archive:
    archive.extractall(filter="data")""",
            "adaptations": [
                {
                    "kind": "parameterized",
                    "source_fragment": "source.tar.xz",
                    "generated_fragment": "archive_path",
                    "rationale": "Select the current archive.",
                    "applicability": "When the archive path varies by task.",
                },
                {
                    "kind": "other_rewrite",
                    "source_fragment": "python -m tarfile -e",
                    "generated_fragment": "archive.extractall(filter=\"data\")",
                    "rationale": "Use an explicit extraction filter.",
                    "applicability": "When the Python version supports the data filter.",
                },
            ],
            "prerequisites": ["The selected archive is readable."],
            "known_limitations": ["Choose a filter for the current trust boundary."],
            "unknowns": [],
        }
        evolution_json = {
            "action": "evolve",
            "target_skill_id": base["skill_id"],
            "target_skill_version": base["version"],
            "reason": "add executable detail supported by the current trajectory",
            "skill": {
                "title": base["title"],
                "granularity": "general",
                "when_to_apply": base["when_to_apply"],
                "rules": deepcopy(base["rules"]),
            },
            "code_example_changes": [{"action": "add", "example": raw_example}],
        }

        visibility_cases = {
            "both-visible": ["action", "result"],
            "action-omitted": ["result"],
            "result-omitted": ["action"],
            "both-omitted": [],
            "ordinary-full-flow": None,
        }
        for label, visible in visibility_cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                executor = R012EvolutionMaintenanceExecutor(
                    manager=object(),
                    encoder=_ControlledEncoder(),
                    journal_root=Path(tmp) / "journals",
                    instance_id="build-pmars",
                    profile={
                        "kind": "r012_execution_profile",
                        "event_selection": {
                            "profile_ref": "visibility-fixture",
                            "selection_rule_ref": "visibility-fixture",
                            "max_matching_skills": 1,
                            "skill_token_budget": 256,
                        },
                        "evolution": {
                            "full_lifecycle_arms": ["C"],
                            "explicit_selection_manifest_required": True,
                            "candidate_selection_mode": "all_actually_supplied",
                        },
                    },
                    selections={},
                )
                call = {"call_id": f"{label}-call", "json": deepcopy(evolution_json)}
                if visible is not None:
                    call["visible_step_ids_by_source"] = {"build-pmars": visible}
                journal = {
                    "path": str(Path(tmp) / "evolution.json"),
                    "sha256": "journal-before",
                    "response": {"path": str(Path(tmp) / "response.json"), "sha256": "response-sha"},
                }
                finalized: list[dict[str, object]] = []

                def finish(_executor, journal_ref, *, status, value):
                    finalized.append({"status": status, "value": deepcopy(value)})
                    return deepcopy(journal_ref)

                with patch(
                    "scripts.run_r015_c_only_harbor_driver._manager_call",
                    return_value=(call, journal, None),
                ), patch(
                    "scripts.run_r015_c_only_harbor_driver._finish_manager_journal",
                    side_effect=finish,
                ), patch(
                    "scripts.run_r015_c_only_harbor_driver._fig9_operation",
                    return_value={
                        "candidate": {"provenance": {}},
                        "evidence": {},
                    },
                ) as fig9:
                    if label in {"both-visible", "ordinary-full-flow"}:
                        decisions, operations = _supplied_maintenance(
                            context={"trial_id": trial_id, "task_id": "build-pmars"},
                            executor=executor,
                            bank=bank,
                            trajectory=trace,
                            attempts=attempts,
                        )
                        self.assertEqual(decisions[0]["action"], "evolve")
                        self.assertEqual(len(operations), 1)
                        self.assertEqual(len(fig9.call_args.kwargs["candidate"]["code_examples"]), 1)
                        self.assertIn("fig8_evolve_validated", [item["status"] for item in finalized])
                    else:
                        with self.assertRaisesRegex(COnlyHarborDriverError, "absent from supplied original fragments"):
                            _supplied_maintenance(
                                context={"trial_id": trial_id, "task_id": "build-pmars"},
                                executor=executor,
                                bank=bank,
                                trajectory=trace,
                                attempts=attempts,
                            )
                        fig9.assert_not_called()
                        rejected = [item for item in finalized if item["status"] == "fig8_output_rejected"]
                        self.assertEqual(len(rejected), 1)
                        self.assertIn("absent from supplied original fragments", rejected[0]["value"]["error"])
                        self.assertEqual(rejected[0]["value"]["model_output"], evolution_json)

    def test_real_driver_and_coordinator_complete_two_independent_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            run_dir.mkdir()
            state_path = run_dir / "state.json"
            # Keep the production 12-task config untouched while using a
            # small four-task manifest for this model-free integration test.
            # Four tasks exercise the early D02 skip, later description
            # ranking/pairing, bank growth, and two complete independent
            # rounds without making the unit suite pay the full 24-trial
            # state-validation cost.
            fixture_config = root / "config.json"
            fixture_baseline = root / "baseline.json"
            config_value = json.loads(CONFIG.read_text(encoding="utf-8"))
            baseline_value = json.loads(BASELINE.read_text(encoding="utf-8"))
            config_value["tasks"] = deepcopy(config_value["tasks"][:4])
            for order, task in enumerate(config_value["tasks"], start=1):
                task["order"] = order
            baseline_value["tasks"] = deepcopy(baseline_value["tasks"][:4])
            for order, task in enumerate(baseline_value["tasks"], start=1):
                task["order"] = order
            config_value["baseline_manifest"] = {
                "path": str(fixture_baseline),
                "sha256": "0" * 64,
                "comparison_only": True,
                "solver_input_imported": False,
                "skills_imported": False,
                "trajectories_imported": False,
            }
            write_json(fixture_baseline, baseline_value)
            config_value["baseline_manifest"]["sha256"] = sha256_file(fixture_baseline)
            write_json(fixture_config, config_value)
            protocol = COnlyProtocol.initialize(fixture_config, fixture_baseline, state_path)
            protocol.authorize_start()
            protocol.save(state_path)

            task_root = root / "tasks"
            task_root.mkdir()
            for task in protocol.tasks:
                task_dir = task_root / task["canonical_instance_id"]
                task_dir.mkdir()
                task_toml = task_dir / "task.toml"
                task_toml.write_text("[metadata]\nname = 'controlled'\n", encoding="utf-8")

            ledger_path = run_dir / "manager-ledger.json"
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
            write_json(ledger_path, {"schema_version": 2, "limit": "unlimited", "calls": []})
            call_counter = {"value": 0}
            trial_processes: list[dict[str, object]] = []
            snapshot_refs: list[dict[str, object]] = []
            event_thread_ids: list[str] = []
            event_graph_dirs: list[Path] = []

            def paths_and_metadata(input_value: dict[str, object]):
                task_id = str(input_value["assignment"]["task_id"])
                task_dir = task_root / task_id
                task_toml = task_dir / "task.toml"
                metadata = {
                    "task_name": f"terminal-bench/{task_id}",
                    "task_path": str(task_dir),
                    "task_checkout_commit": "controlled-public-checkout",
                    "task_toml": {
                        "path": str(task_toml),
                        "sha256": sha256_file(task_toml),
                        "size_bytes": task_toml.stat().st_size,
                    },
                    "public_environment": {
                        "docker_image": f"controlled/{task_id}:test",
                        "docker_image_identity": "sha256:" + "1" * 64,
                        "agent_timeout_sec": 30,
                        "verifier_timeout_sec": 30,
                        "build_timeout_sec": 30,
                    },
                }
                paths = {
                    "task_root": task_root,
                    "harbor": "controlled-harbor",
                    "python": Path(os.sys.executable),
                    "plugin": ROOT / "openclaw_plugin",
                    "sidecar": ROOT / "scripts" / "run_openclaw_r012_sidecar.py",
                    "manager_ledger": ledger_path,
                    "manager_config": None,
                }
                return paths, metadata, task_dir

            def controlled_import(context: dict[str, object], process: dict[str, object]) -> dict[str, object]:
                task_id = str(context["task_id"])
                trial_id = str(context["trial_id"])
                round_id = int(trial_id.split(":", 1)[0].removeprefix("r"))
                trace = {
                    "source": {
                        "canonical_instance_id": task_id,
                        "instance_id": task_id,
                        "task_name": f"terminal-bench/{task_id}",
                    },
                    "instruction": f"Repair the controlled {task_id} task.",
                    "steps": [
                        {
                            "source_entry_id": "action-1",
                            "role": "assistant",
                            "content": "apply the controlled repair",
                            "assistant": {"tool_calls": [{"tool_call_id": "repair-call",
                                                           "tool_name": "exec",
                                                           "arguments": {"command": "printf repaired"}}]},
                        },
                        {
                            "source_entry_id": "observe-1",
                            "role": "toolResult",
                            "content": "controlled observable failure",
                            "tool_result": {"tool_call_id": "repair-call",
                                            "tool_name": "exec", "is_error": False},
                        }
                    ],
                    "outcome": {"reward": 1.0, "status": "completed"},
                    "text_manager_eligible": True,
                    "historical": False,
                    "source_kind": "controlled_current_c_only",
                    "r015_binding": {
                        "round_id": round_id,
                        "task_id": task_id,
                        "trial_id": trial_id,
                        "session_id": str(context["session_id"]),
                    },
                }
                trace_path = Path(context["artifact_root"]) / "trajectory-live.json"
                write_json(trace_path, trace)
                self.assertTrue(trace_path.is_file(), str(trace_path))
                trace_ref = {
                    "round_id": round_id,
                    "task_id": task_id,
                    "trial_id": trial_id,
                    "session_id": str(context["session_id"]),
                    "complete": True,
                    "path": str(trace_path),
                    "sha256": sha256_file(trace_path),
                }
                raw_evidence = {
                    "official_harbor_trial": True,
                    "official_trial_boundary_started": True,
                    "evidence_mode": "official_live",
                    "condition": "C-only",
                    "round_id": round_id,
                    "task_id": task_id,
                    "trial_id": trial_id,
                    "session_id": str(context["session_id"]),
                    "historical_baseline_used": False,
                    "baseline_imported": False,
                    "controlled_transport": True,
                }
                return {
                    "outcome": "completed",
                    "trajectory": trace_ref,
                    "raw_evidence": raw_evidence,
                    "proxy_attempt_records": [],
                    "trace": trace,
                }

            def controlled_manager_context(context: dict[str, object], *, output_path: Path):
                task_id = str(context["task_id"])
                manager_context = {
                    "manager_ledger": ledger_path,
                    "manager_ledger_before_calls": len(json.loads(ledger_path.read_text(encoding="utf-8"))["calls"]),
                }
                executor = R012EvolutionMaintenanceExecutor(
                    manager=_controlled_manager(run_dir, ledger_path),
                    encoder=_ControlledEncoder(),
                    journal_root=run_dir / "manager-journals" / task_id,
                    instance_id=task_id,
                    profile=deepcopy(context["profile"]),
                    selections={
                        str(context["trial_id"]): {
                            "trial_id": str(context["trial_id"]),
                            "instance_id": task_id,
                            "arm": "C",
                            "action": "evaluate_all_supplied",
                            "reason": "controlled integration evaluates all supplied skills",
                        }
                    },
                    evolution_prompt="controlled evolution prompt",
                    maintenance_prompt="controlled maintenance prompt",
                )
                return object(), executor, manager_context

            def controlled_manager_call(
                executor: R012EvolutionMaintenanceExecutor,
                *,
                trial_id: str,
                phase: str,
                purpose: str,
                messages: list[dict[str, str]],
                metadata: dict[str, object],
                trajectory_context=None, source_traces=None, messages_builder=None,
            ):
                if messages_builder is not None:
                    self.assertEqual(messages_builder(source_traces), messages)
                del messages
                call_counter["value"] += 1
                call_id = f"call-{call_counter['value']:04d}"
                manager_root = ledger_path.parent / "manager"
                response_path = manager_root / "model_calls" / call_id / "response.json"
                response_path.parent.mkdir(parents=True, exist_ok=False)
                if phase.startswith("event-"):
                    model_json: dict[str, object] = {"action": "skip", "reason": "controlled event extraction skip"}
                elif phase == "description":
                    model_json = {
                        "task_family": "shared diagnostic repair",
                        "observed_obstacle": "the controlled task exposed a reproducible obstacle",
                        "attempted_procedure": "inspect the failure and rerun the bounded validation",
                        "observed_outcome": "the controlled task completed",
                        "source_step_ids": ["observe-1"],
                    }
                elif phase == "task-sop-candidate":
                    current = str(metadata["task_id"])
                    model_json = {
                        "action": "generate",
                        "skill": {
                            "title": f"Controlled SOP for {current}",
                            "granularity": "general",
                            "when_to_apply": "When a bounded terminal repair needs validation.",
                            "rules": ["Inspect the failure, apply the bounded repair, and observe validation."],
                        },
                        "candidate_context": {
                            "task_goal": "Complete the controlled repair",
                            "whole_task_outcome": "completed",
                            "hard_constraints": [],
                            "environment_assumptions": [],
                            "observed_results": ["bounded validation completed"],
                            "known_limitations": [],
                        },
                        "evidence": {"rule_evidence": [{"rule_index": 0, "sources": [{"canonical_instance_id": current, "step_ids": ["action-1", "observe-1"]}]}]},
                    }
                elif phase == "task-pairing":
                    ranked_ids = metadata.get("ranked_task_ids")
                    self.assertIsInstance(ranked_ids, list)
                    prior = str(ranked_ids[0])
                    current = str(metadata["task_id"])
                    model_json = {
                        "action": "select",
                        "selected_instance_ids": [current, prior],
                        "reason": "controlled tasks share the same diagnostic procedure",
                        "shared_subprocedure": "inspect the failure and rerun bounded validation",
                        "instance_evidence": [
                            {"canonical_instance_id": current, "description_evidence": "same procedure"},
                            {"canonical_instance_id": prior, "description_evidence": "same procedure"},
                        ],
                    }
                elif phase.startswith("task-extraction-"):
                    self.assertIsInstance(source_traces, list)
                    model_json = {
                        "action": "generate",
                        "skill": {
                            "title": f"Controlled diagnostic workflow for {metadata['task_id']}",
                            "granularity": "general",
                            "when_to_apply": "When a controlled terminal repair needs a reproducible validation loop.",
                            "rules": ["Inspect the failure, apply the bounded repair, and rerun validation."],
                        },
                        "evidence": {
                            "rule_evidence": [
                                {
                                    "rule_index": 0,
                                    "sources": [
                                        {
                                            "canonical_instance_id": str(trace["source"]["canonical_instance_id"]),
                                            "step_ids": ["action-1", "observe-1"],
                                        }
                                        for trace in source_traces
                                    ],
                                }
                            ]
                        },
                    }
                elif phase.startswith("fig9-extraction-"):
                    model_json = {"action": "add", "reason": "controlled extracted candidate is reproducible",
                                  "evidence": {"source_skill_ids": ["candidate"], "source_example_ids": []}}
                else:
                    self.fail(f"unexpected controlled manager phase: {phase}")
                api_response = {
                    "choices": [
                        {
                            "message": {"content": json.dumps(model_json)},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10},
                }
                write_json(
                    response_path,
                    {
                        "kind": "live_manager_call",
                        "purpose": purpose,
                        "metadata": metadata,
                        "controlled": True,
                        "http_status": 200,
                        "raw_response": json.dumps(api_response),
                        "parsed_response": api_response,
                        "usage": api_response["usage"],
                        "finish_reason": "stop",
                    },
                )
                journal_path = executor._journal_path(trial_id, phase)
                journal_path.parent.mkdir(parents=True, exist_ok=True)
                write_json(
                    journal_path,
                    {
                        "schema_version": 1,
                        "kind": "r012_pre_call_journal",
                        "trial_id": trial_id,
                        "phase": phase,
                        "status": "prepared_before_manager_call",
                        "purpose": purpose,
                        "call_metadata": deepcopy(metadata),
                    },
                )
                ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                ledger["calls"].append(
                    {
                        "run_dir": str(manager_root),
                        "call_id": call_id,
                        "purpose": purpose,
                        "status": "succeeded",
                    }
                )
                write_json(ledger_path, ledger)
                response_ref = {
                    "call_id": call_id,
                    "path": str(response_path),
                    "sha256": sha256_file(response_path),
                }
                return (
                    {"call_id": call_id, "json": model_json},
                    {"path": str(journal_path), "sha256": sha256_file(journal_path), "response": response_ref},
                    None,
                )

            def record_process(context: dict[str, object]) -> dict[str, object]:
                process = {
                    "kind": "r015_c_only_official_harbor_process",
                    "classification": "completed",
                    "official_trial_boundary_started": True,
                    "harbor_returncode": 0,
                    "controlled_transport": True,
                    "trial_id": context["trial_id"],
                }
                trial_processes.append(process)
                return process

            def run_round(expected_round: int) -> None:
                while protocol.current_task_id is not None:
                    task_id = str(protocol.current_task_id)
                    assignment = protocol.freeze_task(task_id)
                    protocol.save(state_path)
                    input_path = _write_driver_input(protocol, assignment, run_dir, state_path)
                    output_path = input_path.with_name("driver-output.json")
                    with patch(
                        "scripts.run_r015_c_only_harbor_driver._task_root_and_service",
                        side_effect=paths_and_metadata,
                    ), patch(
                        "scripts.run_r015_c_only_harbor_driver._sidecar_check",
                        return_value={"returncode": 0, "controlled": True},
                    ), patch(
                        "scripts.run_r015_c_only_harbor_driver._harbor_config_check",
                        return_value={"status": "valid", "controlled": True},
                    ), patch(
                        "scripts.run_r015_c_only_harbor_driver._run_official_harbor",
                        side_effect=lambda context: record_process(context),
                    ), patch(
                        "scripts.run_r015_c_only_harbor_driver._import_official_evidence",
                        side_effect=controlled_import,
                    ), patch(
                        "scripts.run_r015_c_only_harbor_driver._manager_context",
                        side_effect=controlled_manager_context,
                    ), patch(
                        "scripts.run_r015_c_only_harbor_driver._manager_call",
                        side_effect=controlled_manager_call,
                    ), patch(
                        "scripts.run_r015_c_only_harbor_driver.TaskChatBoundary",
                        _ControlledTaskChat,
                    ):
                        output = _run_trial(json.loads(input_path.read_text(encoding="utf-8")), input_path, output_path)
                    self.assertEqual(output["round_id"], expected_round)
                    self.assertEqual(output["condition"], "C-only")
                    self.assertEqual(output["evidence_mode"], "official_live")
                    _apply_driver_output(protocol, output, assignment, state_path, manager_root=run_dir / "manager")
                    publication = output["publication"]
                    snapshot = publication["evidence"]["manager_ledger"]
                    snapshot_refs.append(snapshot)
                    event = output["extraction"]["evidence"]["event"]
                    self.assertEqual(event["kind"], "r015_event_graph_v3")
                    event_thread_ids.append(event["thread_id"])
                    event_graph_dirs.append(Path(event["directory"]))
                    protocol.save(state_path)

            run_round(1)
            round_one = COnlyProtocol.load(state_path, fixture_config, fixture_baseline)
            self.assertEqual(round_one._round(1)["status"], "complete")
            self.assertEqual(len(round_one._round(1)["description_pool"]), len(round_one.tasks))
            self.assertEqual(len(round_one._round(1)["task_candidate_pool"]), len(round_one.tasks))
            self.assertGreater(len(round_one._round(1)["bank"]["skills"]), 0)
            formal_operations = [operation for operation in round_one._round(1)["bank"]["operations"]
                                 if operation.get("evidence", {}).get("extraction_source", {}).get("kind")
                                 == "task_graph_sop_merge_v1"]
            self.assertTrue(formal_operations)
            for operation in round_one._round(1)["bank"]["operations"]:
                if operation.get("evidence", {}).get("kind") == "r015_c_only_fig9_manager_evidence":
                    self.assertEqual(operation["evidence"]["decision_references"],
                                     {"source_skill_ids": ["candidate"], "source_example_ids": []})
            source_sops = {item["candidate_id"]: item
                           for item in round_one._round(1)["task_candidate_pool"]}
            for operation in formal_operations:
                derivation = operation["evidence"]["extraction_source"]
                cited = derivation["model_output"]["evidence"]["source_candidate_ids"]
                self.assertEqual([item["candidate_id"] for item in derivation["source_sop_evidence"]], cited)
                self.assertEqual(operation["candidate"]["provenance"]["source_instance_ids"],
                                 sorted(source_sops[item]["task_id"] for item in cited))
                for linked in derivation["source_sop_evidence"]:
                    original = source_sops[linked["candidate_id"]]
                    self.assertEqual(linked["evidence"], original["evidence"])
                    self.assertEqual(linked["trajectory_ref"], original["trajectory_ref"])
                    self.assertEqual(linked["generation"], original["raw"])
                published_skill = next(skill for skill in round_one._round(1)["bank"]["skills"]
                                       if skill["skill_id"] == operation["result_skill_id"])
                from codeskill_rebuild.runtime import render_skill
                rendered = render_skill(published_skill)
                self.assertNotIn("source_candidate_ids", rendered)
                self.assertTrue(all(item not in rendered for item in cited))
            round_one_pool_paths = {item["path"] for item in round_one._round(1)["trajectory_pool"]}
            round_one_bank_hash = SkillBank.from_dict(round_one._round(1)["bank"]).snapshot()["state_sha256"]

            protocol.start_next_round()
            protocol.save(state_path)
            self.assertEqual(protocol.current_round_id, 2)
            self.assertEqual(protocol._round(2)["status"], "active")
            self.assertEqual(protocol._round(2)["trajectory_pool"], [])
            self.assertEqual(protocol._round(2)["description_pool"], [])
            self.assertEqual(protocol._round(2)["task_candidate_pool"], [])
            self.assertEqual(protocol._round(2)["bank"]["skills"], [])
            self.assertNotEqual(protocol._round(2).get("created_from_round1_state_sha256"), round_one_bank_hash)

            run_round(2)
            restored = COnlyProtocol.load(state_path, fixture_config, fixture_baseline)
            self.assertEqual(restored._round(2)["status"], "complete")
            self.assertGreater(len(restored._round(2)["bank"]["skills"]), 0)
            self.assertEqual(len(restored._round(2)["description_pool"]), len(restored.tasks))
            self.assertEqual(len(restored._round(2)["task_candidate_pool"]), len(restored.tasks))
            round_two_pool_paths = {item["path"] for item in restored._round(2)["trajectory_pool"]}
            self.assertTrue(round_one_pool_paths.isdisjoint(round_two_pool_paths))
            self.assertEqual(len(trial_processes), len(restored.tasks) * 2)
            self.assertEqual(len(snapshot_refs), len(restored.tasks) * 2)
            self.assertEqual(len(set(event_thread_ids)), len(snapshot_refs))
            self.assertEqual(call_counter["value"], len(restored.tasks) * 2)
            graph_calls = [json.loads(path.read_text(encoding="utf-8"))
                           for path in (run_dir / "manager" / "task-calls").glob("*/identity.json")]
            self.assertEqual(sum(call["stage"] == "event-generate-000" for call in graph_calls),
                             len(restored.tasks) * 2)
            self.assertGreater(
                len(list((run_dir / "manager" / "task-calls").glob("*/wire-response.json"))),
                len(restored.tasks) * 2,
            )
            for round_id in (1, 2):
                round_state = restored._round(round_id)
                self.assertTrue(all(item["round_id"] == round_id for item in round_state["trajectory_pool"]))
                self.assertTrue(all(item["round_id"] == round_id for item in round_state["description_pool"]))
                self.assertTrue(all(item["round_id"] == round_id for item in round_state["task_candidate_pool"]))
                self.assertTrue(
                    all(
                        operation["evidence"]["c_only_round"] == round_id
                        for operation in round_state["bank"]["operations"]
                    )
                )
            self.assertEqual(len(event_graph_dirs), len(restored.tasks) * 2)
            self.assertTrue(all((directory / "checkpoints.sqlite").is_file()
                                for directory in event_graph_dirs))

    def test_completed_trial_stage_uses_continuation_without_relaunch(self) -> None:
        """A paid completed trial can continue manager phases exactly once."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            run_dir.mkdir()
            state_path = run_dir / "state.json"
            fixture_config = root / "config.json"
            fixture_baseline = root / "baseline.json"
            config_value = json.loads(CONFIG.read_text(encoding="utf-8"))
            baseline_value = json.loads(BASELINE.read_text(encoding="utf-8"))
            config_value["tasks"] = deepcopy(config_value["tasks"][:1])
            config_value["tasks"][0]["order"] = 1
            baseline_value["tasks"] = deepcopy(baseline_value["tasks"][:1])
            baseline_value["tasks"][0]["order"] = 1
            config_value["baseline_manifest"] = {
                "path": str(fixture_baseline),
                "sha256": "0" * 64,
                "comparison_only": True,
                "solver_input_imported": False,
                "skills_imported": False,
                "trajectories_imported": False,
            }
            write_json(fixture_baseline, baseline_value)
            config_value["baseline_manifest"]["sha256"] = sha256_file(fixture_baseline)
            write_json(fixture_config, config_value)
            protocol = COnlyProtocol.initialize(fixture_config, fixture_baseline, state_path)
            protocol.authorize_start()
            protocol.save(state_path)
            assignment = protocol.freeze_task("build-pmars")
            protocol.save(state_path)
            input_path = _write_driver_input(protocol, assignment, run_dir, state_path)
            task_dir = input_path.parent
            trajectory_path = task_dir / "completed-trial-trajectory.json"
            trace = {
                "source": {
                    "canonical_instance_id": "build-pmars",
                    "instance_id": "build-pmars",
                    "task_name": "terminal-bench/build-pmars",
                },
                "instruction": "Complete the controlled task.",
                "steps": [{"source_entry_id": "observe-1", "role": "toolResult", "content": "done"}],
                "outcome": {"reward": 1.0},
                "text_manager_eligible": True,
                "r015_binding": {
                    "round_id": 1,
                    "task_id": "build-pmars",
                    "trial_id": assignment["trial_id"],
                    "session_id": "continuation-session-r1",
                },
            }
            write_json(trajectory_path, trace)
            trajectory = {
                "round_id": 1,
                "task_id": "build-pmars",
                "trial_id": assignment["trial_id"],
                "session_id": "continuation-session-r1",
                "complete": True,
                "path": str(trajectory_path),
                "sha256": sha256_file(trajectory_path),
            }
            trial = {
                "condition": "C-only",
                "round_id": 1,
                "task_id": "build-pmars",
                "trial_id": assignment["trial_id"],
                "outcome": "completed",
                "trajectory": trajectory,
                "supplied_skills": [],
                "raw_evidence": {
                    "official_harbor_trial": True,
                    "condition": "C-only",
                    "round_id": 1,
                    "task_id": "build-pmars",
                    "trial_id": assignment["trial_id"],
                    "session_id": "continuation-session-r1",
                    "historical_baseline_used": False,
                },
            }
            stage_payload = {
                "trial": trial,
                "trace": trace,
                "proxy_attempt_records": [],
                "official_process": {
                    "classification": "completed",
                    "official_trial_boundary_started": True,
                    "harbor_returncode": 0,
                },
                "task_metadata": {},
            }
            stage_path = _driver_stage_path(input_path, "trial")
            write_json(
                stage_path,
                {
                    "schema_version": 1,
                    "kind": "r015_c_only_driver_stage",
                    "status": "complete",
                    "phase": "trial",
                    "condition": "C-only",
                    "round_id": 1,
                    "task_id": "build-pmars",
                    "trial_id": assignment["trial_id"],
                    "input_path": str(input_path),
                    "input_sha256": sha256_file(input_path),
                    "payload_sha256": sha256_text(canonical_json(stage_payload)),
                    "payload": stage_payload,
                },
            )
            write_json(
                _driver_process_path(input_path),
                {
                    "schema_version": 1,
                    "kind": "r015_c_only_driver_process",
                    "status": "failed",
                    "condition": "C-only",
                    "round_id": 1,
                    "task_id": "build-pmars",
                    "trial_id": assignment["trial_id"],
                    "input_path": str(input_path),
                    "input_sha256": sha256_file(input_path),
                    "returncode": 1,
                    "timed_out": False,
                    "error": "manager phase was interrupted after Harbor completed",
                },
            )
            driver = root / "controlled-driver.py"
            invocation_log = root / "invocations.log"
            driver.write_text(
                """import hashlib, json, sys
from pathlib import Path
args = sys.argv
input_path = Path(args[args.index('--input') + 1])
output_path = Path(args[args.index('--output') + 1])
value = json.loads(input_path.read_text(encoding='utf-8'))
assignment = value['assignment']
continuation = '--continue-from-trial' in args
with Path(r'''__LOG__''').open('a', encoding='utf-8') as stream:
    stream.write(('continue' if continuation else 'normal') + '\\n')
task_id = assignment['task_id']
round_id = assignment['round_id']
session_id = f'controlled-r{round_id}'
trajectory_path = output_path.parent / 'trajectory.json'
trajectory_value = {'r015_binding': {'round_id': round_id, 'task_id': task_id, 'trial_id': assignment['trial_id'], 'session_id': session_id}, 'source': {'canonical_instance_id': task_id}}
trajectory_path.write_text(json.dumps(trajectory_value, sort_keys=True), encoding='utf-8')
trajectory = {'round_id': round_id, 'task_id': task_id, 'trial_id': assignment['trial_id'], 'session_id': session_id, 'complete': True, 'path': str(trajectory_path), 'sha256': hashlib.sha256(trajectory_path.read_bytes()).hexdigest()}
raw = {'official_harbor_trial': True, 'condition': 'C-only', 'round_id': round_id, 'task_id': task_id, 'trial_id': assignment['trial_id'], 'session_id': session_id, 'historical_baseline_used': False}
trial = {'condition': 'C-only', 'round_id': round_id, 'task_id': task_id, 'trial_id': assignment['trial_id'], 'outcome': 'completed', 'trajectory': trajectory, 'supplied_skills': [], 'raw_evidence': raw}
extraction = {'condition': 'C-only', 'round_id': round_id, 'task_id': task_id, 'decision': 'skip', 'reason': 'continuation fixture has no manager candidate', 'candidates': [], 'trajectory_ref': trajectory, 'description_records': [], 'evidence': {'kind': 'controlled-continuation'}}
publication = {'condition': 'C-only', 'round_id': round_id, 'task_id': task_id, 'operations': [], 'manager_decisions': []}
output = {'schema_version': 1, 'kind': 'r015_c_only_trial_driver_output', 'condition': 'C-only', 'round_id': round_id, 'task_id': task_id, 'trial': trial, 'event_attempts': [], 'extraction': extraction, 'publication': publication, 'official_process': {'classification': 'controlled', 'official_trial_boundary_started': True}, 'historical_baseline_used': False, 'evidence_mode': 'controlled_fixture'}
output_path.write_text(json.dumps(output, sort_keys=True), encoding='utf-8')
""".replace("__LOG__", str(invocation_log).replace("\\", "\\\\")),
                encoding="utf-8",
            )
            _run_driver(protocol, driver, run_dir, state_path, allow_test_fixture=True)
            self.assertEqual(invocation_log.read_text(encoding="utf-8").splitlines(), ["continue", "normal"])
            restored = COnlyProtocol.load(state_path, fixture_config, fixture_baseline)
            self.assertEqual(restored.state["formal_campaign"], "complete")
            self.assertEqual(restored.current_round_id, 2)

    def test_builtin_continuation_reuses_trial_trajectory_and_runs_manager_phases(self) -> None:
        """The official driver's continuation path must not rerun Harbor."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            run_dir.mkdir()
            state_path = run_dir / "state.json"
            fixture_config = root / "config.json"
            fixture_baseline = root / "baseline.json"
            config_value = json.loads(CONFIG.read_text(encoding="utf-8"))
            baseline_value = json.loads(BASELINE.read_text(encoding="utf-8"))
            config_value["tasks"] = deepcopy(config_value["tasks"][:1])
            config_value["tasks"][0]["order"] = 1
            baseline_value["tasks"] = deepcopy(baseline_value["tasks"][:1])
            baseline_value["tasks"][0]["order"] = 1
            config_value["baseline_manifest"] = {
                "path": str(fixture_baseline),
                "sha256": "0" * 64,
                "comparison_only": True,
                "solver_input_imported": False,
                "skills_imported": False,
                "trajectories_imported": False,
            }
            write_json(fixture_baseline, baseline_value)
            config_value["baseline_manifest"]["sha256"] = sha256_file(fixture_baseline)
            write_json(fixture_config, config_value)
            protocol = COnlyProtocol.initialize(fixture_config, fixture_baseline, state_path)
            protocol.authorize_start()
            protocol.save(state_path)
            assignment = protocol.freeze_task("build-pmars")
            protocol.save(state_path)
            input_path = _write_driver_input(protocol, assignment, run_dir, state_path)
            output_path = input_path.with_name("driver-output.json")
            task_root = root / "tasks"
            task_dir = task_root / "build-pmars"
            task_dir.mkdir(parents=True)
            task_toml = task_dir / "task.toml"
            task_toml.write_text("[metadata]\nname = 'controlled continuation'\n", encoding="utf-8")
            ledger_path = run_dir / "manager-ledger.json"
            write_json(ledger_path, {"schema_version": 2, "limit": "unlimited", "calls": []})
            session_id = _session_id(str(assignment["trial_id"]))
            trajectory_path = task_dir / "completed-trial-trajectory.json"
            trace = {
                "source": {
                    "canonical_instance_id": "build-pmars",
                    "instance_id": "build-pmars",
                    "task_name": "terminal-bench/build-pmars",
                },
                "instruction": "Repair the controlled build-pmars task.",
                "steps": [
                    {
                        "source_entry_id": "observe-1",
                        "role": "toolResult",
                        "content": "controlled observable failure",
                    }
                ],
                "outcome": {"reward": 1.0, "status": "completed"},
                "text_manager_eligible": True,
                "historical": False,
                "source_kind": "controlled_current_c_only",
                "r015_binding": {
                    "round_id": 1,
                    "task_id": "build-pmars",
                    "trial_id": assignment["trial_id"],
                    "session_id": session_id,
                },
            }
            write_json(trajectory_path, trace)
            trajectory_ref = {
                "round_id": 1,
                "task_id": "build-pmars",
                "trial_id": assignment["trial_id"],
                "session_id": session_id,
                "complete": True,
                "path": str(trajectory_path),
                "sha256": sha256_file(trajectory_path),
            }
            metadata = {
                "task_name": "terminal-bench/build-pmars",
                "task_path": str(task_dir),
                "task_checkout_commit": "controlled-public-checkout",
                "task_toml": {
                    "path": str(task_toml),
                    "sha256": sha256_file(task_toml),
                    "size_bytes": task_toml.stat().st_size,
                },
                "public_environment": {
                    "docker_image": "controlled/build-pmars:test",
                    "docker_image_identity": "sha256:" + "2" * 64,
                    "agent_timeout_sec": 30,
                    "verifier_timeout_sec": 30,
                    "build_timeout_sec": 30,
                },
            }
            paths = {
                "task_root": task_root,
                "harbor": "controlled-harbor",
                "python": Path(os.sys.executable),
                "plugin": ROOT / "openclaw_plugin",
                "sidecar": ROOT / "scripts" / "run_openclaw_r012_sidecar.py",
                "manager_ledger": ledger_path,
                "manager_config": None,
            }
            stage_payload = {
                "trial": {
                    "condition": "C-only",
                    "round_id": 1,
                    "task_id": "build-pmars",
                    "trial_id": assignment["trial_id"],
                    "outcome": "completed",
                    "trajectory": trajectory_ref,
                    "supplied_skills": [],
                    "raw_evidence": {
                        "official_harbor_trial": True,
                        "evidence_mode": "official_live",
                        "official_trial_boundary_started": True,
                        "condition": "C-only",
                        "round_id": 1,
                        "task_id": "build-pmars",
                        "trial_id": assignment["trial_id"],
                        "session_id": session_id,
                        "historical_baseline_used": False,
                    },
                },
                "trace": trace,
                "proxy_attempt_records": [],
                "official_process": {
                    "classification": "completed",
                    "official_trial_boundary_started": True,
                    "harbor_returncode": 0,
                },
                "task_metadata": metadata,
            }
            stage_path = _driver_stage_path(input_path, "trial")
            write_json(
                stage_path,
                {
                    "schema_version": 1,
                    "kind": "r015_c_only_driver_stage",
                    "status": "complete",
                    "phase": "trial",
                    "condition": "C-only",
                    "round_id": 1,
                    "task_id": "build-pmars",
                    "trial_id": assignment["trial_id"],
                    "input_path": str(input_path),
                    "input_sha256": sha256_file(input_path),
                    "payload_sha256": sha256_text(canonical_json(stage_payload)),
                    "payload": stage_payload,
                },
            )
            _driver_process_path(input_path).write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "r015_c_only_driver_process",
                        "status": "failed",
                        "condition": "C-only",
                        "round_id": 1,
                        "task_id": "build-pmars",
                        "trial_id": assignment["trial_id"],
                        "input_path": str(input_path),
                        "input_sha256": sha256_file(input_path),
                        "returncode": 1,
                        "timed_out": False,
                        "error": "manager phase was interrupted after Harbor completed",
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            def paths_and_metadata(input_value: dict[str, object]):
                del input_value
                return paths, metadata, task_dir

            call_counter = {"value": 0}
            phases: list[str] = []

            def controlled_manager_context(context: dict[str, object], *, output_path: Path):
                del output_path
                executor = R012EvolutionMaintenanceExecutor(
                    manager=_controlled_manager(run_dir, ledger_path),
                    encoder=_ControlledEncoder(),
                    journal_root=run_dir / "manager-journals" / str(context["task_id"]),
                    instance_id=str(context["task_id"]),
                    profile=deepcopy(context["profile"]),
                    selections={
                        str(context["trial_id"]): {
                            "trial_id": str(context["trial_id"]),
                            "instance_id": str(context["task_id"]),
                            "arm": "C",
                            "action": "evaluate_all_supplied",
                            "reason": "controlled continuation",
                        }
                    },
                    evolution_prompt="controlled evolution prompt",
                    maintenance_prompt="controlled maintenance prompt",
                )
                return object(), executor, {
                    "manager_ledger": ledger_path,
                    "manager_ledger_before_calls": len(json.loads(ledger_path.read_text(encoding="utf-8"))["calls"]),
                }

            def controlled_manager_call(
                executor: R012EvolutionMaintenanceExecutor,
                *,
                trial_id: str,
                phase: str,
                purpose: str,
                messages: list[dict[str, str]],
                metadata: dict[str, object],
                trajectory_context=None, source_traces=None, messages_builder=None,
            ):
                if messages_builder is not None:
                    self.assertEqual(messages_builder(source_traces), messages)
                del messages
                call_counter["value"] += 1
                phases.append(phase)
                call_id = f"continuation-call-{call_counter['value']:04d}"
                response_path = run_dir / "manager" / "model_calls" / call_id / "response.json"
                response_path.parent.mkdir(parents=True, exist_ok=False)
                model_json: dict[str, object]
                if phase.startswith("event-"):
                    model_json = {"action": "skip", "reason": "controlled continuation event skip"}
                elif phase == "description":
                    model_json = {
                        "task_family": "controlled continuation",
                        "observed_obstacle": "the controlled task exposed a reproducible obstacle",
                        "attempted_procedure": "inspect and rerun bounded validation",
                        "observed_outcome": "the controlled task completed",
                        "source_step_ids": ["observe-1"],
                    }
                elif phase == "task-sop-candidate":
                    current = str(metadata["task_id"])
                    model_json = {
                        "action": "generate",
                        "skill": {
                            "title": f"Controlled continuation SOP for {current}",
                            "granularity": "general",
                            "when_to_apply": "When a bounded terminal repair needs validation.",
                            "rules": ["Inspect the failure, apply the bounded repair, and observe validation."],
                        },
                        "candidate_context": {
                            "task_goal": "Complete the controlled repair",
                            "whole_task_outcome": "completed",
                            "hard_constraints": [],
                            "environment_assumptions": [],
                            "observed_results": ["bounded validation completed"],
                            "known_limitations": [],
                        },
                        "evidence": {"rule_evidence": [{"rule_index": 0, "sources": [{"canonical_instance_id": current, "step_ids": ["action-1", "observe-1"]}]}]},
                    }
                else:
                    self.fail(f"unexpected continuation manager phase: {phase}")
                write_json(response_path, {"kind": "controlled_continuation_response", "purpose": purpose, "metadata": metadata})
                journal_path = executor._journal_path(trial_id, phase)
                journal_path.parent.mkdir(parents=True, exist_ok=True)
                write_json(
                    journal_path,
                    {
                        "schema_version": 1,
                        "kind": "r012_pre_call_journal",
                        "trial_id": trial_id,
                        "phase": phase,
                        "status": "prepared_before_manager_call",
                        "purpose": purpose,
                        "call_metadata": deepcopy(metadata),
                    },
                )
                ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                ledger["calls"].append({"call_id": call_id, "purpose": purpose, "status": "succeeded"})
                write_json(ledger_path, ledger)
                response_ref = {"call_id": call_id, "path": str(response_path), "sha256": sha256_file(response_path)}
                return {"call_id": call_id, "json": model_json}, {"path": str(journal_path), "sha256": sha256_file(journal_path), "response": response_ref}, None

            with patch(
                "scripts.run_r015_c_only_harbor_driver._task_root_and_service",
                side_effect=paths_and_metadata,
            ), patch(
                "scripts.run_r015_c_only_harbor_driver._manager_context",
                side_effect=controlled_manager_context,
            ), patch(
                "scripts.run_r015_c_only_harbor_driver._manager_call",
                side_effect=controlled_manager_call,
            ), patch(
                "scripts.run_r015_c_only_harbor_driver.TaskChatBoundary",
                _ControlledTaskChat,
            ):
                output = _continue_from_completed_trial(
                    json.loads(input_path.read_text(encoding="utf-8")),
                    input_path,
                    output_path,
                )
            self.assertEqual(phases, [])
            self.assertEqual(output["extraction"]["evidence"]["event"]["kind"], "r015_event_graph_v3")
            self.assertEqual(output["extraction"]["evidence"]["event"]["event_results"][0]["status"], "skip")
            graph = output["extraction"]["evidence"]["task"]["graph"]
            self.assertEqual(graph["kind"], "r015_task_graph_v1")
            self.assertEqual(graph["outcomes"]["generate"]["status"], "skip")
            self.assertEqual(output["trial"]["trajectory"], trajectory_ref)
            self.assertTrue(output_path.is_file())
            self.assertTrue(_driver_stage_path(input_path, "extraction").is_file())
            self.assertTrue(_driver_stage_path(input_path, "publication").is_file())
            self.assertFalse((run_dir / "official-harbor" / "harbor-process.json").exists())
            snapshot = output["publication"]["evidence"]["manager_ledger"]
            self.assertEqual(snapshot["sha256"], sha256_file(Path(snapshot["path"])))
            self.assertEqual(snapshot["manager_calls_before_trial"], 0)


if __name__ == "__main__":
    unittest.main()
