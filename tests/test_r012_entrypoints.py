from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from codeskill_rebuild.bank import SkillBank
from codeskill_rebuild.r012_execution import profile_sha256
from codeskill_rebuild.runtime import render_skill
from codeskill_rebuild.types import canonical_json, sha256_text


EVENT = {
    "skill_id": "event-a",
    "version": 1,
    "title": "Inspect output",
    "granularity": "event",
    "when_to_apply": "After output",
    "rules": ["Inspect it."],
    "benchmark": "terminal-bench",
}


class R012EntrypointTest(unittest.TestCase):
    def env(self) -> dict[str, str]:
        root = Path(__file__).resolve().parents[1]
        value = os.environ.copy()
        value["PYTHONPATH"] = str(root / "src")
        return value

    def test_default_event_prompt_declares_raw_evidence_sidecar_contract(self) -> None:
        root = Path(__file__).resolve().parents[1]
        prompt = (root / "prompts" / "custom" / "r012_fig07_event_extraction_evidence.md").read_text(encoding="utf-8")
        self.assertIn('"evidence":{"trigger_step_ids"', prompt)
        self.assertIn("exact `source_entry_id` values", prompt)
        self.assertIn("Do not cite native compaction controls", prompt)

    def test_event_runner_plan_only_writes_three_attempt_schedule_without_calls(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            trace = {
                "source": {"canonical_instance_id": "source"},
                "instruction": "task",
                "text_manager_eligible": True,
                "steps": [],
                "outcome": {},
            }
            trace_path = work / "trace.json"
            trace_path.write_text(json.dumps(trace), encoding="utf-8")
            manifest = work / "sources.json"
            manifest.write_text(json.dumps({"sources": [{"canonical_instance_id": "source", "normalized_path": str(trace_path)}]}), encoding="utf-8")
            prompt = work / "prompt.md"
            prompt.write_text("prompt", encoding="utf-8")
            spec = work / "REPRODUCTION_SPEC.md"
            spec.write_text("文件版本：v0.11", encoding="utf-8")
            decisions = work / "RESEARCH_DECISIONS.md"
            decisions.write_text("R012", encoding="utf-8")
            run_dir = work / "run"
            result = subprocess.run(
                [sys.executable, str(root / "scripts" / "run_m2_r012_event_extraction.py"), "--run-dir", str(run_dir), "--source-manifest", str(manifest), "--runtime-prompt", str(prompt), "--spec", str(spec), "--decisions", str(decisions)],
                capture_output=True,
                text=True,
                env=self.env(),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            status = json.loads((run_dir / "run-status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "planned_no_model_calls")
        self.assertEqual(status["schedules"][0]["maximum_initial_attempts"], 3)

    def test_event_runner_uses_r006_fallback_after_exact_full_overflow_and_activates_ledger(self) -> None:
        """The live entrypoint retains raw evidence instead of truncating it."""
        requests: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                request = json.loads(self.rfile.read(length))
                requests.append({"path": self.path, "request": request})
                user = json.loads(request["messages"][-1]["content"])
                if self.path == "/tokenize":
                    count = 600_000 if "full_trajectory" in user or ("segment_steps" in user and len(user["segment_steps"]) > 3) else 10
                    response = {"count": count}
                elif "segment_steps" in user:
                    ids = [item["source_entry_id"] for item in user["segment_steps"]]
                    observations = [item["source_entry_id"] for item in user["segment_steps"] if item["role"] == "toolResult"]
                    response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"summary": "All supplied source steps are observed.", "covered_step_ids": ids, "verbatim_evidence_step_ids": [observations[-1]]})}}], "usage": {"prompt_tokens": 10}}
                else:
                    response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"action": "generate", "skill": {"title": "Inspect a failed tool result", "granularity": "event-driven", "when_to_apply": "After a tool result reports a failure.", "rules": ["Use the observed failure to choose the next command."]}, "evidence": {"trigger_step_ids": ["r1"], "response_step_ids": ["a2"], "outcome_step_ids": ["r2"], "rule_evidence": [{"rule_index": 0, "step_ids": ["r1", "a2", "r2"]}]}})}}], "usage": {"prompt_tokens": 10}}
                encoded = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        root = Path(__file__).resolve().parents[1]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp)
                trace = {
                    "source": {"canonical_instance_id": "source"}, "instruction": "task", "text_manager_eligible": True, "outcome": {},
                    "steps": [
                        {"source_entry_id": "u", "role": "user"},
                        {"source_entry_id": "a1", "role": "assistant", "assistant": {"tool_calls": [{"tool_call_id": "c1"}]}},
                        {"source_entry_id": "r1", "role": "toolResult", "tool_result": {"tool_call_id": "c1"}},
                        {"source_entry_id": "a2", "role": "assistant", "assistant": {"tool_calls": [{"tool_call_id": "c2"}]}},
                        {"source_entry_id": "r2", "role": "toolResult", "tool_result": {"tool_call_id": "c2"}},
                    ],
                }
                trace_path = work / "trace.json"
                trace_path.write_text(json.dumps(trace), encoding="utf-8")
                sources = work / "sources.json"
                sources.write_text(json.dumps({"sources": [{"canonical_instance_id": "source", "normalized_path": str(trace_path)}]}), encoding="utf-8")
                prompt = work / "prompt.md"
                compact = work / "compact.md"
                spec = work / "REPRODUCTION_SPEC.md"
                decisions = work / "RESEARCH_DECISIONS.md"
                config = work / "config.json"
                ledger = work / "ledger.json"
                for path, content in ((prompt, "event prompt"), (compact, "summary prompt"), (spec, "文件版本：v0.11"), (decisions, "R014")):
                    path.write_text(content, encoding="utf-8")
                config.write_text(json.dumps({"services": {"deepseek_flash": {"base_url": f"http://127.0.0.1:{server.server_port}", "model_id": "fixture"}}}), encoding="utf-8")
                ledger.write_text(json.dumps({"schema_version": 1, "limit": 100, "calls": []}), encoding="utf-8")
                run_dir = work / "run"
                result = subprocess.run(
                    [sys.executable, str(root / "scripts" / "run_m2_r012_event_extraction.py"), "--run-dir", str(run_dir), "--source-manifest", str(sources), "--runtime-prompt", str(prompt), "--evidence-compaction-prompt", str(compact), "--spec", str(spec), "--decisions", str(decisions), "--execute-manager", "--activate-unlimited-development-ledger", "--config", str(config), "--ledger", str(ledger)],
                    capture_output=True, text=True, env=self.env(), timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                first = json.loads((run_dir / "extraction" / "event" / "source" / "initial-01.json").read_text(encoding="utf-8"))
                self.assertEqual(first["trajectory_input_mode"], "evidence_compacted")
                self.assertEqual(first["evidence"]["original_full_request_tokens"], 600_000)
                self.assertEqual(first["evidence"]["retained_step_ids"], ["a1", "a2", "r1", "r2"])
                self.assertTrue((run_dir / "extraction" / "event" / "source" / "summaries" / "initial-01-segment-01.json").is_file())
                self.assertTrue((run_dir / "extraction" / "event" / "source" / "summaries" / "initial-01-segment-02.json").is_file())
                final_request = json.loads((run_dir / "model_calls" / "call-0003" / "request.json").read_text(encoding="utf-8"))
                self.assertEqual(first["evidence"]["messages_sha256"], sha256_text(canonical_json(final_request["request"]["messages"])))
                self.assertEqual(first["evidence"]["composed_prompt_refs"]["runtime_prompt"]["sha256"], sha256_text("event prompt"))
                self.assertEqual(json.loads(ledger.read_text(encoding="utf-8"))["limit"], "unlimited")
        finally:
            server.shutdown()
            server.server_close()
        final_requests = [item["request"] for item in requests if item["path"] == "/chat/completions" and "evidence_compacted" in item["request"]["messages"][-1]["content"]]
        self.assertGreaterEqual(len(final_requests), 2)
        self.assertTrue(json.loads(final_requests[1]["messages"][-1]["content"])["previous_event_candidates"])

    def test_event_runner_records_post_summary_context_block_without_final_completion(self) -> None:
        requests: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append({"path": self.path, "request": request})
                user = json.loads(request["messages"][-1]["content"])
                if self.path == "/tokenize":
                    response = {"count": 600_000 if "full_trajectory" in user or user.get("evidence_compacted") else 10}
                else:
                    ids = [item["source_entry_id"] for item in user["segment_steps"]]
                    response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"summary": "Observed source segment.", "covered_step_ids": ids, "verbatim_evidence_step_ids": ["r1", "r2"]})}}], "usage": {"prompt_tokens": 10}}
                encoded = json.dumps(response).encode("utf-8")
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
                trace = {"source": {"canonical_instance_id": "source"}, "instruction": "task", "text_manager_eligible": True, "outcome": {}, "steps": [{"source_entry_id": "u", "role": "user"}, {"source_entry_id": "a1", "role": "assistant", "assistant": {"tool_calls": [{"tool_call_id": "c1"}]}}, {"source_entry_id": "r1", "role": "toolResult", "tool_result": {"tool_call_id": "c1"}}, {"source_entry_id": "a2", "role": "assistant", "assistant": {"tool_calls": [{"tool_call_id": "c2"}]}}, {"source_entry_id": "r2", "role": "toolResult", "tool_result": {"tool_call_id": "c2"}}]}
                trace_path = work / "trace.json"
                trace_path.write_text(json.dumps(trace), encoding="utf-8")
                sources = work / "sources.json"
                sources.write_text(json.dumps({"sources": [{"canonical_instance_id": "source", "normalized_path": str(trace_path)}]}), encoding="utf-8")
                prompt, compact, spec, decisions, config, ledger = (work / name for name in ("prompt.md", "compact.md", "spec.md", "decisions.md", "config.json", "ledger.json"))
                for path, content in ((prompt, "event prompt"), (compact, "summary prompt"), (spec, "文件版本：v0.11"), (decisions, "R014")):
                    path.write_text(content, encoding="utf-8")
                config.write_text(json.dumps({"services": {"deepseek_flash": {"base_url": f"http://127.0.0.1:{server.server_port}", "model_id": "fixture"}}}), encoding="utf-8")
                ledger.write_text(json.dumps({"schema_version": 1, "limit": 100, "calls": []}), encoding="utf-8")
                run_dir = work / "run"
                result = subprocess.run([sys.executable, str(root / "scripts" / "run_m2_r012_event_extraction.py"), "--run-dir", str(run_dir), "--source-manifest", str(sources), "--runtime-prompt", str(prompt), "--evidence-compaction-prompt", str(compact), "--spec", str(spec), "--decisions", str(decisions), "--execute-manager", "--activate-unlimited-development-ledger", "--config", str(config), "--ledger", str(ledger)], capture_output=True, text=True, env=self.env(), timeout=30)
                self.assertNotEqual(result.returncode, 0)
                failure = json.loads((run_dir / "run-status.json").read_text(encoding="utf-8"))["failure"]
            self.assertEqual(failure["classification"], "context_blocked_after_manager_summary_calls")
            self.assertEqual(failure["context_block_detail"]["phase"], "final_preflight_after_summaries")
            self.assertEqual(failure["context_block_detail"]["manager_call_count_before_block"], 1)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(len([item for item in requests if item["path"] == "/chat/completions"]), 1)

    def test_event_runner_records_final_tokenizer_failure_after_summary_calls(self) -> None:
        requests: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append({"path": self.path, "request": request})
                user = json.loads(request["messages"][-1]["content"])
                if self.path == "/tokenize" and user.get("evidence_compacted"):
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b"tokenizer temporarily unavailable")
                    return
                if self.path == "/tokenize":
                    response = {"count": 600_000 if "full_trajectory" in user else 10}
                else:
                    ids = [item["source_entry_id"] for item in user["segment_steps"]]
                    response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"summary": "Observed source segment.", "covered_step_ids": ids, "verbatim_evidence_step_ids": ["r1", "r2"]})}}], "usage": {"prompt_tokens": 10}}
                encoded = json.dumps(response).encode("utf-8")
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
                trace = {"source": {"canonical_instance_id": "source"}, "instruction": "task", "text_manager_eligible": True, "outcome": {}, "steps": [{"source_entry_id": "u", "role": "user"}, {"source_entry_id": "a1", "role": "assistant", "assistant": {"tool_calls": [{"tool_call_id": "c1"}]}}, {"source_entry_id": "r1", "role": "toolResult", "tool_result": {"tool_call_id": "c1"}}, {"source_entry_id": "a2", "role": "assistant", "assistant": {"tool_calls": [{"tool_call_id": "c2"}]}}, {"source_entry_id": "r2", "role": "toolResult", "tool_result": {"tool_call_id": "c2"}}]}
                trace_path = work / "trace.json"
                trace_path.write_text(json.dumps(trace), encoding="utf-8")
                sources = work / "sources.json"
                sources.write_text(json.dumps({"sources": [{"canonical_instance_id": "source", "normalized_path": str(trace_path)}]}), encoding="utf-8")
                prompt, compact, spec, decisions, config, ledger = (work / name for name in ("prompt.md", "compact.md", "spec.md", "decisions.md", "config.json", "ledger.json"))
                for path, content in ((prompt, "event prompt"), (compact, "summary prompt"), (spec, "文件版本：v0.11"), (decisions, "R014")):
                    path.write_text(content, encoding="utf-8")
                config.write_text(json.dumps({"services": {"deepseek_flash": {"base_url": f"http://127.0.0.1:{server.server_port}", "model_id": "fixture"}}}), encoding="utf-8")
                ledger.write_text(json.dumps({"schema_version": 1, "limit": 100, "calls": []}), encoding="utf-8")
                run_dir = work / "run"
                result = subprocess.run([sys.executable, str(root / "scripts" / "run_m2_r012_event_extraction.py"), "--run-dir", str(run_dir), "--source-manifest", str(sources), "--runtime-prompt", str(prompt), "--evidence-compaction-prompt", str(compact), "--spec", str(spec), "--decisions", str(decisions), "--execute-manager", "--activate-unlimited-development-ledger", "--config", str(config), "--ledger", str(ledger)], capture_output=True, text=True, env=self.env(), timeout=30)
                self.assertNotEqual(result.returncode, 0)
                failure = json.loads((run_dir / "run-status.json").read_text(encoding="utf-8"))["failure"]
            self.assertEqual(failure["classification"], "context_blocked_after_manager_summary_calls")
            self.assertEqual(failure["context_block_detail"]["phase"], "final_tokenizer_after_summaries")
            self.assertEqual(failure["context_block_detail"]["manager_call_count_before_block"], 1)
            self.assertEqual(failure["context_block_detail"]["state"], "tokenizer_unavailable")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(len([item for item in requests if item["path"] == "/chat/completions"]), 1)

    def test_event_runner_rejects_final_candidate_that_cites_omitted_raw_fragment(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                user = json.loads(request["messages"][-1]["content"])
                if self.path == "/tokenize":
                    response = {"count": 600_000 if "full_trajectory" in user else 10}
                elif "segment_steps" in user:
                    ids = [item["source_entry_id"] for item in user["segment_steps"]]
                    response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"summary": "Observed source segment.", "covered_step_ids": ids, "verbatim_evidence_step_ids": ["r2"]})}}], "usage": {"prompt_tokens": 10}}
                else:
                    response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"action": "generate", "skill": {"title": "Invalid omitted citation", "granularity": "event-driven", "when_to_apply": "After failure", "rules": ["Use observation."]}, "evidence": {"trigger_step_ids": ["r1"], "response_step_ids": ["a2"], "outcome_step_ids": ["r2"], "rule_evidence": [{"rule_index": 0, "step_ids": ["r1", "a2", "r2"]}]}})}}], "usage": {"prompt_tokens": 10}}
                encoded = json.dumps(response).encode("utf-8")
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
                trace = {"source": {"canonical_instance_id": "source"}, "instruction": "task", "text_manager_eligible": True, "outcome": {}, "steps": [{"source_entry_id": "u", "role": "user"}, {"source_entry_id": "a1", "role": "assistant", "assistant": {"tool_calls": [{"tool_call_id": "c1"}]}}, {"source_entry_id": "r1", "role": "toolResult", "tool_result": {"tool_call_id": "c1"}}, {"source_entry_id": "a2", "role": "assistant", "assistant": {"tool_calls": [{"tool_call_id": "c2"}]}}, {"source_entry_id": "r2", "role": "toolResult", "tool_result": {"tool_call_id": "c2"}}]}
                trace_path = work / "trace.json"
                trace_path.write_text(json.dumps(trace), encoding="utf-8")
                sources = work / "sources.json"
                sources.write_text(json.dumps({"sources": [{"canonical_instance_id": "source", "normalized_path": str(trace_path)}]}), encoding="utf-8")
                prompt, compact, spec, decisions, config, ledger = (work / name for name in ("prompt.md", "compact.md", "spec.md", "decisions.md", "config.json", "ledger.json"))
                for path, content in ((prompt, "event prompt"), (compact, "summary prompt"), (spec, "文件版本：v0.11"), (decisions, "R014")):
                    path.write_text(content, encoding="utf-8")
                config.write_text(json.dumps({"services": {"deepseek_flash": {"base_url": f"http://127.0.0.1:{server.server_port}", "model_id": "fixture"}}}), encoding="utf-8")
                ledger.write_text(json.dumps({"schema_version": 1, "limit": 100, "calls": []}), encoding="utf-8")
                run_dir = work / "run"
                result = subprocess.run([sys.executable, str(root / "scripts" / "run_m2_r012_event_extraction.py"), "--run-dir", str(run_dir), "--source-manifest", str(sources), "--runtime-prompt", str(prompt), "--evidence-compaction-prompt", str(compact), "--spec", str(spec), "--decisions", str(decisions), "--execute-manager", "--activate-unlimited-development-ledger", "--config", str(config), "--ledger", str(ledger)], capture_output=True, text=True, env=self.env(), timeout=30)
                self.assertNotEqual(result.returncode, 0)
                failure = json.loads((run_dir / "run-status.json").read_text(encoding="utf-8"))["failure"]
                self.assertFalse((run_dir / "extraction" / "event" / "source" / "initial-01.json").exists())
                response = json.loads((run_dir / "model_calls" / "call-0002" / "response.json").read_text(encoding="utf-8"))
            self.assertEqual(failure["classification"], "post_call_validation_or_runner_failure")
            self.assertIn("absent from supplied original fragments", failure["error"])
            self.assertEqual(response["http_status"], 200)
        finally:
            server.shutdown()
            server.server_close()

    def test_event_runner_uses_full_at_and_below_exact_boundary_then_compacts_only_over(self) -> None:
        requests: list[dict] = []
        counts = {"under": 511_999, "equal": 512_000, "over": 512_001}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append({"path": self.path, "request": request})
                system = request["messages"][0]["content"]
                mode = next(value for value in counts if value in system)
                user = json.loads(request["messages"][-1]["content"])
                if self.path == "/tokenize":
                    response = {"count": counts[mode] if "full_trajectory" in user else 10}
                elif "segment_steps" in user:
                    ids = [item["source_entry_id"] for item in user["segment_steps"]]
                    response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"summary": "Observed segment.", "covered_step_ids": ids, "verbatim_evidence_step_ids": ["r"]})}}], "usage": {"prompt_tokens": 10}}
                else:
                    response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"action": "skip", "reason": "fixture stop"})}}], "usage": {"prompt_tokens": counts[mode] if "full_trajectory" in user else 10}}
                encoded = json.dumps(response).encode("utf-8")
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
                trace = {"source": {"canonical_instance_id": "source"}, "instruction": "task", "text_manager_eligible": True, "outcome": {}, "steps": [{"source_entry_id": "u", "role": "user"}, {"source_entry_id": "a", "role": "assistant", "assistant": {"tool_calls": [{"tool_call_id": "c"}]}}, {"source_entry_id": "r", "role": "toolResult", "tool_result": {"tool_call_id": "c"}}]}
                trace_path = work / "trace.json"
                trace_path.write_text(json.dumps(trace), encoding="utf-8")
                sources = work / "sources.json"
                sources.write_text(json.dumps({"sources": [{"canonical_instance_id": "source", "normalized_path": str(trace_path)}]}), encoding="utf-8")
                compact, spec, decisions, config = (work / name for name in ("compact.md", "spec.md", "decisions.md", "config.json"))
                for path, content in ((compact, "summary prompt"), (spec, "文件版本：v0.11"), (decisions, "R014")):
                    path.write_text(content, encoding="utf-8")
                config.write_text(json.dumps({"services": {"deepseek_flash": {"base_url": f"http://127.0.0.1:{server.server_port}", "model_id": "fixture"}}}), encoding="utf-8")
                outcomes: dict[str, dict] = {}
                for mode in counts:
                    prompt, ledger, run_dir = work / f"{mode}.md", work / f"{mode}-ledger.json", work / f"{mode}-run"
                    prompt.write_text(f"event prompt {mode}", encoding="utf-8")
                    ledger.write_text(json.dumps({"schema_version": 1, "limit": 100, "calls": []}), encoding="utf-8")
                    result = subprocess.run([sys.executable, str(root / "scripts" / "run_m2_r012_event_extraction.py"), "--run-dir", str(run_dir), "--source-manifest", str(sources), "--runtime-prompt", str(prompt), "--evidence-compaction-prompt", str(compact), "--spec", str(spec), "--decisions", str(decisions), "--execute-manager", "--activate-unlimited-development-ledger", "--config", str(config), "--ledger", str(ledger)], capture_output=True, text=True, env=self.env(), timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    outcomes[mode] = json.loads((run_dir / "extraction" / "event" / "source" / "initial-01.json").read_text(encoding="utf-8"))
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(outcomes["under"]["trajectory_input_mode"], "full")
        self.assertEqual(outcomes["equal"]["trajectory_input_mode"], "full")
        self.assertEqual(outcomes["over"]["trajectory_input_mode"], "evidence_compacted")

    def test_evolution_audit_runner_excludes_unsupplied_skills(self) -> None:
        root = Path(__file__).resolve().parents[1]
        block = "[CODESKILL EVENT PRIOR KNOWLEDGE]\n" + render_skill(EVENT)
        record = {
            "trial_id": "trial-entry",
            "attempt_ordinal": 1,
            "forwarded_request_ordinal": 1,
            "proxy_outcome": "stream_forwarded",
            "forwarded_request": {"messages": [{"role": "user", "content": block}]},
            "event_selection": [{"injected_skills": [{"skill": EVENT, "block_sha256": __import__("hashlib").sha256(block.encode()).hexdigest()}]}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            attempt = work / "attempt.json"
            attempt.write_text(json.dumps(record), encoding="utf-8")
            output = work / "evolution.json"
            result = subprocess.run(
                [sys.executable, str(root / "scripts" / "audit_m3_r012_supplied_evolution.py"), "--trial-id", "trial-entry", "--attempt-record", str(attempt), "--output", str(output)],
                capture_output=True,
                text=True,
                env=self.env(),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            audit = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual([item["skill"]["skill_id"] for item in audit["candidates"]], ["event-a"])

    def test_instance_freeze_runner_writes_distinct_arm_repeat_snapshots(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            banks: list[Path] = []
            for arm in ("extraction", "full"):
                path = work / f"{arm}.json"
                SkillBank.empty("terminal-bench").save(path)
                banks.append(path)
            output = work / "freeze.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts" / "plan_m3_r012_instance_freeze.py"),
                    "--instance-id",
                    "terminal-bench/target",
                    "--repeat",
                    "one",
                    "--repeat",
                    "two",
                    "--arm-bank",
                    f"extraction={banks[0]}",
                    "--arm-bank",
                    f"full={banks[1]}",
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                env=self.env(),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            freeze = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(len(freeze["assignments"]), 4)
        self.assertEqual({item["arm"] for item in freeze["assignments"]}, {"extraction", "full"})

    def test_durable_lifecycle_runner_releases_evidenced_no_supplied_full_arm_without_manager(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            profile = {
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
            profile_path = work / "profile.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            bank = work / "full.json"
            SkillBank.empty("terminal-bench").save(bank)
            spec = work / "REPRODUCTION_SPEC.md"
            spec.write_text("文件版本：v0.11", encoding="utf-8")
            decisions = work / "RESEARCH_DECISIONS.md"
            decisions.write_text("R012", encoding="utf-8")
            state = work / "lifecycle.json"
            freeze = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts" / "run_m3_r012_lifecycle.py"),
                    "freeze",
                    "--state",
                    str(state),
                    "--profile",
                    str(profile_path),
                    "--instance-id",
                    "terminal-bench/target",
                    "--repeat",
                    "one",
                    "--arm-bank",
                    f"full={bank}",
                    "--spec",
                    str(spec),
                    "--decisions",
                    str(decisions),
                ],
                capture_output=True,
                text=True,
                env=self.env(),
            )
            self.assertEqual(freeze.returncode, 0, freeze.stderr)
            result_evidence = work / "result.json"
            result_evidence.write_text(json.dumps({"status": "fixture"}), encoding="utf-8")
            trial_id = "target:full:one"
            no_supplied_attempt = work / "no-supplied-attempt.json"
            no_supplied_attempt.write_text(
                json.dumps(
                    {
                        "trial_id": trial_id,
                        "attempt_ordinal": 1,
                        "forwarded_request_ordinal": 1,
                        "proxy_outcome": "stream_forwarded",
                        "forwarded_request": {"messages": [{"role": "user", "content": "fixture without injected skills"}]},
                        "event_selection": [],
                    }
                ),
                encoding="utf-8",
            )
            wrong_trace = work / "wrong-trace.json"
            wrong_trace.write_text(json.dumps({"source": {"canonical_instance_id": "other"}, "steps": []}), encoding="utf-8")
            rejected = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts" / "run_m3_r012_lifecycle.py"),
                    "finish",
                    "--state",
                    str(state),
                    "--trial-id",
                    trial_id,
                    "--result-evidence",
                    str(result_evidence),
                    "--trajectory-evidence",
                    str(wrong_trace),
                ],
                capture_output=True,
                text=True,
                env=self.env(),
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse((work / "lifecycle-evidence").exists())
            finished = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts" / "run_m3_r012_lifecycle.py"),
                    "finish",
                    "--state",
                    str(state),
                    "--trial-id",
                    trial_id,
                    "--result-evidence",
                    str(result_evidence),
                    "--proxy-attempt",
                    str(no_supplied_attempt),
                ],
                capture_output=True,
                text=True,
                env=self.env(),
            )
            self.assertEqual(finished.returncode, 0, finished.stderr)
            result_evidence.write_text(json.dumps({"status": "overwrite-attempt"}), encoding="utf-8")
            duplicate_finish = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts" / "run_m3_r012_lifecycle.py"),
                    "finish",
                    "--state",
                    str(state),
                    "--trial-id",
                    trial_id,
                    "--result-evidence",
                    str(result_evidence),
                ],
                capture_output=True,
                text=True,
                env=self.env(),
            )
            self.assertNotEqual(duplicate_finish.returncode, 0)
            state_after_finish = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(state_after_finish["coordinator"]["instances"]["target"]["assignments"][trial_id]["result_evidence"]["trial_result_value"]["status"], "fixture")
            manual_skip = work / "manual-skip.json"
            manual_skip.write_text(
                json.dumps(
                    {
                        "kind": "r012_evolution_selection_manifest",
                        "instance_id": "target",
                        "profile_sha256": profile_sha256(profile),
                        "release_order": [trial_id],
                        "selections": [{"trial_id": trial_id, "action": "skip", "reason": "manual bypass attempt"}],
                    }
                ),
                encoding="utf-8",
            )
            rejected_release = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts" / "run_m3_r012_lifecycle.py"),
                    "release",
                    "--state",
                    str(state),
                    "--instance-id",
                    "target",
                    "--selection-manifest",
                    str(manual_skip),
                ],
                capture_output=True,
                text=True,
                env=self.env(),
            )
            self.assertNotEqual(rejected_release.returncode, 0)
            self.assertEqual(
                json.loads(state.read_text(encoding="utf-8"))["coordinator"]["instances"]["target"]["release_state"],
                "pending",
            )
            selection = work / "selection.json"
            selection.write_text(
                json.dumps(
                    {
                        "kind": "r012_evolution_selection_manifest",
                        "instance_id": "target",
                        "profile_sha256": profile_sha256(profile),
                        "release_order": [trial_id],
                        "selections": [{"trial_id": trial_id, "action": "evaluate_all_supplied", "reason": "no supplied skill in this fixture"}],
                    }
                ),
                encoding="utf-8",
            )
            released = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts" / "run_m3_r012_lifecycle.py"),
                    "release",
                    "--state",
                    str(state),
                    "--instance-id",
                    "target",
                    "--selection-manifest",
                    str(selection),
                ],
                capture_output=True,
                text=True,
                env=self.env(),
            )
            self.assertEqual(released.returncode, 0, released.stderr)
            value = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(value["last_action"]["status"], "released")
        self.assertEqual(value["coordinator"]["instances"]["target"]["release_state"], "released")

    def test_no_related_group_audit_entrypoint_preserves_raw_description_review_packet(self) -> None:
        root = Path(__file__).resolve().parents[1]
        trace = {
            "source": {"canonical_instance_id": "anchor"},
            "steps": [
                {"source_entry_id": "u", "role": "user", "content": "task"},
                {"source_entry_id": "a", "role": "assistant", "content": "action"},
            ],
        }
        candidate_trace = {**trace, "source": {"canonical_instance_id": "candidate"}}
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            anchor_trace = work / "anchor-trace.json"
            anchor_desc = work / "anchor-desc.json"
            candidate_trace_path = work / "candidate-trace.json"
            candidate_desc = work / "candidate-desc.json"
            pair = work / "pair.json"
            for path, value in (
                (anchor_trace, trace),
                (anchor_desc, {"task_family": "review", "source_step_ids": ["a"]}),
                (candidate_trace_path, candidate_trace),
                (candidate_desc, {"task_family": "recovery", "source_step_ids": ["u"]}),
                (pair, {"action": "no_related_group", "reason": "short descriptions do not establish one reusable procedure"}),
            ):
                path.write_text(json.dumps(value), encoding="utf-8")
            manifest = work / "candidates.json"
            manifest.write_text(
                json.dumps({"candidates": [{"trace_path": str(candidate_trace_path), "description_path": str(candidate_desc)}]}),
                encoding="utf-8",
            )
            output = work / "audit.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts" / "audit_m2_r012_no_related_group.py"),
                    "--anchor-trace",
                    str(anchor_trace),
                    "--anchor-description",
                    str(anchor_desc),
                    "--pairing-result",
                    str(pair),
                    "--candidate-manifest",
                    str(manifest),
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                env=self.env(),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            audit = json.loads(output.read_text(encoding="utf-8"))
        self.assertIsNone(audit["automated_classification"])
        self.assertEqual(audit["anchor"]["description_cited_steps"][0]["source_entry_id"], "a")


if __name__ == "__main__":
    unittest.main()
