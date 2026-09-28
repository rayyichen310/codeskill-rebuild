from __future__ import annotations

import json
import threading
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import unittest

from codeskill_rebuild.c_only_protocol import COnlyProtocol, COnlyProtocolError
from codeskill_rebuild.harbor_recovery import create_harbor_recovery_manifest
from codeskill_rebuild.task_graph import read_task_graph_state
from codeskill_rebuild.types import read_json, sha256_file, write_json
from scripts.run_r015_c_only import _driver_process_path, _run_driver, _write_driver_input
from scripts.run_r015_c_only_harbor_driver import _session_id


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "r015-c-only-coding.json"
BASELINE = ROOT / "docs" / "baselines" / "r015-legacy-coding-baseline-20260913.json"


class _ControlledManagerHandler(BaseHTTPRequestHandler):
    records: list[tuple[str, dict[str, object]]] = []
    lock = threading.Lock()
    force_overflow = False
    invalid_summary = False
    invalid_citation = False

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        with self.lock:
            self.records.append((self.path, payload))
            chat_number = sum(path.endswith("/chat/completions") for path, _ in self.records)
        evidence = json.loads(payload["messages"][-1]["content"])
        if self.path.endswith("/tokenize"):
            # Controlled exact-count boundary: complete trajectory requests
            # overflow; source segments and final compacted requests fit.
            nested_trajectory = evidence.get("trajectory")
            oversized = self.force_overflow and (
                "full_trajectory" in evidence
                or "steps" in evidence
                or (isinstance(nested_trajectory, dict) and "steps" in nested_trajectory)
            )
            response: dict[str, object] = {"count": 300000 if oversized else 1}
        elif self.path.endswith("/chat/completions"):
            if "segment_steps" in evidence:
                ids = [step["source_entry_id"] for step in evidence["segment_steps"]]
                model_value = {
                    "summary": "The retained tool observation records the attempted repair.",
                    "covered_step_ids": ids[:-1] if self.invalid_summary else ids,
                    "verbatim_evidence_step_ids": ["missing-step"] if self.invalid_citation else ["r"],
                }
            elif "max_events_per_trace" in evidence:
                model_value = {"schema_version": 2, "events": []}
            elif "task_name" not in evidence:
                model_value: dict[str, object] = {
                    "action": "skip",
                    "reason": "controlled recovery event extraction stop",
                }
            else:
                model_value = {
                    "task_family": "controlled diagnostic repair",
                    "observed_obstacle": "the controlled task exposed a reproducible obstacle",
                    "attempted_procedure": "inspect the bounded failure and rerun validation",
                    "observed_outcome": "the controlled task completed",
                    "source_step_ids": ["u"],
                }
            response = {
                "id": f"controlled-{chat_number}",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(model_value),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        else:
            self.send_error(404)
            return
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class R015COnlyHarborRecoveryIntegrationTest(unittest.TestCase):
    """Exercise the original-bound Harbor recovery through the real coordinator.

    The child driver runs the production ``--recover-from-harbor-manifest``
    path.  Only the external Harbor/task lookup boundary is controlled; the
    evidence importer, ManagerClient, journals, phase stages, protocol saves,
    publication, and next-task advance are real.
    """

    @staticmethod
    def _file_ref(path: Path) -> dict[str, object]:
        return {
            "path": str(path.resolve()),
            "exists": True,
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        }

    def test_original_failed_process_recovery_advances_once_without_harbor_relaunch(self) -> None:
        self._exercise()

    def test_overflow_uses_durable_d01_summary_after_event_without_candidates(self) -> None:
        self._exercise(force_overflow=True)

    def test_incomplete_summary_stops_without_publishing_or_replaying_harbor(self) -> None:
        self._exercise(force_overflow=True, invalid_summary=True)

    def test_unknown_summary_citation_stops_without_publishing_or_replaying_harbor(self) -> None:
        self._exercise(force_overflow=True, invalid_citation=True)

    def _exercise(self, *, force_overflow: bool = False, invalid_summary: bool = False,
                  invalid_citation: bool = False) -> None:
        _ControlledManagerHandler.records = []
        _ControlledManagerHandler.force_overflow = force_overflow
        _ControlledManagerHandler.invalid_summary = invalid_summary
        _ControlledManagerHandler.invalid_citation = invalid_citation
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ControlledManagerHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        # Register in reverse cleanup order so shutdown wakes serve_forever,
        # the thread exits, and only then the listening socket is closed.
        self.addCleanup(server.server_close)
        self.addCleanup(server_thread.join, 5)
        self.addCleanup(server.shutdown)
        endpoint = f"http://127.0.0.1:{server.server_port}/v1"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            baseline_path = root / "baseline.json"
            state_path = root / "state.json"
            config_value = json.loads(CONFIG.read_text(encoding="utf-8"))
            baseline_value = json.loads(BASELINE.read_text(encoding="utf-8"))
            config_value["tasks"] = deepcopy(config_value["tasks"][:2])
            baseline_value["tasks"] = deepcopy(baseline_value["tasks"][:2])
            for order, task in enumerate(config_value["tasks"], start=1):
                task["order"] = order
            for order, task in enumerate(baseline_value["tasks"], start=1):
                task["order"] = order
            # The fixture is a public local-task identity test; do not bind it
            # to the repository's immutable audit path.  The production
            # formal config retains that audit and is tested separately.
            config_value.pop("task_artifact_audit", None)
            config_value["baseline_manifest"] = {
                "path": str(baseline_path.resolve()),
                "sha256": "0" * 64,
                "comparison_only": True,
                "solver_input_imported": False,
                "skills_imported": False,
                "trajectories_imported": False,
            }
            config_value["runtime_alignment"]["baseline_observed"]["endpoint"] = endpoint
            write_json(baseline_path, baseline_value)
            config_value["baseline_manifest"]["sha256"] = sha256_file(baseline_path)
            write_json(config_path, config_value)

            protocol = COnlyProtocol.initialize(config_path, baseline_path, state_path)
            protocol.authorize_start()
            protocol.save(state_path)
            assignment = protocol.freeze_task("build-pmars")
            protocol.save(state_path)

            run_dir = root / "attempts" / "attempt-003"
            run_dir.mkdir(parents=True)
            input_path = _write_driver_input(protocol, assignment, run_dir, state_path)
            original_input_hash = sha256_file(input_path)
            original_output_path = input_path.with_name("driver-output.json")
            original_process_path = _driver_process_path(input_path)
            original_process = {
                "schema_version": 1,
                "kind": "r015_c_only_driver_process",
                "status": "failed",
                "condition": "C-only",
                "round_id": 1,
                "task_id": "build-pmars",
                "trial_id": "r1:C:build-pmars",
                "input_path": str(input_path),
                "input_sha256": original_input_hash,
                "output_path": str(original_output_path),
                "output_sha256": None,
                "returncode": 1,
                "timed_out": False,
                "error_type": "COnlyHarborDriverError",
                "error": "official Harbor evidence discovery failed; current task requires reconciliation",
            }
            write_json(original_process_path, original_process)

            task_root = root / "tasks"
            task_path = task_root / "build-pmars"
            task_path.mkdir(parents=True)
            task_toml = task_path / "task.toml"
            task_toml.write_text("[metadata]\nname = 'controlled build-pmars'\n", encoding="utf-8")
            session_id = _session_id("r1:C:build-pmars")
            trial_dir = root / "original-harbor" / "jobs" / "build-pmars__attempt-001"
            (trial_dir / "agent").mkdir(parents=True)
            (trial_dir / "verifier").mkdir()
            local_task = {
                "download_dir": None,
                "git_commit_id": None,
                "git_url": None,
                "name": None,
                "overwrite": False,
                "path": str(task_path.resolve()),
                "ref": None,
                "source": None,
            }
            write_json(
                trial_dir / "config.json",
                {
                    "trial_name": trial_dir.name,
                    "task": local_task,
                    "agents": [{"kwargs": {"session_id": session_id}}],
                },
            )
            (trial_dir / "agent" / "instruction.txt").write_text(
                "Repair the controlled build-pmars task.\n", encoding="utf-8"
            )
            session_events = [
                {"type": "session", "id": session_id},
                {
                    "type": "message",
                    "id": "u",
                    "parentId": None,
                    "message": {
                        "role": "user",
                        "content": "Repair the controlled build-pmars task.",
                    },
                },
                {
                    "type": "message", "id": "act", "parentId": "u",
                    "message": {"role": "assistant", "content": [
                        {"type": "toolCall", "id": "call-one", "name": "exec", "arguments": {"command": "true"}},
                    ]},
                },
                {
                    "type": "message", "id": "r", "parentId": "act",
                    "message": {"role": "toolResult", "toolCallId": "call-one", "toolName": "exec", "content": "Observed tool result", "details": {"exitCode": 0}},
                },
                {
                    "type": "message",
                    "id": "a",
                    "parentId": "r",
                    "message": {
                        "role": "assistant",
                        "content": "The official trial completed with a preserved observation.",
                        "stopReason": "stop",
                    },
                },
            ]
            (trial_dir / "agent" / "openclaw.session.jsonl").write_text(
                "\n".join(json.dumps(event) for event in session_events) + "\n",
                encoding="utf-8",
            )
            write_json(
                trial_dir / "result.json",
                {
                    "status": "completed",
                    "task_name": "terminal-bench/build-pmars",
                    "task_id": {"path": str(task_path.resolve())},
                    "trial_name": trial_dir.name,
                    "agent_result": {"status": "completed"},
                    "verifier_result": {"rewards": {"reward": 0.0}},
                    "exception_info": {
                        "exception_type": "NonZeroAgentExitCodeError",
                        "exception_message": "agent stopped after the input-budget rejection",
                    },
                },
            )
            (trial_dir / "verifier" / "reward.txt").write_text("0\n", encoding="utf-8")

            original_artifact_root = root / "original-harbor" / "official-process"
            original_artifact_root.mkdir(parents=True)
            task_config = original_artifact_root / "job.json"
            sidecar_config = original_artifact_root / "sidecar.json"
            host_config = original_artifact_root / "openclaw-host.json"
            database = original_artifact_root / "openclaw-agent.sqlite"
            write_json(task_config, {"kind": "controlled-public-job-config"})
            write_json(sidecar_config, {"kind": "controlled-public-sidecar-config"})
            write_json(host_config, {"kind": "controlled-public-openclaw-config"})
            database.write_bytes(b"controlled immutable sqlite seed\n")
            official_process = {
                "kind": "r015_c_only_official_process",
                "status": "official_trial_boundary_completed",
                "classification": "harbor_nonzero",
                "official_trial_boundary_started": True,
            }
            process_evidence = {
                **official_process,
                "task_config": self._file_ref(task_config),
                "sidecar_config": self._file_ref(sidecar_config),
                "openclaw_host_config": self._file_ref(host_config),
                "native_database_seed": self._file_ref(database),
            }
            official_process_path = original_artifact_root / "harbor-process.json"
            write_json(official_process_path, official_process)

            original_sidecar = root / "original-harbor" / "sidecar"
            attempts_dir = original_sidecar / "upstream_requests"
            attempts_dir.mkdir(parents=True)
            overlay_path = original_sidecar / "overlay-state.json"
            write_json(overlay_path, {"kind": "controlled-overlay-state"})
            write_json(
                attempts_dir / "attempt-0001.json",
                {
                    "schema_version": 3,
                    "kind": "r012_actual_upstream_request",
                    "trial_id": "r1:C:build-pmars",
                    "attempt_ordinal": 1,
                    "state_path": str(overlay_path.resolve()),
                    "proxy_outcome": "stream_forwarded",
                    "forwarded_request": {
                        "messages": [
                            {
                                "role": "user",
                                "content": "Repair the controlled build-pmars task.",
                            }
                        ]
                    },
                    "normal_call_boundary": {
                        "kind": "r012_public_plugin_normal_call_boundary",
                        "trial_id": "r1:C:build-pmars",
                        "session_id": session_id,
                    },
                },
            )
            # Preserve the real task6 boundary shape: the final overlay
            # rejection has a candidate payload and selection journal, but no
            # forwarded request or provider boundary because the request was
            # rejected before the upstream transport opened.
            write_json(
                attempts_dir / "attempt-0002.json",
                {
                    "schema_version": 3,
                    "kind": "r012_actual_upstream_request",
                    "trial_id": "r1:C:build-pmars",
                    "attempt_ordinal": 2,
                    "forwarded_request_ordinal": 2,
                    "native_request": {"messages": [{"role": "user", "content": "native"}]},
                    "preflight_candidate_forwarded_request": {
                        "messages": [{"role": "user", "content": "candidate"}],
                    },
                    "exact_forwarded_input_tokens": 250234,
                    "max_input_tokens": 250000,
                    "token_count_scope": "complete_forwarded_openai_payload",
                    "uncommitted_selection": {"event": {"decision": "injected_pending_budget"}},
                    "outcome": "context_or_overlay_error",
                    "error_type": "OverlayInputLimitError",
                    "error": "forwarded input 250234 exceeds configured input budget 250000",
                    "state_path": str(overlay_path.resolve()),
                },
            )

            import_failure_path = original_artifact_root / "import-failure.json"
            write_json(
                import_failure_path,
                {
                    "kind": "r015_c_only_official_import_failure",
                    "official_harbor_trial": True,
                    "task_id": "build-pmars",
                    "trial_id": "r1:C:build-pmars",
                    "process": process_evidence,
                    "error_type": "COnlyHarborDriverError",
                    "error": "preserved controlled importer failure before trial stage",
                },
            )
            recovery_manifest_path = root / "harbor-recovery.json"
            recovery_root = input_path.parent / "explicit-recovery"
            manifest = create_harbor_recovery_manifest(
                input_path=input_path,
                original_driver_process_path=original_process_path,
                import_failure_path=import_failure_path,
                official_harbor_process_path=official_process_path,
                trial_dir=trial_dir,
                sidecar_dir=original_sidecar,
                recovery_root=recovery_root,
                manager_root=run_dir / "manager",
                output_path=recovery_manifest_path,
            )
            self.assertEqual(manifest["safety"]["harbor_rerun"], False)
            immutable_original_refs = {
                "input": sha256_file(input_path),
                "process": sha256_file(original_process_path),
                "import_failure": sha256_file(import_failure_path),
                "official_process": sha256_file(official_process_path),
                "result": sha256_file(trial_dir / "result.json"),
                "session": sha256_file(trial_dir / "agent" / "openclaw.session.jsonl"),
                "sidecar_attempt": sha256_file(attempts_dir / "attempt-0001.json"),
            }

            wrapper_path = root / "controlled-recovery-driver.py"
            sentinel_path = root / "normal-driver-reached.txt"
            wrapper_path.write_text(
                """
import sys
from pathlib import Path

ROOT = Path(__ROOT__)
TASK_ROOT = Path(__TASK_ROOT__)
SENTINEL = Path(__SENTINEL__)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
import scripts.run_r015_c_only_harbor_driver as driver

def task_binding(value):
    task_id = str(value["assignment"]["task_id"])
    task_path = TASK_ROOT / task_id
    task_toml = task_path / "task.toml"
    metadata = {
        "task_name": "terminal-bench/" + task_id,
        "task_path": str(task_path),
        "task_checkout_commit": "controlled-public-checkout",
        "task_toml": {
            "path": str(task_toml),
            "sha256": driver.sha256_file(task_toml),
            "size_bytes": task_toml.stat().st_size,
        },
        "public_environment": {
            "docker_image": "controlled/" + task_id + ":test",
            "docker_image_identity": "sha256:" + "a" * 64,
            "agent_timeout_sec": 30,
            "verifier_timeout_sec": 30,
            "build_timeout_sec": 30,
        },
    }
    paths = {
        "task_root": TASK_ROOT,
        "harbor": "controlled-harbor",
        "python": Path(sys.executable),
        "plugin": ROOT / "openclaw_plugin",
        "sidecar": ROOT / "scripts" / "run_openclaw_r012_sidecar.py",
        "manager_ledger": None,
        "manager_config": None,
    }
    return paths, metadata, task_path

def forbidden(*args, **kwargs):
    raise RuntimeError("Harbor/sidecar execution is forbidden during artifact recovery")

driver._task_root_and_service = task_binding
driver._run_official_harbor = forbidden
driver._sidecar_check = forbidden
driver._harbor_config_check = forbidden
if "--recover-from-harbor-manifest" not in sys.argv:
    SENTINEL.write_text("normal next-task driver reached", encoding="utf-8")
    raise SystemExit(77)
driver.main()
""".replace("__ROOT__", repr(str(ROOT.resolve())))
                .replace("__TASK_ROOT__", repr(str(task_root.resolve())))
                .replace("__SENTINEL__", repr(str(sentinel_path.resolve()))),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(COnlyProtocolError, "official C-only driver failed"):
                _run_driver(
                    protocol,
                    wrapper_path,
                    run_dir,
                    state_path,
                    harbor_recovery_manifest_path=recovery_manifest_path,
                )

            second_input = run_dir / "round-1" / "cancel-async-tasks" / "driver-input.json"
            second_process = _driver_process_path(second_input)
            recovered_process = recovery_root / "driver-recovery-process.json"
            if invalid_summary or invalid_citation:
                self.assertFalse(sentinel_path.exists())
                self.assertEqual(len([path for path, _ in _ControlledManagerHandler.records if path.endswith("/chat/completions")]), 2)
                self.assertFalse((recovery_root / "driver-stage-extraction.json").exists())
                self.assertFalse((recovery_root / "driver-stage-publication.json").exists())
                self.assertEqual(sha256_file(original_process_path), immutable_original_refs["process"])
                loaded = COnlyProtocol.load(state_path, config_path, baseline_path)
                self.assertEqual(loaded.current_task_id, "build-pmars")
                self.assertEqual(loaded._round(1)["bank"]["skills"], [])
                return
            self.assertTrue(
                sentinel_path.is_file(),
                "coordinator did not advance to the next task; "
                f"root files={sorted(str(path.relative_to(root)) for path in root.rglob('*') if path.is_file())}; "
                f"second_process={read_json(second_process) if second_process.is_file() else None}; "
                f"recovery_process={read_json(recovered_process) if recovered_process.is_file() else None}; "
                f"reconcile={read_json(input_path.with_name('reconcile-required.json')) if input_path.with_name('reconcile-required.json').is_file() else None}",
            )
            self.assertEqual(
                [path for path, _ in _ControlledManagerHandler.records if path.endswith("/chat/completions")],
                ["/v1/chat/completions"] * (4 if force_overflow else 3),
            )
            self.assertGreaterEqual(
                sum(path.endswith("/tokenize") for path, _ in _ControlledManagerHandler.records),
                4,
            )
            if force_overflow:
                calls = [payload for path, payload in _ControlledManagerHandler.records if path.endswith("/chat/completions")]
                bodies = [json.loads(payload["messages"][-1]["content"]) for payload in calls]
                self.assertEqual(sum("segment_steps" in body for body in bodies), 1)
                self.assertEqual(bodies[0]["max_events_per_trace"], 8)
                self.assertEqual(bodies[1]["required_covered_step_ids"], ["u", "act", "r", "a"])
                compacted = bodies[2]
                self.assertTrue(compacted["evidence_compacted"])
                self.assertEqual([step["source_entry_id"] for step in compacted["original_cited_fragments"]],
                                 ["u", "act", "r"])
                self.assertEqual(compacted["evidence_summaries"][0]["covered_step_ids"],
                                 ["u", "act", "r", "a"])
                self.assertIn("handles", bodies[3])

            recovered_output = recovery_root / "driver-output.json"
            self.assertTrue(recovered_output.is_file())
            self.assertTrue(recovered_process.is_file())
            self.assertTrue((recovery_root / "recovery-intent.json").is_file())
            self.assertTrue((recovery_root / "recovery-complete.json").is_file())
            for phase in ("trial", "extraction", "publication"):
                self.assertTrue((recovery_root / f"driver-stage-{phase}.json").is_file())
            recovery_process_value = read_json(recovered_process)
            self.assertEqual(recovery_process_value["status"], "succeeded")
            self.assertEqual(recovery_process_value["returncode"], 0)
            self.assertEqual(recovery_process_value["continuation_from"], "original_failed_harbor_trial_artifact_recovery")
            recovered_value = read_json(recovered_output)
            self.assertEqual(recovered_value["trial"]["outcome"], "completed")
            self.assertEqual(recovered_value["trial"]["raw_evidence"]["classification"], "agent_failure")
            self.assertEqual(recovered_value["trial"]["raw_evidence"]["official_reward"], "0")
            self.assertEqual(len(recovered_value["proxy_attempt_records"]), 2)
            self.assertEqual(
                recovered_value["proxy_attempt_records"][1]["_non_forwarded_terminal"]["reason"],
                "input_budget_rejected_before_provider_boundary",
            )
            self.assertNotIn("normal_call_boundary", recovered_value["proxy_attempt_records"][1])
            self.assertNotIn("forwarded_request", recovered_value["proxy_attempt_records"][1])
            preflight = recovered_value["trial"]["raw_evidence"]["preflight_terminal_observations"]
            self.assertEqual(len(preflight), 1)
            self.assertEqual(preflight[0]["exact_forwarded_input_tokens"], 250234)
            self.assertEqual(preflight[0]["max_input_tokens"], 250000)
            self.assertFalse(preflight[0]["provider_boundary"])

            loaded = COnlyProtocol.load(state_path, config_path, baseline_path)
            first = loaded._round(1)["assignments"]["build-pmars"]
            self.assertEqual(first["status"], "finished")
            self.assertIn("extraction", first)
            self.assertIn("publication", first)
            handoff_path = recovery_root / "extraction-awaiting-ack.json"
            ack_path = recovery_root / "extraction-ack.json"
            self.assertTrue(handoff_path.is_file())
            self.assertEqual(read_json(ack_path)["handoff_sha256"], sha256_file(handoff_path))
            graph_marker = first["extraction"]["evidence"]["task"]["graph"]
            graph_state = read_task_graph_state(
                directory=Path(graph_marker["directory"]), identity=graph_marker["identity"])
            if force_overflow:
                self.assertEqual(graph_state["outcomes"]["d01_plan"]["mode"], "segmented")
                self.assertEqual(graph_state["outcomes"]["d01_plan"]["full_input_tokens"], 300000)
                self.assertEqual(graph_state["outcomes"]["d01_bundle"]["summary_count"], 1)
            else:
                self.assertEqual(graph_state["outcomes"]["d01_plan"]["mode"], "full")
                self.assertEqual(graph_state["description_results"], [])
            self.assertEqual(graph_state["stage"], "publication_confirmed")
            self.assertEqual(graph_state["event_receipt"]["extraction_ref"]["ack_sha256"], sha256_file(ack_path))
            self.assertIn("build-pmars", loaded._round(1)["completed_tasks"])
            self.assertEqual(loaded.current_task_id, "cancel-async-tasks")
            self.assertTrue(second_input.is_file())
            self.assertEqual(read_json(second_process)["status"], "failed")

            # Recovery manager artifacts follow the trusted attempt root.  A
            # sibling under the logical state directory must not be created.
            graph_responses = list((run_dir / "manager" / "task-calls").glob("*/wire-response.json"))
            self.assertEqual(len(graph_responses), 4 if force_overflow else 3)
            self.assertTrue(all((path.parent / "wire-request.json").is_file()
                                for path in graph_responses))
            self.assertFalse((root / "manager").exists())
            ledger = read_json(run_dir / "manager-ledger.json")
            self.assertEqual(ledger["calls"], [])
            self.assertEqual(len(loaded._round(1)["trajectory_pool"]), 1)
            self.assertEqual(len(loaded._round(1)["description_pool"]), 1)
            second_input_value = read_json(second_input)
            self.assertEqual(
                [item["task_id"] for item in second_input_value["round_material"]["trajectory_pool"]],
                ["build-pmars"],
            )
            self.assertEqual(
                [item["task_id"] for item in second_input_value["round_material"]["description_pool"]],
                ["build-pmars"],
            )

            for label, expected in immutable_original_refs.items():
                paths = {
                    "input": input_path,
                    "process": original_process_path,
                    "import_failure": import_failure_path,
                    "official_process": official_process_path,
                    "result": trial_dir / "result.json",
                    "session": trial_dir / "agent" / "openclaw.session.jsonl",
                    "sidecar_attempt": attempts_dir / "attempt-0001.json",
                }
                self.assertEqual(sha256_file(paths[label]), expected, label)
            self.assertFalse(original_output_path.exists())
            for phase in ("trial", "extraction", "publication"):
                self.assertFalse(input_path.with_name(f"driver-stage-{phase}.json").exists())
            calls_before_replay = len(_ControlledManagerHandler.records)
            with self.assertRaisesRegex(COnlyProtocolError, "different current driver input"):
                _run_driver(
                    loaded, wrapper_path, run_dir, state_path,
                    harbor_recovery_manifest_path=recovery_manifest_path,
                )
            self.assertEqual(len(_ControlledManagerHandler.records), calls_before_replay)


if __name__ == "__main__":
    unittest.main()
