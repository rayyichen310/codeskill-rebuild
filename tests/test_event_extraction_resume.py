from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from codeskill_rebuild.event_extraction import EventExtractionSchedule
from codeskill_rebuild.types import contract_from_files, write_json


def trace() -> dict:
    return {
        "source": {"canonical_instance_id": "fixture-source", "instance_id": "fixture-source", "task_name": "terminal-bench/fixture-source"},
        "instruction": "fixture instruction",
        "text_manager_eligible": True,
        "outcome": {},
        "steps": [
            {"source_entry_id": "u", "role": "user", "content": "task"},
            {"source_entry_id": "trigger", "role": "toolResult", "content": "observed failure"},
            {"source_entry_id": "response", "role": "assistant", "content": "inspect failure"},
            {"source_entry_id": "outcome", "role": "toolResult", "content": "observed result"},
        ],
    }


def paper_skill() -> dict:
    return {
        "title": "Inspect the observed failure",
        "granularity": "event-driven",
        "when_to_apply": "After a command result reports a failure",
        "rules": ["Inspect the observed failure before retrying."],
    }


def valid_result() -> dict:
    return {
        "action": "generate",
        "skill": paper_skill(),
        "evidence": {
            "trigger_step_ids": ["trigger"],
            "response_step_ids": ["response"],
            "outcome_step_ids": ["outcome"],
            "rule_evidence": [{"rule_index": 0, "step_ids": ["trigger", "response", "outcome"]}],
        },
    }


class EventExtractionResumeTest(unittest.TestCase):
    def env(self) -> dict[str, str]:
        value = dict(os.environ)
        value["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        return value

    def test_repair_resolves_paid_slot_without_reissuing_original_then_continues(self) -> None:
        requests: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append({"path": self.path, "request": request})
                if self.path == "/tokenize":
                    response = {"count": 10}
                else:
                    system = request["messages"][0]["content"]
                    output = valid_result() if system == "repair prompt" else {"action": "skip", "reason": "fixture no additional event"}
                    response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(output)}}], "usage": {"prompt_tokens": 10}}
                encoded = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        root = Path(__file__).resolve().parents[1]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp)
                prior = work / "prior"
                source_path = work / "trace.json"
                write_json(source_path, trace())
                source_manifest = work / "sources.json"
                write_json(source_manifest, {"sources": [{"canonical_instance_id": "fixture-source", "normalized_path": str(source_path)}]})
                spec, decisions = work / "spec.md", work / "decisions.md"
                spec.write_text("文件版本：v0.11", encoding="utf-8")
                decisions.write_text("R012", encoding="utf-8")
                runtime, repair = work / "runtime.md", work / "repair.md"
                runtime.write_text("runtime prompt", encoding="utf-8")
                repair.write_text("repair prompt", encoding="utf-8")
                planned = subprocess.run(
                    [
                        sys.executable,
                        str(root / "scripts" / "run_m2_r012_event_extraction.py"),
                        "--run-dir", str(prior), "--source-manifest", str(source_manifest),
                        "--runtime-prompt", str(runtime), "--spec", str(spec), "--decisions", str(decisions),
                    ],
                    capture_output=True, text=True, env=self.env(), timeout=30,
                )
                self.assertEqual(planned.returncode, 0, planned.stderr)
                planned_status = json.loads((prior / "run-status.json").read_text(encoding="utf-8"))
                # The real failure had one earlier generated attempt.  Rebuild
                # the original entrypoint's schedule/manifest shape so a next
                # ordinal of two is eligible for repair.
                schedule = EventExtractionSchedule.from_manifest(trace(), planned_status["schedules"][0])
                schedule.record_initial_result(
                    {
                        "action": "generate",
                        "skill": {**paper_skill(), "title": "Prior distinct event", "granularity": "event", "benchmark": "terminal-bench"},
                        "evidence": valid_result()["evidence"],
                    },
                    model_call_id="call-0001",
                    evidence={},
                )
                invalid = {**valid_result(), "evidence": {**valid_result()["evidence"], "response_step_ids": ["u"]}}
                write_json(prior / "extraction" / "event" / "fixture-source" / "initial-02-failure.json", {"source_instance_id": "fixture-source", "error": "event response must be an assistant action after the local trigger"})
                write_json(prior / "model_calls" / "call-0011" / "request.json", {"purpose": "r012_event_initial:fixture-source:2"})
                write_json(prior / "model_calls" / "call-0011" / "response.json", {"parsed_response": {"choices": [{"message": {"content": json.dumps(invalid)}}]}})
                write_json(prior / "run-status.json", {"status": "blocked_or_failed", "failure": {"source_instance_id": "fixture-source", "initial_attempt_ordinal": 2, "classification": "post_call_validation_or_runner_failure", "error": "event response must be an assistant action after the local trigger"}, "completed_schedules": [schedule.manifest()]})
                config, ledger = work / "config.json", work / "ledger.json"
                write_json(config, {"services": {"deepseek_flash": {"base_url": f"http://127.0.0.1:{server.server_port}", "model_id": "fixture"}}})
                write_json(ledger, {"schema_version": 2, "limit": "unlimited", "calls": []})
                derived = work / "derived"
                result = subprocess.run(
                    [
                        sys.executable,
                        str(root / "scripts" / "resume_m2_r012_event_extraction.py"),
                        "--prior-run-dir", str(prior), "--derived-run-dir", str(derived), "--source-manifest", str(source_manifest),
                        "--runtime-prompt", str(runtime), "--repair-prompt", str(repair), "--spec", str(spec), "--decisions", str(decisions),
                        "--config", str(config), "--ledger", str(ledger), "--execute-manager", "--activate-unlimited-development-ledger",
                    ],
                    capture_output=True, text=True, env=self.env(), timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                status = json.loads((derived / "run-status.json").read_text(encoding="utf-8"))
                repaired_schedule = status["schedules"][0]
                self.assertEqual(status["status"], "completed")
                self.assertEqual([item["initial_attempt_ordinal"] for item in repaired_schedule["attempts"]], [1, 2, 3])
                self.assertEqual(repaired_schedule["attempts"][1]["outcome"], "repaired_generated")
                self.assertEqual(repaired_schedule["attempts"][1]["model_call_id"], "call-0011")
                self.assertEqual(repaired_schedule["retry_records"][0]["model_call_id"], "call-0001")
                self.assertTrue((derived / "extraction" / "event" / "fixture-source" / "initial-03.json").is_file())
                self.assertEqual(json.loads((prior / "model_calls" / "call-0011" / "request.json").read_text(encoding="utf-8"))["purpose"], "r012_event_initial:fixture-source:2")
                self.assertTrue((derived / "contract" / "contract.json").is_file())
                self.assertFalse((derived / "contract" / "contract" / "contract.json").exists())
                changed_runtime = work / "changed-runtime.md"
                changed_runtime.write_text("changed prompt", encoding="utf-8")
                rejected = subprocess.run(
                    [
                        sys.executable,
                        str(root / "scripts" / "resume_m2_r012_event_extraction.py"),
                        "--prior-run-dir", str(prior), "--derived-run-dir", str(work / "changed-input"),
                        "--source-manifest", str(source_manifest), "--runtime-prompt", str(changed_runtime),
                        "--repair-prompt", str(repair), "--spec", str(spec), "--decisions", str(decisions),
                        "--config", str(config), "--ledger", str(ledger), "--execute-manager", "--activate-unlimited-development-ledger",
                    ],
                    capture_output=True, text=True, env=self.env(), timeout=30,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn("runtime_prompt differs", rejected.stderr)
                self.assertFalse((work / "changed-input").exists())
                changed_trace = trace()
                changed_trace["instruction"] = "same source ID, changed normalized content"
                write_json(source_path, changed_trace)
                trace_rejected = subprocess.run(
                    [
                        sys.executable,
                        str(root / "scripts" / "resume_m2_r012_event_extraction.py"),
                        "--prior-run-dir", str(prior), "--derived-run-dir", str(work / "changed-trace"),
                        "--source-manifest", str(source_manifest), "--runtime-prompt", str(runtime),
                        "--repair-prompt", str(repair), "--spec", str(spec), "--decisions", str(decisions),
                        "--config", str(config), "--ledger", str(ledger), "--execute-manager", "--activate-unlimited-development-ledger",
                    ],
                    capture_output=True, text=True, env=self.env(), timeout=30,
                )
                self.assertNotEqual(trace_rejected.returncode, 0)
                self.assertIn("normalized trace for fixture-source differs", trace_rejected.stderr)
                self.assertFalse((work / "changed-trace").exists())
        finally:
            server.shutdown()
            server.server_close()
        completion_prompts = [item["request"]["messages"][0]["content"] for item in requests if item["path"] == "/chat/completions"]
        self.assertEqual(completion_prompts, ["repair prompt", "runtime prompt"])


if __name__ == "__main__":
    unittest.main()
