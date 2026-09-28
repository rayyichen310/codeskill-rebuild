from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codeskill_rebuild.manager import ManagerClient, ManagerProfile
from codeskill_rebuild.manager_reconciliation import (
    ManagerReconciliationError,
    audit_saved_manager_call,
    load_reconciliation_manifest,
)
from codeskill_rebuild.types import canonical_json, sha256_file, sha256_text, write_json
from scripts.run_r015_c_only_harbor_driver import _reconciled_manager_call


class _ModelFreeCounter:
    method = "controlled_complete_request_counter"

    def __init__(self, count: int) -> None:
        self.count = count
        self.calls: list[dict] = []
        self.last_exchange: dict | None = None

    def __call__(self, messages: list[dict], *, request_options: dict | None = None) -> int:
        self.calls.append({"messages": messages, "request_options": dict(request_options or {})})
        self.last_exchange = {
            "endpoint": "http://example.invalid/v1/tokenize",
            "request": {"messages": messages, "add_generation_prompt": True, **dict(request_options or {})},
            "http_status": 200,
            "raw_response": json.dumps({"count": self.count}),
            "parsed_response": {"count": self.count},
            "count": self.count,
        }
        return self.count


class ManagerReconciliationTest(unittest.TestCase):
    def _fixture(self, root: Path, *, usage_tokens: int = 10) -> dict[str, Path | dict]:
        task_id = "controlled-task"
        trial_id = "r1:C:controlled-task"
        session_id = "session-controlled-1"
        purpose = f"r015_c_only_event_extraction:{trial_id}:001"
        session_path = root / "session.jsonl"
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_text('{"session_id":"session-controlled-1"}\n', encoding="utf-8")
        trace_path = root / "trajectory-live.json"
        trace = {
            "source": {
                "canonical_instance_id": task_id,
                "instance_id": task_id,
                "task_name": f"terminal-bench/{task_id}",
                "session_id": session_id,
                "session_path": str(session_path),
                "session_sha256": sha256_file(session_path),
            },
            "instruction": "Perform the controlled task.",
            "steps": [
                {"source_entry_id": "initial", "role": "user", "content": "task"},
                {"source_entry_id": "observed", "role": "toolResult", "content": "observation"},
                {"source_entry_id": "response", "role": "assistant", "content": "response"},
                {"source_entry_id": "outcome", "role": "toolResult", "content": "outcome"},
            ],
            "outcome": {"reward": 1.0, "status": "completed"},
            "text_manager_eligible": True,
            "r015_binding": {"round_id": 1, "task_id": task_id, "trial_id": trial_id, "session_id": session_id},
        }
        write_json(trace_path, trace)
        messages = [
            {"role": "system", "content": "Extract one event or skip."},
            {"role": "user", "content": '{"source":"controlled"}'},
        ]
        options = {
            "model": "deepseek-ai/DeepSeek-V4-Flash",
            "temperature": 0.0,
            "max_tokens": 8192,
            "response_format": {"type": "json_object"},
            "reasoning_effort": "max",
        }
        request_path = root / "request.json"
        write_json(
            request_path,
            {
                "kind": "live_manager_call",
                "purpose": purpose,
                "call_metadata": {"task_id": task_id, "condition": "C-only"},
                "request": {"messages": messages, **options},
            },
        )
        response_path = root / "response.json"
        response_value = {
            "kind": "live_manager_call",
            "purpose": purpose,
            "http_status": 200,
            "classification": "tokenizer_prompt_count_mismatch",
            "finish_reason": "stop",
            "usage": {"prompt_tokens": usage_tokens, "completion_tokens": 2, "total_tokens": usage_tokens + 2},
            "parsed_response": {
                "choices": [{"message": {"content": '{"action":"skip","reason":"no reusable event"}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": usage_tokens},
            },
        }
        write_json(response_path, response_value)
        journal_path = root / "journal.json"
        write_json(
            journal_path,
            {
                "kind": "r012_pre_call_journal",
                "trial_id": trial_id,
                "phase": "event-001",
                "purpose": purpose,
                "status": "manager_call_raised",
                "messages": messages,
                "messages_sha256": sha256_text(canonical_json(messages)),
            },
        )
        ledger_path = root / "ledger.json"
        write_json(
            ledger_path,
            {
                "schema_version": 2,
                "limit": "unlimited",
                "calls": [
                    {
                        "run_dir": str(root / "manager"),
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
        return {
            "request": request_path,
            "response": response_path,
            "journal": journal_path,
            "ledger": ledger_path,
            "trace": trace_path,
            "trial_id": trial_id,
            "task_id": task_id,
            "round_id": 1,
            "session_id": session_id,
            "purpose": purpose,
        }

    def test_audit_reuses_complete_saved_shape_without_changing_originals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._fixture(root)
            original_hashes = {key: sha256_file(fixture[key]) for key in ("request", "response", "journal", "ledger", "trace")}
            output = root / "reconciliation.json"
            exchange = root / "tokenizer-exchange.json"
            counter = _ModelFreeCounter(10)
            manifest = audit_saved_manager_call(
                request_path=fixture["request"],
                response_path=fixture["response"],
                journal_path=fixture["journal"],
                ledger_path=fixture["ledger"],
                trace_path=fixture["trace"],
                expected={
                    "call_id": "call-0001",
                    "phase": "event-001",
                    "purpose": fixture["purpose"],
                    "round_id": fixture["round_id"],
                    "task_id": fixture["task_id"],
                    "trial_id": fixture["trial_id"],
                    "session_id": fixture["session_id"],
                },
                tokenizer=counter,
                output_path=output,
                tokenizer_exchange_path=exchange,
                reconciliation_run_dir=root,
            )
            loaded = load_reconciliation_manifest(output)
            unchanged = {key: sha256_file(fixture[key]) for key in original_hashes}
            self.assertNotEqual(loaded["original"]["ledger"]["path"], str(fixture["ledger"]))
            self.assertEqual(loaded["original"]["ledger"]["path"], loaded["original"]["ledger_snapshot"]["path"])
            self.assertTrue(Path(loaded["original"]["ledger"]["path"]).is_file())
        self.assertEqual(manifest["status"], "ready_for_explicit_manager_phase_resume")
        self.assertEqual(loaded["corrected_preflight"]["estimated_input_tokens"], 10)
        self.assertEqual(loaded["response"]["action"], "skip")
        self.assertEqual(counter.calls[0]["request_options"]["reasoning_effort"], "max")
        self.assertTrue(loaded["resume"]["harbor_rerun"] is False)
        self.assertTrue(loaded["resume"]["original_files_immutable"])
        self.assertEqual(unchanged, original_hashes)

    def test_audit_rejects_a_corrected_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._fixture(root, usage_tokens=10)
            with self.assertRaisesRegex(ManagerReconciliationError, "differs from saved chat usage"):
                audit_saved_manager_call(
                    request_path=fixture["request"],
                    response_path=fixture["response"],
                    journal_path=fixture["journal"],
                    ledger_path=fixture["ledger"],
                    trace_path=fixture["trace"],
                    expected={
                        "call_id": "call-0001",
                        "phase": "event-001",
                        "purpose": fixture["purpose"],
                        "round_id": fixture["round_id"],
                        "task_id": fixture["task_id"],
                        "trial_id": fixture["trial_id"],
                        "session_id": fixture["session_id"],
                    },
                    tokenizer=_ModelFreeCounter(9),
                    output_path=root / "reconciliation.json",
                    tokenizer_exchange_path=root / "tokenizer-exchange.json",
                    reconciliation_run_dir=root,
                )

    def test_driver_reuses_audited_call_in_a_distinct_journal_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._fixture(root / "source")
            run_dir = root / "run"
            manager_dir = run_dir / "manager"
            call_dir = manager_dir / "model_calls" / "call-0001"
            call_dir.mkdir(parents=True)
            request_path = call_dir / "request.json"
            response_path = call_dir / "response.json"
            shutil.copy2(source["request"], request_path)
            shutil.copy2(source["response"], response_path)
            trace_path = run_dir / "trajectory-live.json"
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source["trace"], trace_path)
            original_journal = run_dir / "manager-journals" / str(source["task_id"]) / sha256_text(str(source["trial_id"]))[:20] / "event-001.json"
            original_journal.parent.mkdir(parents=True)
            shutil.copy2(source["journal"], original_journal)
            ledger_path = run_dir / "manager-ledger.json"
            write_json(
                ledger_path,
                {
                    "schema_version": 2,
                    "limit": "unlimited",
                    "calls": [
                        {
                            "run_dir": str(manager_dir),
                            "call_id": "call-0001",
                            "purpose": source["purpose"],
                            "status": "tokenizer_mismatch",
                            "http_status": 200,
                            "finish_reason": "stop",
                            "classification": "tokenizer_prompt_count_mismatch",
                            "response_path": str(response_path),
                        }
                    ],
                },
            )
            reconciliation_path = run_dir / "reconciliation.json"
            exchange_path = run_dir / "tokenizer-exchange.json"
            audit_saved_manager_call(
                request_path=request_path,
                response_path=response_path,
                journal_path=original_journal,
                ledger_path=ledger_path,
                trace_path=trace_path,
                expected={
                    "call_id": "call-0001",
                    "phase": "event-001",
                    "purpose": source["purpose"],
                    "round_id": source["round_id"],
                    "task_id": source["task_id"],
                    "trial_id": source["trial_id"],
                    "session_id": source["session_id"],
                },
                tokenizer=_ModelFreeCounter(10),
                output_path=reconciliation_path,
                tokenizer_exchange_path=exchange_path,
                reconciliation_run_dir=run_dir,
            )
            reconciliation = load_reconciliation_manifest(reconciliation_path)
            # The active run ledger is intentionally append-only.  Adding a
            # later manager call must not invalidate the immutable audit, but
            # changing the preserved call itself must fail closed.
            live_ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            live_ledger["calls"].append(
                {
                    "run_dir": str(manager_dir),
                    "call_id": "call-0002",
                    "purpose": "later-phase",
                    "status": "succeeded",
                }
            )
            write_json(ledger_path, live_ledger)
            appended = load_reconciliation_manifest(reconciliation_path)
            self.assertEqual(appended["original"]["live_ledger"]["path"], str(ledger_path))
            original_hash = sha256_file(original_journal)
            manager = ManagerClient(
                ManagerProfile(base_url="http://example.invalid/v1", model="deepseek-ai/DeepSeek-V4-Flash", max_total_calls=None),
                manager_dir,
                {},
                ledger_path,
                exact_token_counter=None,
            )
            executor = SimpleNamespace(manager=manager, journal_root=run_dir / "manager-journals" / str(source["task_id"]))
            request_record = json.loads(request_path.read_text(encoding="utf-8"))
            call, journal, invalid = _reconciled_manager_call(
                executor=executor,
                context={
                    "assignment": {
                        "round_id": source["round_id"],
                        "task_id": source["task_id"],
                        "trial_id": source["trial_id"],
                    },
                    "session_id": source["session_id"],
                },
                phase="event-001",
                purpose=source["purpose"],
                messages=request_record["request"]["messages"],
                reconciliation=reconciliation,
                reconciliation_path=reconciliation_path,
            )
            self.assertIsNone(invalid)
            self.assertEqual(call["call_id"], "call-0001")
            self.assertEqual(manager.call_count, 1)
            self.assertEqual(sha256_file(original_journal), original_hash)
            self.assertTrue(Path(journal["path"]).is_file())
            self.assertNotEqual(Path(journal["path"]).resolve(), original_journal.resolve())
            self.assertEqual(Path(journal["path"]).parent.parent.parent.name, "manager-journals-reconciliation")
            # A byte change to call-0001 is different from an append and is
            # rejected even though the immutable snapshot still exists.
            changed = json.loads(ledger_path.read_text(encoding="utf-8"))
            changed["calls"][0]["status"] = "succeeded"
            write_json(ledger_path, changed)
            with self.assertRaisesRegex(RuntimeError, "active ledger no longer matches"):
                _reconciled_manager_call(
                    executor=executor,
                    context={
                        "assignment": {
                            "round_id": source["round_id"],
                            "task_id": source["task_id"],
                            "trial_id": source["trial_id"],
                        },
                        "session_id": source["session_id"],
                    },
                    phase="event-001",
                    purpose=source["purpose"],
                    messages=request_record["request"]["messages"],
                    reconciliation=reconciliation,
                    reconciliation_path=reconciliation_path,
                )
