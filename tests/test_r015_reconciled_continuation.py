from __future__ import annotations

import json
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sys
import threading
import tempfile
import textwrap
import unittest

from unittest.mock import patch

from codeskill_rebuild.c_only_protocol import COnlyProtocol
from codeskill_rebuild.manager import ManagerClient, ManagerProfile, ServerMessageTokenCounter
from codeskill_rebuild.manager_reconciliation import ManagerReconciliationError, audit_saved_manager_call, load_reconciliation_manifest
from codeskill_rebuild.r012_execution import R012EvolutionMaintenanceExecutor
from codeskill_rebuild.types import canonical_json, sha256_file, sha256_text, write_json
from scripts import run_r015_c_only as coordinator
from scripts.run_r015_c_only import _apply_driver_output, _driver_command, _driver_process_path, _driver_stage_path, _trial_stage_is_safe_continuation, _write_driver_input
from scripts.run_r015_c_only_harbor_driver import (
    COnlyHarborDriverError,
    _prompt_path,
    _session_id,
    _continue_from_completed_trial as _official_continuation,
)
from scripts.legacy.run_r015_c_only_harbor_driver import _continue_from_completed_trial


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "r015-c-only-coding.json"
BASELINE = ROOT / "docs" / "baselines" / "r015-legacy-coding-baseline-20260913.json"


def test_reconciled_call_routes_to_explicit_legacy_entrypoint(tmp_path):
    official = ROOT / "scripts" / "run_r015_c_only_harbor_driver.py"
    command = _driver_command(official, tmp_path / "input.json", tmp_path / "output.json",
                              continue_from_trial=True,
                              reconciliation_manifest_path=tmp_path / "reconcile.json")
    assert Path(command[1]) == ROOT / "scripts" / "legacy" / "run_r015_c_only_harbor_driver.py"
    with unittest.TestCase().assertRaisesRegex(COnlyHarborDriverError, "pre-Graph"):
        _official_continuation({}, tmp_path / "input.json", tmp_path / "output.json",
                               reconciliation_manifest_path=tmp_path / "reconcile.json")


class _ControlledEncoder:
    repo_id = "controlled-minilm"
    resolved_revision = "controlled-revision"

    def index_description(self, value: dict[str, object]) -> tuple[list[float], dict[str, object]]:
        return [1.0, 0.0], {"kind": "controlled-description-index", "task_family": value["task_family"]}

    def index_skill(self, value: dict[str, object]) -> tuple[list[float], dict[str, object]]:
        return [1.0, 0.0], {"kind": "controlled-skill-index", "granularity": value["granularity"]}


class _AuditCounter:
    method = "controlled_complete_request_counter"

    def __init__(self, count: int = 1) -> None:
        self.count = count
        self.last_exchange: dict[str, object] | None = None

    def __call__(self, messages: list[dict[str, object]], *, request_options: dict[str, object] | None = None) -> int:
        options = dict(request_options or {})
        payload = {"messages": messages, "add_generation_prompt": True, **options}
        self.last_exchange = {
            "endpoint": "http://controlled.invalid/v1/tokenize",
            "request": payload,
            "http_status": 200,
            "raw_response": json.dumps({"count": self.count}),
            "parsed_response": {"count": self.count},
            "count": self.count,
        }
        return self.count


class _ManagerHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _json(self, value: dict[str, object]) -> None:
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        server = self.server
        if self.path.endswith("/tokenize"):
            server.tokenize_requests.append(payload)
            self._json({"count": 1})
            return
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        server.chat_requests.append(payload)
        index = len(server.chat_requests)
        if index == 1:
            model_json: dict[str, object] = {"action": "skip", "reason": "controlled next event is not reusable"}
        elif index == 2:
            model_json = {
                "task_family": "controlled diagnostic repair",
                "observed_obstacle": "the task exposed a reproducible obstacle",
                "attempted_procedure": "inspect the bounded failure and rerun validation",
                "observed_outcome": "the controlled task completed",
                "source_step_ids": ["observe-1"],
            }
        elif index == 3:
            model_json = {
                "action": "generate",
                "skill": {
                    "title": "Controlled continuation SOP",
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
                "evidence": {"rule_evidence": [{"rule_index": 0, "sources": [{"canonical_instance_id": "build-pmars", "step_ids": ["response-1", "outcome-1"]}]}]},
            }
        elif index == 4:
            model_json = {"action": "add", "reason": "the extracted event is useful and distinct"}
        else:
            raise AssertionError(f"unexpected controlled chat request {index}")
        content = json.dumps(model_json, ensure_ascii=False, separators=(",", ":"))
        self._json(
            {
                "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )


class ReconciledContinuationIntegrationTest(unittest.TestCase):
    """Exercise the paid-call reuse path through real driver and coordinator code."""

    def test_reconciled_continuation_reuses_call_and_applies_all_remaining_phases(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ManagerHandler)
        server.chat_requests = []
        server.tokenize_requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
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
                frozen_state_hash = json.loads(input_path.read_text(encoding="utf-8"))["state"]["sha256"]
                # Simulate the coordinator's save immediately before a
                # manager-only recovery.  The trial input/stage retain the
                # launch hash while the mutable state file advances.
                protocol.save(state_path)
                self.assertNotEqual(sha256_file(state_path), frozen_state_hash)
                task_dir = root / "tasks" / "build-pmars"
                task_dir.mkdir(parents=True)
                task_toml = task_dir / "task.toml"
                task_toml.write_text("[metadata]\nname = 'controlled public task'\n", encoding="utf-8")
                metadata = {
                    "task_name": "terminal-bench/build-pmars",
                    "task_path": str(task_dir),
                    "task_checkout_commit": "controlled-public-checkout",
                    "task_toml": {"path": str(task_toml), "sha256": sha256_file(task_toml), "size_bytes": task_toml.stat().st_size},
                    "public_environment": {
                        "docker_image": "controlled/build-pmars:test",
                        "docker_image_identity": "sha256:" + "a" * 64,
                        "agent_timeout_sec": 30,
                        "verifier_timeout_sec": 30,
                        "build_timeout_sec": 30,
                    },
                }
                paths = {
                    "task_root": task_dir.parent,
                    "harbor": "controlled-harbor",
                    "python": Path(os.sys.executable),
                    "plugin": ROOT / "openclaw_plugin",
                    "sidecar": ROOT / "scripts" / "run_openclaw_r012_sidecar.py",
                    "manager_ledger": run_dir / "manager-ledger.json",
                    "manager_config": None,
                }

                trial_id = str(assignment["trial_id"])
                task_id = str(assignment["task_id"])
                session_id = _session_id(trial_id)
                artifact_root = input_path.parent / "official-harbor"
                artifact_root.mkdir(parents=True)
                session_path = artifact_root / "session.jsonl"
                session_path.write_text(json.dumps({"session_id": session_id}) + "\n", encoding="utf-8")
                trace_path = artifact_root / "trajectory-live.json"
                trace = {
                    "source": {
                        "canonical_instance_id": task_id,
                        "instance_id": task_id,
                        "task_name": f"terminal-bench/{task_id}",
                        "session_id": session_id,
                        "session_path": str(session_path),
                        "session_sha256": sha256_file(session_path),
                    },
                    "instruction": "Repair the controlled build-pmars task.",
                    "steps": [
                        {"source_entry_id": "initial", "role": "user", "content": "task request"},
                        {"source_entry_id": "observe-1", "role": "toolResult", "content": "observed local failure"},
                        {"source_entry_id": "response-1", "role": "assistant", "content": "performed a bounded repair"},
                        {"source_entry_id": "outcome-1", "role": "toolResult", "content": "repair completed"},
                    ],
                    "outcome": {"reward": 1.0, "status": "completed"},
                    "text_manager_eligible": True,
                    "historical": False,
                    "source_kind": "controlled_current_c_only",
                    "r015_binding": {"round_id": 1, "task_id": task_id, "trial_id": trial_id, "session_id": session_id},
                }
                write_json(trace_path, trace)
                trajectory_ref = {"round_id": 1, "task_id": task_id, "trial_id": trial_id, "session_id": session_id, "complete": True, "path": str(trace_path), "sha256": sha256_file(trace_path)}
                trial = {
                    "condition": "C-only",
                    "round_id": 1,
                    "task_id": task_id,
                    "trial_id": trial_id,
                    "outcome": "completed",
                    "trajectory": trajectory_ref,
                    "supplied_skills": [],
                    "raw_evidence": {
                        "official_harbor_trial": True,
                        "evidence_mode": "official_live",
                        "official_trial_boundary_started": True,
                        "condition": "C-only",
                        "round_id": 1,
                        "task_id": task_id,
                        "trial_id": trial_id,
                        "session_id": session_id,
                        "historical_baseline_used": False,
                        "controlled_transport": True,
                    },
                }
                stage_payload = {
                    "trial": trial,
                    "trace": trace,
                    "proxy_attempt_records": [],
                    "official_process": {"classification": "completed", "official_trial_boundary_started": True, "harbor_returncode": 0},
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
                        "task_id": task_id,
                        "trial_id": trial_id,
                        "session_id": session_id,
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
                        "task_id": task_id,
                        "trial_id": trial_id,
                        "input_path": str(input_path),
                        "input_sha256": sha256_file(input_path),
                        "output_path": str(output_path),
                        "returncode": 1,
                        "timed_out": False,
                        "error": "manager phase interrupted after the official trial boundary",
                    },
                )

                manager_root = run_dir / "manager"
                call_dir = manager_root / "model_calls" / "call-0001"
                call_dir.mkdir(parents=True)
                purpose = f"r015_c_only_event_extraction:{trial_id}:001"
                event_messages = [
                    {"role": "system", "content": _prompt_path("custom/r012_fig07_event_extraction_evidence.md").read_text(encoding="utf-8")},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "task_context": trace["instruction"],
                                "source": trace["source"],
                                "full_trajectory": trace["steps"],
                                "outcome": trace["outcome"],
                                "previous_event_candidate_ids": [],
                                "previous_event_candidates": [],
                            },
                            ensure_ascii=False,
                        ),
                    },
                ]
                options = {
                    "model": "deepseek-ai/DeepSeek-V4-Flash",
                    "temperature": 0.0,
                    "max_tokens": 8192,
                    "response_format": {"type": "json_object"},
                    "reasoning_effort": "max",
                }
                event_model = {
                    "action": "generate",
                    "skill": {
                        "title": "Recover a controlled tool failure",
                        "granularity": "event-driven",
                        "when_to_apply": "When a local tool reports the same bounded failure.",
                        "rules": ["Inspect the local observation before retrying the bounded repair."],
                    },
                    "evidence": {
                        "trigger_step_ids": ["observe-1"],
                        "response_step_ids": ["response-1"],
                        "outcome_step_ids": ["outcome-1"],
                        "rule_evidence": [{"rule_index": 0, "step_ids": ["observe-1", "response-1", "outcome-1"]}],
                    },
                }
                request_path = call_dir / "request.json"
                write_json(
                    request_path,
                    {
                        "kind": "live_manager_call",
                        "purpose": purpose,
                        "historical": False,
                        "fixture": False,
                        "call_metadata": {"attempt_no": 1, "condition": "C-only", "task_id": task_id},
                        "request": {"messages": event_messages, **options},
                    },
                )
                response_path = call_dir / "response.json"
                write_json(
                    response_path,
                    {
                        "kind": "live_manager_call",
                        "purpose": purpose,
                        "http_status": 200,
                        "classification": "tokenizer_prompt_count_mismatch",
                        "finish_reason": "stop",
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                        "parsed_response": {
                            "choices": [{"message": {"content": json.dumps(event_model, ensure_ascii=False, separators=(",", ":"))}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                        },
                    },
                )
                journal_path = run_dir / "manager-journals" / task_id / sha256_text(trial_id)[:20] / "event-001.json"
                write_json(
                    journal_path,
                    {
                        "schema_version": 1,
                        "kind": "r012_pre_call_journal",
                        "trial_id": trial_id,
                        "phase": "event-001",
                        "purpose": purpose,
                        "status": "manager_call_raised",
                        "messages": event_messages,
                        "messages_sha256": sha256_text(canonical_json(event_messages)),
                    },
                )
                ledger_path = run_dir / "manager-ledger.json"
                write_json(
                    ledger_path,
                    {
                        "schema_version": 2,
                        "limit": "unlimited",
                        "calls": [
                            {
                                "run_dir": str(manager_root),
                                "call_id": "call-0001",
                                "purpose": purpose,
                                "status": "tokenizer_mismatch",
                                "http_status": 200,
                                "finish_reason": "stop",
                                "classification": "tokenizer_prompt_count_mismatch",
                                "response_path": str(response_path),
                            }
                        ],
                    },
                )
                reconciliation_path = run_dir / "manager-reconciliation-call-0001.json"
                audit_saved_manager_call(
                    request_path=request_path,
                    response_path=response_path,
                    journal_path=journal_path,
                    ledger_path=ledger_path,
                    trace_path=trace_path,
                    expected={
                        "call_id": "call-0001",
                        "phase": "event-001",
                        "purpose": purpose,
                        "round_id": 1,
                        "task_id": task_id,
                        "trial_id": trial_id,
                        "session_id": session_id,
                    },
                    tokenizer=_AuditCounter(1),
                    output_path=reconciliation_path,
                    tokenizer_exchange_path=run_dir / "manager-reconciliation-tokenizer.json",
                    driver_stage_ref={"path": str(stage_path), "sha256": sha256_file(stage_path)},
                    reconciliation_run_dir=run_dir,
                )
                reconciliation = load_reconciliation_manifest(reconciliation_path)

                # The explicit reconciliation boundary must reject identity
                # relabelling and an in-flight original process before it can
                # reach any manager or Harbor code.
                original_process_bytes = _driver_process_path(input_path).read_bytes()
                process_value = json.loads(original_process_bytes.decode("utf-8"))
                process_value["status"] = "running"
                write_json(_driver_process_path(input_path), process_value)
                self.assertFalse(
                    _trial_stage_is_safe_continuation(
                        input_path=input_path,
                        output_path=output_path,
                        assignment=assignment,
                        allow_reconciled_manager_journal=True,
                    )
                )
                _driver_process_path(input_path).write_bytes(original_process_bytes)
                for field, wrong_value in (("session_id", "wrong-session"), ("task_id", "wrong-task")):
                    wrong_manifest_value = deepcopy(reconciliation)
                    wrong_manifest_value["trial"][field] = wrong_value
                    wrong_manifest_path = run_dir / f"wrong-{field}.json"
                    write_json(wrong_manifest_path, wrong_manifest_value)
                    with self.assertRaises(COnlyHarborDriverError):
                        _continue_from_completed_trial(
                            json.loads(input_path.read_text(encoding="utf-8")),
                            input_path,
                            run_dir / f"wrong-{field}-output.json",
                            reconciliation_manifest_path=wrong_manifest_path,
                        )
                wrong_source = deepcopy(reconciliation)
                wrong_source["original"]["trajectory"]["sha256"] = "0" * 64
                wrong_source_path = run_dir / "wrong-source.json"
                write_json(wrong_source_path, wrong_source)
                with self.assertRaises(ManagerReconciliationError):
                    load_reconciliation_manifest(wrong_source_path)

                base_url = f"http://127.0.0.1:{server.server_port}/v1"

                def controlled_manager_context(context: dict[str, object], *, output_path: Path):
                    del output_path
                    manager = ManagerClient(
                        ManagerProfile(
                            base_url=base_url,
                            model="deepseek-ai/DeepSeek-V4-Flash",
                            timeout_seconds=30,
                            max_output_tokens=8192,
                            manager_context_tokens=270000,
                            safety_tokens=4096,
                            temperature=0.0,
                            reasoning_effort="max",
                            max_total_calls=None,
                        ),
                        manager_root,
                        {},
                        ledger_path,
                        exact_token_counter=ServerMessageTokenCounter(base_url),
                    )
                    executor = R012EvolutionMaintenanceExecutor(
                        manager=manager,
                        encoder=_ControlledEncoder(),
                        journal_root=run_dir / "manager-journals" / task_id,
                        instance_id=task_id,
                        profile=deepcopy(context["profile"]),
                        selections={
                            trial_id: {
                                "trial_id": trial_id,
                                "instance_id": task_id,
                                "arm": "C",
                                "action": "evaluate_all_supplied",
                                "reason": "controlled reconciliation continuation",
                            }
                        },
                        evolution_prompt=_prompt_path("paper/fig08_evolution.md").read_text(encoding="utf-8"),
                        maintenance_prompt=_prompt_path("paper/fig09_maintenance.md").read_text(encoding="utf-8"),
                    )
                    return manager, executor, {
                        "manager_ledger": ledger_path,
                        "manager_ledger_before_calls": len(json.loads(ledger_path.read_text(encoding="utf-8"))["calls"]),
                    }

                with patch(
                    "scripts.run_r015_c_only_harbor_driver._task_root_and_service",
                    return_value=(paths, metadata, task_dir),
                ), patch(
                    "scripts.run_r015_c_only_harbor_driver._manager_context",
                    side_effect=controlled_manager_context,
                ):
                    output = _continue_from_completed_trial(
                        json.loads(input_path.read_text(encoding="utf-8")),
                        input_path,
                        output_path,
                        reconciliation_manifest_path=reconciliation_path,
                    )

                self.assertEqual(len(server.chat_requests), 4)
                self.assertGreaterEqual(len(server.tokenize_requests), 4)
                self.assertEqual(
                    server.chat_requests[0]["messages"][0]["content"],
                    _prompt_path("custom/r015_fig07_event_extraction_with_code_examples.md").read_text(encoding="utf-8"),
                )
                self.assertNotEqual(
                    server.chat_requests[0]["messages"][0]["content"],
                    _prompt_path("custom/r012_fig07_event_extraction_evidence.md").read_text(encoding="utf-8"),
                )
                call_dirs = sorted(path for path in (manager_root / "model_calls").glob("call-*") if path.is_dir())
                self.assertEqual([path.name for path in call_dirs], ["call-0001", "call-0002", "call-0003"])
                self.assertEqual(len(list((manager_root / "task-calls").glob("*/wire-response.json"))), 2)
                self.assertEqual(output["event_attempts"][0]["outcome"], "generated")
                self.assertEqual(output["event_attempts"][1]["outcome"], "skip")
                self.assertEqual(output["publication"]["operations"][0]["decision"], "add")
                self.assertTrue(_driver_stage_path(input_path, "extraction").is_file())
                self.assertTrue(_driver_stage_path(input_path, "publication").is_file())
                self.assertFalse((run_dir / "official-harbor" / "harbor-process.json").exists())
                reconciliation_journal = run_dir / "manager-journals-reconciliation" / task_id / sha256_text(trial_id)[:20] / "event-001.json"
                self.assertEqual(json.loads(reconciliation_journal.read_text(encoding="utf-8"))["status"], "event_generated")
                for phase in ("event-002", "fig9-extraction-001"):
                    journal = run_dir / "manager-journals" / task_id / sha256_text(trial_id)[:20] / f"{phase}.json"
                    expected_status = {
                        "event-002": "event_skip",
                        "fig9-extraction-001": "fig9_applied_to_driver_staged_bank",
                    }[phase]
                    self.assertEqual(json.loads(journal.read_text(encoding="utf-8"))["status"], expected_status)

                _apply_driver_output(protocol, output, assignment, state_path, manager_root=manager_root)
                restored = COnlyProtocol.load(state_path, fixture_config, fixture_baseline)
                self.assertIn(task_id, restored.state["rounds"]["1"]["completed_tasks"])
                self.assertTrue(restored._assignment(task_id).get("publication"))
                self.assertEqual(len(json.loads(ledger_path.read_text(encoding="utf-8"))["calls"]), 3)
                loaded_after_continuation = load_reconciliation_manifest(reconciliation_path)
                self.assertEqual(loaded_after_continuation["original"]["preserved_ledger_entry"]["call_id"], "call-0001")
                self.assertEqual(loaded_after_continuation["resume"]["reuse_existing_call_id"], "call-0001")
                saved_response_bytes = response_path.read_bytes()
                response_path.write_bytes(saved_response_bytes + b"\n")
                with self.assertRaises(ManagerReconciliationError):
                    load_reconciliation_manifest(reconciliation_path)
                response_path.write_bytes(saved_response_bytes)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_coordinator_resume_uses_split_execution_root_and_advances_next_task_once(self) -> None:
        """Run the real coordinator/driver subprocess boundary on a split layout.

        The first task has an immutable completed Harbor stage and an audited
        event-001 response.  The child driver therefore enters the explicit
        reconciliation path, reuses call-0001, and performs only the later
        controlled manager calls.  The remaining trials use the real driver
        code with Harbor and task evidence transports replaced by a
        model-free controlled transport.  This keeps state saves, input
        binding, journals, publication, and ordered advancement under the
        production coordinator while proving that ``state.json`` and the
        attempt manager root are not conflated.
        """
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ManagerHandler)
        server.chat_requests = []
        server.tokenize_requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                logical_root = root / "logical-campaign"
                run_dir = logical_root / "attempts" / "attempt-003"
                run_dir.mkdir(parents=True)
                state_path = logical_root / "state.json"
                fixture_config = root / "config.json"
                fixture_baseline = root / "baseline.json"
                config_value = json.loads(CONFIG.read_text(encoding="utf-8"))
                baseline_value = json.loads(BASELINE.read_text(encoding="utf-8"))
                config_value["tasks"] = deepcopy(config_value["tasks"][:2])
                baseline_value["tasks"] = deepcopy(baseline_value["tasks"][:2])
                for index, item in enumerate(config_value["tasks"], start=1):
                    item["order"] = index
                    baseline_value["tasks"][index - 1]["order"] = index
                config_value["baseline_manifest"] = {
                    "path": str(fixture_baseline),
                    "sha256": "0" * 64,
                    "comparison_only": True,
                    "solver_input_imported": False,
                    "skills_imported": False,
                    "trajectories_imported": False,
                }
                config_value["runtime_alignment"]["baseline_observed"]["endpoint"] = f"http://127.0.0.1:{server.server_port}/v1"
                config_value["driver"].update(
                    {
                        "task_root": str(root / "tasks"),
                        "python_executable": str(Path(sys.executable).absolute()),
                        "harbor_executable": "controlled-harbor",
                        "plugin_path": str((ROOT / "openclaw_plugin").resolve()),
                        "sidecar_script": str((ROOT / "scripts" / "run_openclaw_r012_sidecar.py").resolve()),
                        "manager_ledger": "run-dir/manager-ledger.json",
                    }
                )
                write_json(fixture_baseline, baseline_value)
                config_value["baseline_manifest"]["sha256"] = sha256_file(fixture_baseline)
                write_json(fixture_config, config_value)

                protocol = COnlyProtocol.initialize(fixture_config, fixture_baseline, state_path)
                protocol.authorize_start()
                protocol.save(state_path)
                assignment = protocol.freeze_task("build-pmars")
                protocol.save(state_path)
                input_path = coordinator._write_driver_input(protocol, assignment, run_dir, state_path)
                output_path = input_path.with_name("driver-output.json")
                protocol.save(state_path)
                input_value = json.loads(input_path.read_text(encoding="utf-8"))
                self.assertEqual(input_value["round_material"]["trajectory_pool"], [])
                self.assertEqual(input_value["round_material"]["description_pool"], [])

                task_root = root / "tasks"
                metadata_by_task: dict[str, dict[str, object]] = {}
                task_dirs: dict[str, Path] = {}
                for order, task_id in enumerate(("build-pmars", "cancel-async-tasks"), start=1):
                    task_dir = task_root / task_id
                    task_dir.mkdir(parents=True)
                    task_toml = task_dir / "task.toml"
                    task_toml.write_text(
                        "\n".join(
                            [
                                "[task]",
                                f"name = 'terminal-bench/{task_id}'",
                                "[environment]",
                                f"docker_image = 'controlled/{task_id}:test'",
                                "build_timeout_sec = 30",
                                "[agent]",
                                "timeout_sec = 30",
                                "[verifier]",
                                "timeout_sec = 30",
                            ]
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    image_id = "sha256:" + (str(order) * 64)[:64]
                    metadata_by_task[task_id] = {
                        "task_name": f"terminal-bench/{task_id}",
                        "task_path": str(task_dir),
                        "task_checkout_commit": "controlled-public-checkout",
                        "task_toml": {
                            "path": str(task_toml),
                            "exists": True,
                            "sha256": sha256_file(task_toml),
                            "size_bytes": task_toml.stat().st_size,
                        },
                        "public_environment": {
                            "docker_image": f"controlled/{task_id}:test",
                            "docker_image_identity": {
                                "status": "observed",
                                "image_id": image_id,
                                "repo_digests": [],
                            },
                            "agent_timeout_sec": 30,
                            "verifier_timeout_sec": 30,
                            "build_timeout_sec": 30,
                        },
                        "hidden_material_read": False,
                    }
                    task_dirs[task_id] = task_dir

                task_id = str(assignment["task_id"])
                trial_id = str(assignment["trial_id"])
                session_id = _session_id(trial_id)
                artifact_root = input_path.parent / "official-harbor"
                artifact_root.mkdir(parents=True)
                session_path = artifact_root / "session.jsonl"
                session_path.write_text(json.dumps({"session_id": session_id}) + "\n", encoding="utf-8")
                trace_path = artifact_root / "trajectory-live.json"
                trace = {
                    "source": {
                        "canonical_instance_id": task_id,
                        "instance_id": task_id,
                        "task_name": f"terminal-bench/{task_id}",
                        "session_id": session_id,
                        "session_path": str(session_path),
                        "session_sha256": sha256_file(session_path),
                    },
                    "instruction": "Repair the controlled build-pmars task.",
                    "steps": [
                        {"source_entry_id": "initial", "role": "user", "content": "task request"},
                        {"source_entry_id": "observe-1", "role": "toolResult", "content": "observed local failure"},
                        {"source_entry_id": "response-1", "role": "assistant", "content": "performed a bounded repair"},
                        {"source_entry_id": "outcome-1", "role": "toolResult", "content": "repair completed"},
                    ],
                    "outcome": {"reward": 1.0, "status": "completed"},
                    "text_manager_eligible": True,
                    "historical": False,
                    "source_kind": "controlled_current_c_only",
                    "r015_binding": {"round_id": 1, "task_id": task_id, "trial_id": trial_id, "session_id": session_id},
                }
                write_json(trace_path, trace)
                trajectory_ref = {
                    "round_id": 1,
                    "task_id": task_id,
                    "trial_id": trial_id,
                    "session_id": session_id,
                    "complete": True,
                    "path": str(trace_path),
                    "sha256": sha256_file(trace_path),
                }
                trial = {
                    "condition": "C-only",
                    "round_id": 1,
                    "task_id": task_id,
                    "trial_id": trial_id,
                    "outcome": "completed",
                    "trajectory": trajectory_ref,
                    "supplied_skills": [],
                    "raw_evidence": {
                        "official_harbor_trial": True,
                        "evidence_mode": "official_live",
                        "official_trial_boundary_started": True,
                        "condition": "C-only",
                        "round_id": 1,
                        "task_id": task_id,
                        "trial_id": trial_id,
                        "session_id": session_id,
                        "historical_baseline_used": False,
                        "controlled_transport": True,
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
                        "controlled_transport": True,
                    },
                    "task_metadata": metadata_by_task[task_id],
                }
                stage_path = coordinator._driver_stage_path(input_path, "trial")
                write_json(
                    stage_path,
                    {
                        "schema_version": 1,
                        "kind": "r015_c_only_driver_stage",
                        "status": "complete",
                        "phase": "trial",
                        "condition": "C-only",
                        "round_id": 1,
                        "task_id": task_id,
                        "trial_id": trial_id,
                        "session_id": session_id,
                        "input_path": str(input_path),
                        "input_sha256": sha256_file(input_path),
                        "payload_sha256": sha256_text(canonical_json(stage_payload)),
                        "payload": stage_payload,
                    },
                )
                write_json(
                    coordinator._driver_process_path(input_path),
                    {
                        "schema_version": 1,
                        "kind": "r015_c_only_driver_process",
                        "status": "failed",
                        "condition": "C-only",
                        "round_id": 1,
                        "task_id": task_id,
                        "trial_id": trial_id,
                        "input_path": str(input_path),
                        "input_sha256": sha256_file(input_path),
                        "output_path": str(output_path),
                        "returncode": 1,
                        "timed_out": False,
                        "error": "manager phase interrupted after the official trial boundary",
                    },
                )

                manager_root = run_dir / "manager"
                call_dir = manager_root / "model_calls" / "call-0001"
                call_dir.mkdir(parents=True)
                purpose = f"r015_c_only_event_extraction:{trial_id}:001"
                event_messages = [
                    {"role": "system", "content": _prompt_path("custom/r012_fig07_event_extraction_evidence.md").read_text(encoding="utf-8")},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "task_context": trace["instruction"],
                                "source": trace["source"],
                                "full_trajectory": trace["steps"],
                                "outcome": trace["outcome"],
                                "previous_event_candidate_ids": [],
                                "previous_event_candidates": [],
                            },
                            ensure_ascii=False,
                        ),
                    },
                ]
                options = {
                    "model": "deepseek-ai/DeepSeek-V4-Flash",
                    "temperature": 0.0,
                    "max_tokens": 8192,
                    "response_format": {"type": "json_object"},
                    "reasoning_effort": "max",
                }
                event_model = {
                    "action": "generate",
                    "skill": {
                        "title": "Recover a controlled tool failure",
                        "granularity": "event-driven",
                        "when_to_apply": "When a local tool reports the same bounded failure.",
                        "rules": ["Inspect the local observation before retrying the bounded repair."],
                    },
                    "evidence": {
                        "trigger_step_ids": ["observe-1"],
                        "response_step_ids": ["response-1"],
                        "outcome_step_ids": ["outcome-1"],
                        "rule_evidence": [{"rule_index": 0, "step_ids": ["observe-1", "response-1", "outcome-1"]}],
                    },
                }
                request_path = call_dir / "request.json"
                write_json(
                    request_path,
                    {
                        "kind": "live_manager_call",
                        "purpose": purpose,
                        "historical": False,
                        "fixture": False,
                        "call_metadata": {"attempt_no": 1, "condition": "C-only", "task_id": task_id},
                        "request": {"messages": event_messages, **options},
                    },
                )
                response_path = call_dir / "response.json"
                write_json(
                    response_path,
                    {
                        "kind": "live_manager_call",
                        "purpose": purpose,
                        "http_status": 200,
                        "classification": "tokenizer_prompt_count_mismatch",
                        "finish_reason": "stop",
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                        "parsed_response": {
                            "choices": [{"message": {"content": json.dumps(event_model, ensure_ascii=False, separators=(",", ":"))}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                        },
                    },
                )
                journal_path = run_dir / "manager-journals" / task_id / sha256_text(trial_id)[:20] / "event-001.json"
                write_json(
                    journal_path,
                    {
                        "schema_version": 1,
                        "kind": "r012_pre_call_journal",
                        "trial_id": trial_id,
                        "phase": "event-001",
                        "purpose": purpose,
                        "status": "manager_call_raised",
                        "messages": event_messages,
                        "messages_sha256": sha256_text(canonical_json(event_messages)),
                    },
                )
                ledger_path = run_dir / "manager-ledger.json"
                write_json(
                    ledger_path,
                    {
                        "schema_version": 2,
                        "limit": "unlimited",
                        "calls": [
                            {
                                "run_dir": str(manager_root),
                                "call_id": "call-0001",
                                "purpose": purpose,
                                "status": "tokenizer_mismatch",
                                "http_status": 200,
                                "finish_reason": "stop",
                                "classification": "tokenizer_prompt_count_mismatch",
                                "response_path": str(response_path),
                            }
                        ],
                    },
                )
                reconciliation_path = run_dir / "manager-reconciliation-call-0001.json"
                audit_saved_manager_call(
                    request_path=request_path,
                    response_path=response_path,
                    journal_path=journal_path,
                    ledger_path=ledger_path,
                    trace_path=trace_path,
                    expected={
                        "call_id": "call-0001",
                        "phase": "event-001",
                        "purpose": purpose,
                        "round_id": 1,
                        "task_id": task_id,
                        "trial_id": trial_id,
                        "session_id": session_id,
                    },
                    tokenizer=_AuditCounter(1),
                    output_path=reconciliation_path,
                    tokenizer_exchange_path=run_dir / "manager-reconciliation-tokenizer.json",
                    driver_stage_ref={"path": str(stage_path), "sha256": sha256_file(stage_path)},
                    reconciliation_run_dir=run_dir,
                )
                original_reconciliation_hash = sha256_file(reconciliation_path)
                original_response_hash = sha256_file(response_path)

                metadata_path = root / "controlled-driver-metadata.json"
                write_json(metadata_path, {"metadata": metadata_by_task, "task_dirs": {key: str(value) for key, value in task_dirs.items()}})
                harbor_log = root / "controlled-driver-boundary.log"
                wrapper_path = root / "controlled-driver.py"
                wrapper_path.write_text(
                    textwrap.dedent(
                        f"""
                        from __future__ import annotations
                        import json
                        import sys
                        from pathlib import Path
                        from unittest.mock import patch

                        REPO = Path({str(ROOT)!r}).resolve()
                        sys.path.insert(0, str(REPO))
                        sys.path.insert(0, str(REPO / "src"))
                        import scripts.run_r015_c_only_harbor_driver as driver

                        CONTROL = Path({str(metadata_path)!r})
                        LOG = Path({str(harbor_log)!r})
                        values = json.loads(CONTROL.read_text(encoding="utf-8"))
                        metadata = values["metadata"]
                        task_dirs = {{key: Path(value) for key, value in values["task_dirs"].items()}}
                        paths = {{
                            "task_root": Path({str(task_root)!r}),
                            "harbor": "controlled-harbor",
                            "python": Path({str(Path(sys.executable).absolute())!r}),
                            "plugin": REPO / "openclaw_plugin",
                            "sidecar": REPO / "scripts" / "run_openclaw_r012_sidecar.py",
                            "manager_ledger": None,
                            "manager_config": None,
                        }}

                        def resolve(value):
                            task_id = value["assignment"]["task_id"]
                            return paths, metadata[task_id], task_dirs[task_id]

                        def record(label, task_id):
                            with LOG.open("a", encoding="utf-8") as stream:
                                stream.write(label + ":" + str(task_id) + "\\n")

                        def fake_harbor(context):
                            task_id = str(context["task_id"])
                            record("harbor", task_id)
                            process = {{
                                "kind": "r015_c_only_official_process",
                                "status": "official_trial_boundary_completed",
                                "classification": "completed",
                                "official_trial_boundary_started": True,
                                "sidecar_ready": True,
                                "harbor_returncode": 0,
                                "controlled_transport": True,
                            }}
                            driver.write_json(Path(context["artifact_root"]) / "harbor-process.json", process)
                            return process

                        def fake_import(context, process):
                            task_id = str(context["task_id"])
                            artifact_root = Path(context["artifact_root"])
                            session_path = artifact_root / "session.jsonl"
                            session_path.write_text(json.dumps({{"session_id": context["session_id"]}}) + "\\n", encoding="utf-8")
                            trace_path = artifact_root / "trajectory-live.json"
                            trace = {{
                                "source": {{"canonical_instance_id": task_id, "instance_id": task_id, "task_name": str(context["task_name"]), "session_id": context["session_id"]}},
                                "instruction": "The controlled task completed.",
                                "steps": [{{"source_entry_id": "controlled-1", "role": "toolResult", "content": "completed"}}],
                                "outcome": {{"reward": 1.0, "status": "completed"}},
                                "text_manager_eligible": False,
                                "historical": False,
                                "source_kind": "controlled_current_c_only",
                                "r015_binding": {{"round_id": int(context["trial_id"].split(":", 1)[0].removeprefix("r")), "task_id": task_id, "trial_id": context["trial_id"], "session_id": context["session_id"]}},
                            }}
                            driver.write_json(trace_path, trace)
                            return {{
                                "outcome": "completed",
                                "trajectory": {{"round_id": int(context["trial_id"].split(":", 1)[0].removeprefix("r")), "task_id": task_id, "trial_id": context["trial_id"], "session_id": context["session_id"], "complete": True, "path": str(trace_path), "sha256": driver.sha256_file(trace_path)}},
                                "raw_evidence": {{"official_harbor_trial": True, "evidence_mode": "official_live", "official_trial_boundary_started": True, "condition": "C-only", "round_id": int(context["trial_id"].split(":", 1)[0].removeprefix("r")), "task_id": task_id, "trial_id": context["trial_id"], "session_id": context["session_id"], "historical_baseline_used": False, "controlled_transport": True, "process": process}},
                                "proxy_attempt_records": [],
                                "trace": trace,
                            }}

                        class ControlledEncoder:
                            repo_id = "controlled-minilm"
                            resolved_revision = "controlled-revision"
                            def index_description(self, value):
                                return [1.0, 0.0], {{"kind": "controlled-description-index"}}
                            def index_skill(self, value):
                                return [1.0, 0.0], {{"kind": "controlled-skill-index"}}

                        if "--continue-from-trial" in sys.argv:
                            with patch.object(driver, "_task_root_and_service", side_effect=resolve), patch.object(driver, "_ensure_encoder", side_effect=lambda context, executor: ControlledEncoder()):
                                from scripts.legacy import run_r015_c_only_harbor_driver as legacy_driver
                                legacy_driver.main()
                        else:
                            with patch.object(driver, "_task_root_and_service", side_effect=resolve), patch.object(driver, "_harbor_config_check", side_effect=lambda context: {{"status": "valid", "controlled_transport": True, "official_trial_started": False}}), patch.object(driver, "_run_official_harbor", side_effect=fake_harbor), patch.object(driver, "_import_official_evidence", side_effect=fake_import):
                                driver.main()
                        """
                    ).strip()
                    + "\n",
                    encoding="utf-8",
                )

                loaded = COnlyProtocol.load(state_path, fixture_config, fixture_baseline)
                coordinator._run_driver(
                    loaded, wrapper_path, run_dir, state_path,
                    reconciliation_manifest_path=reconciliation_path,
                )

                restored = COnlyProtocol.load(state_path, fixture_config, fixture_baseline)
                self.assertEqual(restored.state["formal_campaign"], "complete")
                self.assertEqual(restored.current_round_id, 2)
                for round_id in ("1", "2"):
                    round_state = restored.state["rounds"][round_id]
                    self.assertEqual(round_state["status"], "complete")
                    self.assertEqual(set(round_state["completed_tasks"]), {"build-pmars", "cancel-async-tasks"})
                    operation_ids = [item["operation_id"] for item in round_state["operations"]]
                    self.assertEqual(len(operation_ids), len(set(operation_ids)))
                start_round = next(item for item in restored.state["global_operations"] if item["operation_id"] == "start-round-2")
                self.assertTrue(start_round["result"]["round_2_pools_empty"])
                round_two_input = run_dir / "round-2" / "build-pmars" / "driver-input.json"
                self.assertEqual(json.loads(round_two_input.read_text(encoding="utf-8"))["round_material"]["trajectory_pool"], [])
                round_one_next_input = run_dir / "round-1" / "cancel-async-tasks" / "driver-input.json"
                self.assertEqual(
                    [item["task_id"] for item in json.loads(round_one_next_input.read_text(encoding="utf-8"))["round_material"]["trajectory_pool"]],
                    ["build-pmars"],
                )

                continuation_path = input_path.with_name("driver-continuation-process.json")
                continuation = json.loads(continuation_path.read_text(encoding="utf-8"))
                self.assertEqual(continuation["status"], "succeeded")
                self.assertEqual(continuation["returncode"], 0)
                self.assertEqual(continuation["reconciliation_manifest"]["path"], str(reconciliation_path.resolve()))
                self.assertEqual(continuation["reconciliation_manifest"]["sha256"], original_reconciliation_hash)
                self.assertIn("--continue-from-trial", continuation["command"])
                self.assertIn(str(reconciliation_path.resolve()), continuation["command"])
                self.assertEqual(sha256_file(reconciliation_path), original_reconciliation_hash)
                self.assertEqual(sha256_file(response_path), original_response_hash)
                self.assertEqual(json.loads(ledger_path.read_text(encoding="utf-8"))["calls"][0]["call_id"], "call-0001")
                call_dirs = sorted(path.name for path in (manager_root / "model_calls").glob("call-*") if path.is_dir())
                self.assertEqual(call_dirs, ["call-0001", "call-0002", "call-0003"])
                self.assertEqual(len(server.chat_requests), 4)
                self.assertGreaterEqual(len(server.tokenize_requests), 3)
                boundary_lines = harbor_log.read_text(encoding="utf-8").splitlines()
                self.assertEqual(boundary_lines.count("harbor:cancel-async-tasks"), 2)
                self.assertEqual(boundary_lines.count("harbor:build-pmars"), 1)
                self.assertEqual(len(boundary_lines), 3)
                self.assertTrue((run_dir / "manager" / "model_calls" / "call-0003" / "response.json").is_file())
                self.assertEqual(len(list((run_dir / "manager" / "task-calls").glob("*/wire-response.json"))), 2)
                self.assertFalse((logical_root / "manager" / "model_calls").exists())
                self.assertTrue(_driver_stage_path(input_path, "extraction").is_file())
                self.assertTrue(_driver_stage_path(input_path, "publication").is_file())
                self.assertEqual(json.loads(coordinator._driver_process_path(input_path).read_text(encoding="utf-8"))["status"], "failed")
                self.assertTrue(load_reconciliation_manifest(reconciliation_path))
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
