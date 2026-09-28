from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codeskill_rebuild.r015_harbor_evidence import (
    HarborTrialEvidenceError,
    import_harbor_openclaw_trial,
    non_forwarded_terminal_disposition,
    non_forwarded_terminal_schema,
)
from codeskill_rebuild.types import read_json


def make_trial(root: Path) -> Path:
    trial = root / "harbor" / "trial"
    (trial / "agent").mkdir(parents=True)
    (trial / "verifier").mkdir()
    (trial / "config.json").write_text(
        json.dumps(
            {
                "task": {"name": "terminal-bench/password-recovery"},
                "agents": [{"kwargs": {"session_id": "session-fixture"}}],
            }
        ),
        encoding="utf-8",
    )
    (trial / "agent" / "instruction.txt").write_text("Repair the password recovery service.", encoding="utf-8")
    events = [
        {"type": "session", "id": "session-fixture"},
        {"type": "message", "id": "u", "parentId": None, "timestamp": "t", "message": {"role": "user", "content": "Repair it."}},
        {"type": "message", "id": "a", "parentId": "u", "timestamp": "t", "message": {"role": "assistant", "content": "Done", "stopReason": "stop"}},
    ]
    (trial / "agent" / "openclaw.session.jsonl").write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    (trial / "result.json").write_text(json.dumps({"status": "completed", "reward": 1}), encoding="utf-8")
    (trial / "verifier" / "reward.txt").write_text("1\n", encoding="utf-8")
    return trial


def make_local_harbor_trial(root: Path) -> Path:
    trial = root / "local-harbor" / "trial"
    (trial / "agent").mkdir(parents=True)
    (trial / "verifier").mkdir()
    (trial / "config.json").write_text(
        json.dumps(
            {
                "task": {
                    "name": None,
                    "path": "/home/runner/terminal-bench-2-1/tasks/password-recovery",
                },
                "agents": [{"kwargs": {"session_id": "session-fixture"}}],
            }
        ),
        encoding="utf-8",
    )
    (trial / "agent" / "instruction.txt").write_text("Repair the password recovery service.", encoding="utf-8")
    events = [
        {"type": "session", "id": "session-fixture"},
        {"type": "message", "id": "u", "parentId": None, "timestamp": "t", "message": {"role": "user", "content": "Repair it."}},
        {"type": "message", "id": "a", "parentId": "u", "timestamp": "t", "message": {"role": "assistant", "content": "Done", "stopReason": "stop"}},
    ]
    (trial / "agent" / "openclaw.session.jsonl").write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    (trial / "result.json").write_text(
        json.dumps({"status": "completed", "task_name": "terminal-bench/password-recovery"}), encoding="utf-8"
    )
    (trial / "verifier" / "reward.txt").write_text("1\n", encoding="utf-8")
    return trial


def make_sidecar(root: Path, *, trial_id: str) -> Path:
    evidence = root / "sidecar"
    (evidence / "upstream_requests").mkdir(parents=True)
    (evidence / "upstream_requests" / "attempt-0001.json").write_text(
        json.dumps(
            {
                "trial_id": trial_id,
                "attempt_ordinal": 1,
                "proxy_outcome": "stream_forwarded",
                "normal_call_boundary": {
                    "kind": "r012_public_plugin_normal_call_boundary",
                    "trial_id": trial_id,
                    "session_id": "session-fixture",
                },
            }
        ),
        encoding="utf-8",
    )
    return evidence


def make_non_forwarded_input_limit_attempt(*, trial_id: str) -> dict:
    return {
        "schema_version": 3,
        "kind": "r012_actual_upstream_request",
        "trial_id": trial_id,
        "attempt_ordinal": 2,
        "forwarded_request_ordinal": 2,
        "native_request": {"model": "fixture", "messages": [{"role": "user", "content": "native"}]},
        "preflight_candidate_forwarded_request": {
            "model": "fixture",
            "messages": [{"role": "user", "content": "candidate"}],
        },
        "exact_forwarded_input_tokens": 11,
        "max_input_tokens": 10,
        "token_count_scope": "complete_forwarded_openai_payload",
        "uncommitted_selection": {"event": {"decision": "injected_pending_budget"}},
        "outcome": "context_or_overlay_error",
        "error_type": "OverlayInputLimitError",
        "error": "forwarded input 11 exceeds configured input budget 10",
        "state_path": "/tmp/fixture-overlay-state.json",
    }


def make_non_forwarded_event_budget_attempt(*, trial_id: str) -> dict:
    return {
        "schema_version": 3,
        "kind": "r012_actual_upstream_request",
        "trial_id": trial_id,
        "attempt_ordinal": 2,
        "forwarded_request_ordinal": 2,
        "native_request": {"model": "fixture", "messages": [{"role": "user", "content": "native"}]},
        "event_skill_token_budget": {"active_event_block_tokens": 51, "budget": 50},
        "uncommitted_selection": {"event": {"decision": "injected_pending_budget"}},
        "outcome": "event_skill_budget_error",
        "error_type": "OverlayEventSkillBudgetError",
        "error": "active event skill blocks require 51 complete-payload tokens above frozen event budget 50",
        "state_path": "/tmp/fixture-overlay-state.json",
    }


def make_proxy_limit_rejection(*, trial_id: str) -> dict:
    return {
        "schema_version": 2,
        "kind": "r008_proxy_limit_rejection",
        "trial_id": trial_id,
        "attempt_ordinal": 2,
        "forwarded_request_ordinal": 2,
        "native_request": {"model": "fixture", "messages": [{"role": "user", "content": "native"}]},
        "proxy_outcome": "request_limit_rejected",
        "error_code": "codeskill_request_limit_reached",
        "error": "CODESKILL forwarded solver request limit reached",
        "state_path": "/tmp/fixture-overlay-state.json",
    }


class HarborTrialEvidenceTest(unittest.TestCase):
    def test_imports_official_trace_reward_and_same_trial_proxy_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet = root / "packet"
            manifest = import_harbor_openclaw_trial(
                trial_id="password-recovery:C:development",
                instance_id="password-recovery",
                harbor_trial_dir=make_trial(root),
                sidecar_evidence_dir=make_sidecar(root, trial_id="password-recovery:C:development"),
                output_dir=packet,
            )
            self.assertEqual(manifest["official_reward"], "1")
            self.assertTrue((packet / "raw-harbor" / "agent" / "openclaw.session.jsonl").is_file())
            self.assertEqual(read_json(packet / "trajectory-evidence.json")["source"]["canonical_instance_id"], "password-recovery")
            self.assertEqual(read_json(packet / "trial-result.json")["trial_id"], "password-recovery:C:development")
            self.assertEqual(read_json(packet / "proxy-attempts" / "attempt-0001.json")["trial_id"], "password-recovery:C:development")

    def test_imports_harbor_local_task_using_result_name_when_config_name_is_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = import_harbor_openclaw_trial(
                trial_id="password-recovery:C:development",
                instance_id="password-recovery",
                harbor_trial_dir=make_local_harbor_trial(root),
                sidecar_evidence_dir=make_sidecar(root, trial_id="password-recovery:C:development"),
                output_dir=root / "packet",
            )
            self.assertEqual(manifest["official_reward"], "1")
            trace = read_json(root / "packet" / "trajectory-evidence.json")
            self.assertEqual(trace["source"]["task_name"], "terminal-bench/password-recovery")
            self.assertEqual(trace["source"]["canonical_instance_id"], "password-recovery")

    def test_rejects_disagreeing_local_task_path_and_result_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial = make_local_harbor_trial(root)
            result = json.loads((trial / "result.json").read_text(encoding="utf-8"))
            result["task_name"] = "terminal-bench/portfolio-optimization"
            (trial / "result.json").write_text(json.dumps(result), encoding="utf-8")
            with self.assertRaisesRegex(HarborTrialEvidenceError, "task name|task identity"):
                import_harbor_openclaw_trial(
                    trial_id="password-recovery:C:development",
                    instance_id="password-recovery",
                    harbor_trial_dir=trial,
                    sidecar_evidence_dir=make_sidecar(root, trial_id="password-recovery:C:development"),
                    output_dir=root / "packet",
                )

    def test_imports_sqlite_session_when_older_adapter_did_not_write_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial = make_local_harbor_trial(root)
            session = trial / "agent" / "openclaw.session.jsonl"
            session.unlink()
            database = trial / "agent" / "codeskill-openclaw-state" / "openclaw-agent.sqlite"
            database.parent.mkdir()
            connection = sqlite3.connect(database)
            try:
                connection.execute("create table transcript_events (session_id text, seq integer, event_json text)")
                events = [
                    {"type": "session", "id": "session-fixture"},
                    {"type": "message", "id": "u", "parentId": None, "message": {"role": "user", "content": "Repair it."}},
                    {"type": "message", "id": "a", "parentId": "u", "message": {"role": "assistant", "content": "Done"}},
                ]
                connection.executemany(
                    "insert into transcript_events values (?, ?, ?)",
                    [("session-fixture", index, json.dumps(event)) for index, event in enumerate(events)],
                )
                connection.commit()
            finally:
                connection.close()
            packet = root / "packet"
            manifest = import_harbor_openclaw_trial(
                trial_id="password-recovery:C:development",
                instance_id="password-recovery",
                harbor_trial_dir=trial,
                sidecar_evidence_dir=make_sidecar(root, trial_id="password-recovery:C:development"),
                output_dir=packet,
                session_source_path=database,
            )
            self.assertEqual(manifest["session_derivation"]["event_count"], 3)
            self.assertTrue(Path(manifest["session_derivation"]["derived_path"]).is_file())
            trace = read_json(packet / "trajectory-evidence.json")
            self.assertEqual(trace["source"]["session_source_kind"], "sqlite_transcript_events_derived_session_jsonl")
            self.assertTrue((packet / "raw-harbor" / "agent" / "codeskill-openclaw-state" / "openclaw-agent.sqlite").is_file())

    def test_rejects_cross_session_sqlite_source_against_config_and_public_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial = make_local_harbor_trial(root)
            (trial / "agent" / "openclaw.session.jsonl").unlink()
            database = trial / "agent" / "codeskill-openclaw-state" / "openclaw-agent.sqlite"
            database.parent.mkdir()
            connection = sqlite3.connect(database)
            try:
                connection.execute("create table transcript_events (session_id text, seq integer, event_json text)")
                events = [
                    {"type": "session", "id": "foreign-session"},
                    {"type": "message", "id": "u", "parentId": None, "message": {"role": "user", "content": "Repair it."}},
                ]
                connection.executemany(
                    "insert into transcript_events values (?, ?, ?)",
                    [("foreign-session", index, json.dumps(event)) for index, event in enumerate(events)],
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(HarborTrialEvidenceError, "session binding mismatch"):
                import_harbor_openclaw_trial(
                    trial_id="password-recovery:C:development",
                    instance_id="password-recovery",
                    harbor_trial_dir=trial,
                    sidecar_evidence_dir=make_sidecar(root, trial_id="password-recovery:C:development"),
                    output_dir=root / "cross-session",
                    session_source_path=database,
                )

    def test_rejects_cross_session_regular_jsonl_against_config_and_public_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial = make_trial(root)
            session = trial / "agent" / "openclaw.session.jsonl"
            text = session.read_text(encoding="utf-8").replace('"session-fixture"', '"foreign-session"', 1)
            session.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(HarborTrialEvidenceError, "session binding mismatch"):
                import_harbor_openclaw_trial(
                    trial_id="password-recovery:C:development",
                    instance_id="password-recovery",
                    harbor_trial_dir=trial,
                    sidecar_evidence_dir=make_sidecar(root, trial_id="password-recovery:C:development"),
                    output_dir=root / "cross-session-jsonl",
                )

    def test_rejects_normal_call_boundary_from_a_different_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial = make_trial(root)
            sidecar = make_sidecar(root, trial_id="password-recovery:C:development")
            attempt = sidecar / "upstream_requests" / "attempt-0001.json"
            value = json.loads(attempt.read_text(encoding="utf-8"))
            value["normal_call_boundary"]["session_id"] = "foreign-session"
            attempt.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(HarborTrialEvidenceError, "session binding mismatch"):
                import_harbor_openclaw_trial(
                    trial_id="password-recovery:C:development",
                    instance_id="password-recovery",
                    harbor_trial_dir=trial,
                    sidecar_evidence_dir=sidecar,
                    output_dir=root / "cross-boundary",
                )

    def test_imports_a_validated_preflight_rejection_without_fabricating_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial_id = "password-recovery:C:development"
            sidecar = make_sidecar(root, trial_id=trial_id)
            terminal = make_non_forwarded_input_limit_attempt(trial_id=trial_id)
            (sidecar / "upstream_requests" / "attempt-0002.json").write_text(
                json.dumps(terminal), encoding="utf-8"
            )
            self.assertEqual(
                non_forwarded_terminal_disposition(terminal)["reason"],
                "input_budget_rejected_before_provider_boundary",
            )
            packet = root / "packet"
            manifest = import_harbor_openclaw_trial(
                trial_id=trial_id,
                instance_id="password-recovery",
                harbor_trial_dir=make_trial(root),
                sidecar_evidence_dir=sidecar,
                output_dir=packet,
            )
            self.assertEqual(manifest["session_binding"]["proxy"]["unbound_attempts"], ["attempt-0002.json"])
            copied = read_json(packet / "proxy-attempts" / "attempt-0002.json")
            self.assertNotIn("normal_call_boundary", copied)
            self.assertNotIn("forwarded_request", copied)

    def test_imports_a_validated_event_budget_rejection_without_fabricating_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial_id = "password-recovery:C:development"
            sidecar = make_sidecar(root, trial_id=trial_id)
            terminal = make_non_forwarded_event_budget_attempt(trial_id=trial_id)
            (sidecar / "upstream_requests" / "attempt-0002.json").write_text(
                json.dumps(terminal), encoding="utf-8"
            )
            self.assertEqual(
                non_forwarded_terminal_disposition(terminal)["reason"],
                "event_skill_budget_rejected_before_provider_boundary",
            )
            manifest = import_harbor_openclaw_trial(
                trial_id=trial_id,
                instance_id="password-recovery",
                harbor_trial_dir=make_trial(root),
                sidecar_evidence_dir=sidecar,
                output_dir=root / "packet",
            )
            self.assertEqual(manifest["session_binding"]["proxy"]["unbound_attempts"], ["attempt-0002.json"])

    def test_imports_a_public_proxy_limit_rejection_without_fabricating_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial_id = "password-recovery:C:development"
            sidecar = make_sidecar(root, trial_id=trial_id)
            terminal = make_proxy_limit_rejection(trial_id=trial_id)
            (sidecar / "upstream_requests" / "attempt-0002.json").write_text(
                json.dumps(terminal), encoding="utf-8"
            )
            self.assertEqual(
                non_forwarded_terminal_disposition(terminal)["reason"],
                "public_preflight_rejection",
            )
            manifest = import_harbor_openclaw_trial(
                trial_id=trial_id,
                instance_id="password-recovery",
                harbor_trial_dir=make_trial(root),
                sidecar_evidence_dir=sidecar,
                output_dir=root / "packet",
            )
            self.assertEqual(manifest["session_binding"]["proxy"]["unbound_attempts"], ["attempt-0002.json"])

    def test_rejects_an_unbound_attempt_without_a_proven_preflight_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial_id = "password-recovery:C:development"
            sidecar = make_sidecar(root, trial_id=trial_id)
            terminal = make_non_forwarded_input_limit_attempt(trial_id=trial_id)
            terminal["error_type"] = "RuntimeError"
            (sidecar / "upstream_requests" / "attempt-0002.json").write_text(
                json.dumps(terminal), encoding="utf-8"
            )
            with self.assertRaisesRegex(HarborTrialEvidenceError, "validated non-forwarded terminal evidence"):
                import_harbor_openclaw_trial(
                    trial_id=trial_id,
                    instance_id="password-recovery",
                    harbor_trial_dir=make_trial(root),
                    sidecar_evidence_dir=sidecar,
                    output_dir=root / "invalid",
                )

    def test_rejects_a_terminal_record_that_also_claims_forwarding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial_id = "password-recovery:C:development"
            sidecar = make_sidecar(root, trial_id=trial_id)
            terminal = make_non_forwarded_input_limit_attempt(trial_id=trial_id)
            terminal["forwarded_request"] = {"messages": []}
            (sidecar / "upstream_requests" / "attempt-0002.json").write_text(
                json.dumps(terminal), encoding="utf-8"
            )
            with self.assertRaisesRegex(HarborTrialEvidenceError, "validated non-forwarded terminal evidence"):
                import_harbor_openclaw_trial(
                    trial_id=trial_id,
                    instance_id="password-recovery",
                    harbor_trial_dir=make_trial(root),
                    sidecar_evidence_dir=sidecar,
                    output_dir=root / "ambiguous",
                )

    def test_rejects_a_terminal_record_that_also_claims_a_normal_boundary(self) -> None:
        """A preflight rejection and a provider boundary are contradictory."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial_id = "password-recovery:C:development"
            sidecar = make_sidecar(root, trial_id=trial_id)
            terminal = make_non_forwarded_input_limit_attempt(trial_id=trial_id)
            terminal["normal_call_boundary"] = {
                "kind": "r012_public_plugin_normal_call_boundary",
                "trial_id": trial_id,
                "session_id": "session-fixture",
            }
            self.assertIsNotNone(non_forwarded_terminal_schema(terminal))
            (sidecar / "upstream_requests" / "attempt-0002.json").write_text(
                json.dumps(terminal), encoding="utf-8"
            )
            with self.assertRaisesRegex(HarborTrialEvidenceError, "both a normal_call_boundary"):
                import_harbor_openclaw_trial(
                    trial_id=trial_id,
                    instance_id="password-recovery",
                    harbor_trial_dir=make_trial(root),
                    sidecar_evidence_dir=sidecar,
                    output_dir=root / "ambiguous-normal-boundary",
                )

    def test_refuses_missing_verifier_or_cross_trial_proxy_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial = make_trial(root)
            (trial / "verifier" / "reward.txt").unlink()
            with self.assertRaisesRegex(HarborTrialEvidenceError, "required reward"):
                import_harbor_openclaw_trial(
                    trial_id="password-recovery:C:development",
                    instance_id="password-recovery",
                    harbor_trial_dir=trial,
                    sidecar_evidence_dir=make_sidecar(root, trial_id="other:C:development"),
                    output_dir=root / "missing-reward",
                )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(HarborTrialEvidenceError, "different trial"):
                import_harbor_openclaw_trial(
                    trial_id="password-recovery:C:development",
                    instance_id="password-recovery",
                    harbor_trial_dir=make_trial(root),
                    sidecar_evidence_dir=make_sidecar(root, trial_id="other:C:development"),
                    output_dir=root / "cross-trial",
                )

    def test_refuses_task_that_differs_from_the_frozen_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(HarborTrialEvidenceError, "differs from the frozen instance"):
                import_harbor_openclaw_trial(
                    trial_id="password-recovery:C:development",
                    instance_id="portfolio-optimization",
                    harbor_trial_dir=make_trial(root),
                    sidecar_evidence_dir=make_sidecar(root, trial_id="password-recovery:C:development"),
                    output_dir=root / "wrong-task",
                )


if __name__ == "__main__":
    unittest.main()
