from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from codeskill_rebuild.openclaw_compaction import (
    NativeCompactionEvidenceError,
    SessionJsonlCompactionDetector,
    SqliteTranscriptCompactionDetector,
)
from codeskill_rebuild.openclaw_overlay import DurableOverlay
from codeskill_rebuild.openclaw_native_summary import NativeSummaryPermit, NativeSummaryPermitError, NativeSummaryPermitGate
from codeskill_rebuild.openclaw_proxy import DurableProxyService, UpstreamStream, serve_in_thread
from codeskill_rebuild.types import read_json


TASK = {"skill_id": "task", "version": 1, "title": "Task", "when_to_apply": "start", "rules": ["inspect"]}
EVENT = {"skill_id": "event", "version": 1, "title": "Event", "when_to_apply": "after tool", "rules": ["verify"]}


class FakeTransport:
    endpoint = "http://fake-upstream/v1/chat/completions"

    def __init__(self, *, chunks: list[bytes], status: int = 200, content_type: str = "text/event-stream") -> None:
        self.chunks = chunks
        self.status = status
        self.content_type = content_type
        self.payloads: list[dict] = []
        self.timeouts: list[int | None] = []
        self.closed = 0

    def open(self, payload: dict, *, timeout_seconds: int | None = None) -> UpstreamStream:
        self.payloads.append(payload)
        self.timeouts.append(timeout_seconds)
        return UpstreamStream(
            status=self.status,
            headers={"content-type": self.content_type, "x-request-id": "fake-request"},
            chunks=iter(self.chunks),
            close=lambda: setattr(self, "closed", self.closed + 1),
        )


def user() -> dict:
    return {"role": "user", "content": "Solve it."}


def complete_batch() -> list[dict]:
    return [
        user(),
        {"role": "assistant", "tool_calls": [{"id": "a", "type": "function", "function": {"name": "exec", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "result"},
    ]


def append_native_jsonl(path: Path, *entries: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def append_native_sqlite_event(database_path: Path, session_id: str, seq: int, entry: dict) -> None:
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS transcript_events (session_id TEXT NOT NULL, seq INTEGER NOT NULL, event_json TEXT NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY (session_id, seq))"
        )
        connection.execute(
            "INSERT INTO transcript_events (session_id, seq, event_json, created_at) VALUES (?, ?, ?, ?)",
            (session_id, seq, json.dumps(entry), 1_725_724_800_000 + seq),
        )
        connection.commit()
    finally:
        connection.close()


def write_native_summary_permit(directory: Path, *, trial_id: str, session_id: str, issued_at_unix_ms: int = 1_000_000) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"native-summary-{session_id}-{'a' * 32}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "codeskill_native_summary_permit",
                "trial_id": trial_id,
                "session_id": session_id,
                "issued_at_unix_ms": issued_at_unix_ms,
                "nonce": "a" * 32,
            }
        ),
        encoding="utf-8",
    )
    return path


def write_normal_call_boundary(directory: Path, *, trial_id: str, session_id: str, issued_at_unix_ms: int = 1_000_000) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"normal-call-{session_id}-{'b' * 32}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "codeskill_normal_call_boundary",
                "trial_id": trial_id,
                "session_id": session_id,
                "issued_at_unix_ms": issued_at_unix_ms,
                "nonce": "b" * 32,
            }
        ),
        encoding="utf-8",
    )
    return path


class DurableProxyTest(unittest.TestCase):
    def make_service(
        self,
        root: Path,
        transport: FakeTransport,
        *,
        task: bool = False,
        event: bool = False,
        token_counter: object | None = None,
    ) -> DurableProxyService:
        overlay = DurableOverlay(
            trial_id="trial-proxy",
            state_path=root / "state.json",
            evidence_dir=root / "evidence",
            token_counter=token_counter or (lambda payload: 5 if "tools" in payload else len(payload["messages"])),
            task_selector=(lambda _initial, _history: {"skill": TASK}) if task else None,
            event_selector=(lambda _anchor, _prefix: {"skill": EVENT}) if event else None,
            enable_task=task,
            enable_event=event,
        )
        return DurableProxyService(overlay=overlay, transport=transport)

    def test_http_streaming_forwards_bytes_and_records_full_payload_token_evidence(self) -> None:
        body = b'data: {"choices":[]}\n\ndata: {"usage":{"prompt_tokens":5}}\n\ndata: [DONE]\n\n'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transport = FakeTransport(chunks=[body[:18], body[18:]])
            service = self.make_service(root, transport)
            server, thread = serve_in_thread(service)
            try:
                port = server.server_address[1]
                payload = {"model": "m", "messages": [user()], "tools": [{"type": "function", "function": {"name": "exec"}}], "tool_choice": "auto", "stream": True}
                request = Request(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=5) as response:
                    raw = response.read()
                    self.assertEqual(response.status, 200)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            record = read_json(root / "evidence" / "upstream_requests" / "attempt-0001.json")
        self.assertEqual(raw, body)
        self.assertEqual(transport.payloads, [payload])
        self.assertEqual(transport.closed, 1)
        self.assertEqual(record["token_count_scope"], "complete_forwarded_openai_payload")
        self.assertEqual(record["exact_forwarded_input_tokens"], 5)
        self.assertTrue(record["tokenizer_prompt_token_comparison"]["matches"])
        self.assertEqual(record["raw_upstream_response"], body.decode())

    def test_event_is_selected_before_forwarding_terminal_batch_and_retained_on_later_history(self) -> None:
        response = b'{"usage":{"prompt_tokens":3},"choices":[{"message":{"content":"ok"}}]}'
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport(chunks=[response], content_type="application/json")
            service = self.make_service(Path(tmp), transport, task=True, event=True)
            list(service.forward({"messages": [user()]}).iter_bytes())
            list(service.forward({"messages": complete_batch()}).iter_bytes())
            list(service.forward({"messages": [*complete_batch(), {"role": "assistant", "content": "next"}]}).iter_bytes())
        event_blocks = [message for message in transport.payloads[1]["messages"] if "EVENT PRIOR" in str(message.get("content"))]
        later_blocks = [message for message in transport.payloads[2]["messages"] if "EVENT PRIOR" in str(message.get("content"))]
        self.assertEqual(len(event_blocks), 1)
        self.assertEqual(len(later_blocks), 1)
        self.assertEqual(transport.payloads[1]["messages"][3]["role"], "user")

    def test_preflight_context_error_does_not_contact_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport(chunks=[b"unused"])
            service = self.make_service(Path(tmp), transport)
            service.overlay.max_input_tokens = 4
            response = service.forward({"messages": [user()], "tools": [{"type": "function"}]})
            raw = b"".join(response.iter_bytes())
        self.assertEqual(response.status, 400)
        self.assertEqual(transport.payloads, [])
        error = json.loads(raw)["error"]
        self.assertEqual(error["code"], "context_length_exceeded")
        self.assertIn("Context length exceeded", error["message"])

    def test_unknown_anchor_is_not_misreported_as_context_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport(chunks=[b"unused"])
            service = self.make_service(Path(tmp), transport, task=True)
            list(service.forward({"messages": [user()]}).iter_bytes())
            # The original user message has vanished without evidence of a
            # native compaction transition, so recovery must not run.
            response = service.forward({"messages": [{"role": "assistant", "content": "later"}]})
            raw = b"".join(response.iter_bytes())
        error = json.loads(raw)["error"]
        self.assertEqual(response.status, 409)
        self.assertEqual(error["code"], "codeskill_anchor_evidence_missing")
        self.assertNotIn("context length exceeded", error["message"].casefold())
        self.assertEqual(len(transport.payloads), 1)

    def test_new_native_jsonl_compaction_retires_the_missing_event_anchor(self) -> None:
        response = b'{"usage":{"prompt_tokens":3},"choices":[{"message":{"content":"ok"}}]}'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_file = root / "native-session.jsonl"
            append_native_jsonl(session_file, {"type": "session", "id": "native-1", "timestamp": "2026-09-09T00:00:00Z"})
            detector = SessionJsonlCompactionDetector(
                location_provider=lambda: {"session_file": session_file, "session_id": "native-1"},
                evidence_dir=root / "evidence",
            )
            transport = FakeTransport(chunks=[response], content_type="application/json")
            service = self.make_service(root, transport, task=True, event=True)
            service.compaction_evidence_provider = detector
            list(service.forward({"messages": [user()]}).iter_bytes())
            list(service.forward({"messages": complete_batch()}).iter_bytes())
            append_native_jsonl(
                session_file,
                {
                    "type": "compaction",
                    "id": "compact-1",
                    "parentId": "assistant-1",
                    "timestamp": "2026-09-09T00:01:00Z",
                    "summary": "the source JSONL remains the raw native record",
                    "firstKeptEntryId": "assistant-2",
                    "tokensBefore": 42,
                },
            )
            list(service.forward({"messages": [{"role": "assistant", "content": "after native compaction"}]}).iter_bytes())
            record = read_json(root / "evidence" / "upstream_requests" / "attempt-0003.json")
            detector_observation = read_json(root / "evidence" / "native_compaction" / "attempt-0003.json")
        carried = [message["content"] for message in transport.payloads[2]["messages"] if "CARRIED" in str(message.get("content"))]
        self.assertEqual(len(carried), 1)
        self.assertEqual(record["carried_priors"]["count"], 1)
        self.assertEqual(record["carried_priors"]["current_transition_evidence"]["compaction_id"], "compact-1")
        self.assertEqual(record["retired_event_priors"]["events"][0]["skill_id"], EVENT["skill_id"])
        self.assertEqual(detector_observation["selected_compaction_id"], "compact-1")
        self.assertEqual(detector_observation["overlay_disposition"]["kind"], "relocation_persisted")
        self.assertEqual(record["retired_event_priors"]["events"][0]["retirement"]["compaction"]["native_session_event_ref"], detector_observation["selected_native_session_event_ref"])

    def test_new_native_sqlite_compaction_retires_the_missing_event_anchor(self) -> None:
        response = b'{"usage":{"prompt_tokens":3},"choices":[{"message":{"content":"ok"}}]}'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database_path = root / "openclaw-agent.sqlite"
            session_id = "native-1"
            append_native_sqlite_event(database_path, session_id, 1, {"type": "message", "id": "user-1"})
            detector = SqliteTranscriptCompactionDetector(
                location_provider=lambda: {"session_file": f"sqlite:main:{session_id}:{database_path}"},
                evidence_dir=root / "evidence",
            )
            transport = FakeTransport(chunks=[response], content_type="application/json")
            service = self.make_service(root, transport, task=True, event=True)
            service.compaction_evidence_provider = detector
            list(service.forward({"messages": [user()]}).iter_bytes())
            list(service.forward({"messages": complete_batch()}).iter_bytes())
            append_native_sqlite_event(
                database_path,
                session_id,
                2,
                {
                    "type": "compaction",
                    "id": "compact-1",
                    "parentId": "assistant-1",
                    "timestamp": "2026-09-09T00:01:00Z",
                    "summary": "the native SQLite row stays outside CODESKILL evidence",
                    "firstKeptEntryId": "assistant-2",
                    "tokensBefore": 42,
                },
            )
            list(service.forward({"messages": [{"role": "assistant", "content": "after native compaction"}]}).iter_bytes())
            record = read_json(root / "evidence" / "upstream_requests" / "attempt-0003.json")
            observation = read_json(root / "evidence" / "native_compaction" / "attempt-0003.json")
        self.assertEqual(record["carried_priors"]["current_transition_evidence"]["compaction_id"], "compact-1")
        self.assertEqual(record["retired_event_priors"]["events"][0]["skill_id"], EVENT["skill_id"])
        self.assertEqual(observation["kind"], "r012_native_sqlite_compaction_observation")
        self.assertIn("#session=native-1#seq=2#sha256=", observation["selected_native_session_event_ref"])
        self.assertEqual(observation["overlay_disposition"]["kind"], "relocation_persisted")
        self.assertNotIn("native SQLite row stays", json.dumps(observation))

    def test_public_hook_permit_forwards_raw_native_summary_then_requires_sqlite_transition(self) -> None:
        response = b'{"usage":{"prompt_tokens":3},"choices":[{"message":{"content":"ok"}}]}'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "native-1"
            transport = FakeTransport(chunks=[response], content_type="application/json")
            service = self.make_service(root, transport, task=True)
            gate = NativeSummaryPermitGate(
                permit_dir=root / "permits",
                expected_session_id=session_id,
                trial_id="trial-proxy",
                clock=lambda: 1_000.0,
            )
            service.native_summary_permit_gate = gate
            evidence = [
                None,
                None,
                {
                    "kind": "native_compaction",
                    "confirmed": True,
                    "compaction_id": "sqlite-compact-1",
                    "native_session_event_ref": "sqlite:main#session=native-1#seq=9#sha256=abc",
                    "observed_at_utc": "2026-09-10T00:00:00+00:00",
                    "before_forwarded_request_ordinal": 2,
                    "after_proxy_attempt_ordinal": 3,
                },
            ]
            service.compaction_evidence_provider = lambda *_args: evidence.pop(0)
            list(service.forward({"messages": [user()]}).iter_bytes())
            write_native_summary_permit(root / "permits", trial_id="trial-proxy", session_id=session_id)
            summary_payload = {"messages": [{"role": "system", "content": "native summary only"}], "stream": False}
            list(service.forward(summary_payload).iter_bytes())
            post_compaction = {"messages": [{"role": "assistant", "content": "after native compaction"}], "stream": False}
            write_normal_call_boundary(root / "permits", trial_id="trial-proxy", session_id=session_id)
            list(service.forward(post_compaction).iter_bytes())
            summary_record = read_json(root / "evidence" / "upstream_requests" / "attempt-0002.json")
            post_record = read_json(root / "evidence" / "upstream_requests" / "attempt-0003.json")
            gate_state = read_json(root / "permits" / "sidecar-permit-state.json")
        self.assertEqual(transport.payloads[1], summary_payload)
        self.assertEqual(summary_record["kind"], "r012_native_summary_upstream_request")
        self.assertEqual(summary_record["overlay_disposition"]["kind"], "native_summary_bypass")
        self.assertEqual(summary_record["native_summary_permit"]["session_id"], session_id)
        self.assertEqual(post_record["carried_priors"]["current_transition_evidence"]["compaction_id"], "sqlite-compact-1")
        self.assertEqual(post_record["normal_call_boundary"]["permit_resolution"], "retired_after_native_sqlite_transition")
        statuses = [value.get("status") for value in gate_state["permits"].values()]
        self.assertEqual(statuses, ["retired_after_native_sqlite_transition"])

    def test_foreign_public_hook_permit_blocks_before_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transport = FakeTransport(chunks=[b"unused"])
            service = self.make_service(root, transport)
            service.native_summary_permit_gate = NativeSummaryPermitGate(
                permit_dir=root / "permits",
                expected_session_id="expected-session",
                trial_id="trial-proxy",
                clock=lambda: 1_000.0,
            )
            write_native_summary_permit(root / "permits", trial_id="trial-proxy", session_id="foreign-session")
            response = service.forward({"messages": [user()]})
            raw = b"".join(response.iter_bytes())
        self.assertEqual(response.status, 409)
        self.assertEqual(json.loads(raw)["error"]["code"], "codeskill_native_summary_permit_error")
        self.assertEqual(transport.payloads, [])

    def test_unfinished_compaction_normal_boundary_revokes_permit_and_uses_the_overlay(self) -> None:
        response = b'{"usage":{"prompt_tokens":3},"choices":[{"message":{"content":"ok"}}]}'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transport = FakeTransport(chunks=[response], content_type="application/json")
            service = self.make_service(root, transport, task=True)
            service.compaction_evidence_provider = lambda *_args: None
            service.native_summary_permit_gate = NativeSummaryPermitGate(
                permit_dir=root / "permits",
                expected_session_id="native-1",
                trial_id="trial-proxy",
                clock=lambda: 1_000.0,
            )
            write_native_summary_permit(root / "permits", trial_id="trial-proxy", session_id="native-1")
            write_normal_call_boundary(root / "permits", trial_id="trial-proxy", session_id="native-1")
            normal_payload = {"messages": [user()], "stream": False}
            list(service.forward(normal_payload).iter_bytes())
            record = read_json(root / "evidence" / "upstream_requests" / "attempt-0001.json")
            state = read_json(root / "permits" / "sidecar-permit-state.json")
        self.assertEqual(record["kind"], "r012_actual_upstream_request")
        self.assertEqual(record["normal_call_boundary"]["permit_resolution"], "revoked_before_native_sqlite_transition")
        self.assertNotIn("r012_native_summary_upstream_request", json.dumps(record))
        self.assertNotEqual(transport.payloads[0], normal_payload)
        statuses = [item.get("status") for item in state["permits"].values()]
        self.assertEqual(statuses, ["revoked_by_public_normal_call_boundary_before_native_sqlite_transition"])

    def test_recovered_unresolved_permit_without_normal_boundary_fails_closed_before_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_native_summary_permit(root / "permits", trial_id="trial-proxy", session_id="native-1")
            first_gate = NativeSummaryPermitGate(
                permit_dir=root / "permits",
                expected_session_id="native-1",
                trial_id="trial-proxy",
                clock=lambda: 1_000.0,
            )
            self.assertIsInstance(first_gate.authorize(compaction_evidence=None), NativeSummaryPermit)
            transport = FakeTransport(chunks=[b"unused"])
            service = self.make_service(root, transport)
            service.compaction_evidence_provider = lambda *_args: None
            service.native_summary_permit_gate = NativeSummaryPermitGate(
                permit_dir=root / "permits",
                expected_session_id="native-1",
                trial_id="trial-proxy",
                clock=lambda: 1_000.0,
            )
            response = service.forward({"messages": [user()]})
            raw = b"".join(response.iter_bytes())
            state = read_json(root / "permits" / "sidecar-permit-state.json")
        self.assertEqual(response.status, 409)
        self.assertEqual(json.loads(raw)["error"]["code"], "codeskill_native_summary_permit_error")
        self.assertEqual(transport.payloads, [])
        statuses = [item.get("status") for item in state["permits"].values()]
        self.assertEqual(statuses, ["rejected_recovered_unresolved_permit_without_public_normal_boundary"])

    def test_stale_permit_with_a_normal_boundary_fails_closed_before_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = [1_000.0]
            write_native_summary_permit(root / "permits", trial_id="trial-proxy", session_id="native-1")
            gate = NativeSummaryPermitGate(
                permit_dir=root / "permits",
                expected_session_id="native-1",
                trial_id="trial-proxy",
                max_age_seconds=300,
                clock=lambda: now[0],
            )
            self.assertIsInstance(gate.authorize(compaction_evidence=None), NativeSummaryPermit)
            now[0] = 1_301.0
            write_normal_call_boundary(root / "permits", trial_id="trial-proxy", session_id="native-1", issued_at_unix_ms=1_301_000)
            transport = FakeTransport(chunks=[b"unused"])
            service = self.make_service(root, transport)
            service.compaction_evidence_provider = lambda *_args: None
            service.native_summary_permit_gate = gate
            response = service.forward({"messages": [user()]})
            raw = b"".join(response.iter_bytes())
        self.assertEqual(response.status, 409)
        self.assertEqual(json.loads(raw)["error"]["code"], "codeskill_native_summary_permit_error")
        self.assertEqual(transport.payloads, [])

    def test_native_summary_retries_are_bounded_without_a_normal_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_native_summary_permit(root / "permits", trial_id="trial-proxy", session_id="native-1")
            gate = NativeSummaryPermitGate(
                permit_dir=root / "permits",
                expected_session_id="native-1",
                trial_id="trial-proxy",
                max_bypass_attempts=4,
                clock=lambda: 1_000.0,
            )
            decisions = [gate.authorize(compaction_evidence=None) for _ in range(4)]
            with self.assertRaisesRegex(NativeSummaryPermitError, "bounded retry allowance"):
                gate.authorize(compaction_evidence=None)
        self.assertTrue(all(isinstance(item, NativeSummaryPermit) for item in decisions))

    def test_foreign_normal_boundary_blocks_before_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transport = FakeTransport(chunks=[b"unused"])
            service = self.make_service(root, transport)
            service.native_summary_permit_gate = NativeSummaryPermitGate(
                permit_dir=root / "permits",
                expected_session_id="expected-session",
                trial_id="trial-proxy",
                clock=lambda: 1_000.0,
            )
            write_normal_call_boundary(root / "permits", trial_id="trial-proxy", session_id="foreign-session")
            response = service.forward({"messages": [user()]})
            raw = b"".join(response.iter_bytes())
        self.assertEqual(response.status, 409)
        self.assertEqual(json.loads(raw)["error"]["code"], "codeskill_native_summary_permit_error")
        self.assertEqual(transport.payloads, [])

    def test_active_public_hook_permit_expires_after_its_first_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = [1_000.0]
            gate = NativeSummaryPermitGate(
                permit_dir=root / "permits",
                expected_session_id="native-1",
                trial_id="trial-proxy",
                max_age_seconds=300,
                clock=lambda: now[0],
            )
            write_native_summary_permit(root / "permits", trial_id="trial-proxy", session_id="native-1")
            self.assertIsNotNone(gate.authorize(compaction_evidence=None))
            now[0] = 1_301.0
            with self.assertRaisesRegex(NativeSummaryPermitError, "outside its allowed lifetime"):
                gate.authorize(compaction_evidence=None)
            state = read_json(root / "permits" / "sidecar-permit-state.json")
        statuses = [record.get("status") for record in state["permits"].values()]
        self.assertEqual(statuses, ["rejected_expired_before_native_sqlite_transition"])

    def test_native_compaction_provider_failure_blocks_without_opening_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transport = FakeTransport(chunks=[b"unused"])
            service = self.make_service(root, transport)

            def corrupted_provider(*_args: object) -> None:
                raise NativeCompactionEvidenceError("malformed native session JSONL")

            service.compaction_evidence_provider = corrupted_provider
            response = service.forward({"messages": [user()]})
            raw = b"".join(response.iter_bytes())
            rejection = read_json(root / "evidence" / "upstream_requests" / "attempt-0001.json")
        self.assertEqual(response.status, 409)
        self.assertEqual(json.loads(raw)["error"]["code"], "codeskill_native_compaction_evidence_error")
        self.assertEqual(transport.payloads, [])
        self.assertEqual(rejection["proxy_outcome"], "native_compaction_evidence_rejected")

    def test_context_rejection_confirms_persisted_relocation_for_a_later_retry(self) -> None:
        response = b'{"usage":{"prompt_tokens":3},"choices":[{"message":{"content":"ok"}}]}'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_file = root / "native-session.jsonl"
            append_native_jsonl(session_file, {"type": "session", "id": "native-1", "timestamp": "2026-09-09T00:00:00Z"})
            detector = SessionJsonlCompactionDetector(
                location_provider=lambda: {"session_file": session_file, "session_id": "native-1"},
                evidence_dir=root / "evidence",
            )
            transport = FakeTransport(chunks=[response], content_type="application/json")
            service = self.make_service(root, transport, task=True, event=True)
            service.compaction_evidence_provider = detector
            list(service.forward({"messages": [user()]}).iter_bytes())
            list(service.forward({"messages": complete_batch()}).iter_bytes())
            append_native_jsonl(
                session_file,
                {
                    "type": "compaction",
                    "id": "compact-1",
                    "parentId": "assistant-1",
                    "timestamp": "2026-09-09T00:01:00Z",
                    "summary": "raw only",
                    "firstKeptEntryId": "assistant-2",
                },
            )
            service.overlay.max_input_tokens = 1
            blocked = service.forward({"messages": [{"role": "assistant", "content": "after native compaction"}]})
            self.assertEqual(blocked.status, 400)
            self.assertEqual(json.loads(b"".join(blocked.iter_bytes()))["error"]["code"], "context_length_exceeded")
            blocked_observation = read_json(root / "evidence" / "native_compaction" / "attempt-0003.json")
            self.assertEqual(blocked_observation["overlay_disposition"]["kind"], "relocation_persisted")
            service.overlay.max_input_tokens = 250_000
            list(service.forward({"messages": [{"role": "assistant", "content": "after native compaction"}]}).iter_bytes())
        carried = [message["content"] for message in transport.payloads[-1]["messages"] if "CARRIED" in str(message.get("content"))]
        self.assertEqual(len(carried), 1)

    def test_context_rejected_new_event_is_not_committed_or_deduplicated_before_retry(self) -> None:
        response = b'{"usage":{"prompt_tokens":3},"choices":[{"message":{"content":"ok"}}]}'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transport = FakeTransport(chunks=[response], content_type="application/json")
            service = self.make_service(root, transport, event=True)
            list(service.forward({"messages": [user()]}).iter_bytes())
            service.overlay.max_input_tokens = 2
            blocked = service.forward({"messages": complete_batch()})
            self.assertEqual(blocked.status, 400)
            self.assertEqual(json.loads(b"".join(blocked.iter_bytes()))["error"]["code"], "context_length_exceeded")
            state_after_rejection = read_json(root / "state.json")
            rejection = read_json(root / "evidence" / "upstream_requests" / "attempt-0002.json")
            self.assertEqual(state_after_rejection["events"], [])
            self.assertEqual(state_after_rejection["processed_batches"], [])
            self.assertEqual(state_after_rejection["selected_skill_versions"], [])
            self.assertIn("uncommitted_selection", rejection)
            self.assertNotIn("event_selection", rejection)
            service.overlay.max_input_tokens = 250_000
            list(service.forward({"messages": complete_batch()}).iter_bytes())
            state_after_retry = read_json(root / "state.json")
        self.assertEqual(len(state_after_retry["events"]), 1)
        event_blocks = [message for message in transport.payloads[-1]["messages"] if "EVENT PRIOR" in str(message.get("content"))]
        self.assertEqual(len(event_blocks), 1)

    def test_forwarded_request_cap_rejects_the_next_request_and_records_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transport = FakeTransport(chunks=[b'{"choices":[]}'])
            service = self.make_service(root, transport)
            service.max_forwarded_requests = 1
            list(service.forward({"messages": [user()]}).iter_bytes())
            response = service.forward({"messages": [user()]})
            raw = b"".join(response.iter_bytes())
            rejection = read_json(root / "evidence" / "upstream_requests" / "attempt-0002.json")
        self.assertEqual(response.status, 429)
        self.assertEqual(json.loads(raw)["error"]["code"], "codeskill_request_limit_reached")
        self.assertEqual(len(transport.payloads), 1)
        self.assertEqual(rejection["proxy_outcome"], "request_limit_rejected")

    def test_output_cap_rejects_a_larger_max_tokens_and_records_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transport = FakeTransport(chunks=[b"unused"])
            service = self.make_service(root, transport)
            service.max_output_tokens = 16_384
            response = service.forward({"messages": [user()], "max_tokens": 16_385})
            raw = b"".join(response.iter_bytes())
            rejection = read_json(root / "evidence" / "upstream_requests" / "attempt-0001.json")
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(raw)["error"]["code"], "codeskill_max_tokens_exceeded")
        self.assertEqual(transport.payloads, [])
        self.assertEqual(rejection["proxy_outcome"], "output_limit_rejected")

    def test_output_cap_accepts_openai_max_completion_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transport = FakeTransport(chunks=[b'{"choices":[]}'])
            service = self.make_service(root, transport)
            service.max_output_tokens = 16_384
            payload = {"messages": [user()], "max_completion_tokens": 16_384}
            list(service.forward(payload).iter_bytes())
        self.assertEqual(transport.payloads, [payload])

    def test_output_cap_rejects_an_invalid_openai_max_completion_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transport = FakeTransport(chunks=[b"unused"])
            service = self.make_service(root, transport)
            service.max_output_tokens = 16_384
            response = service.forward({"messages": [user()], "max_completion_tokens": 16_385})
            raw = b"".join(response.iter_bytes())
            rejection = read_json(root / "evidence" / "upstream_requests" / "attempt-0001.json")
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(raw)["error"]["code"], "codeskill_max_tokens_exceeded")
        self.assertEqual(transport.payloads, [])
        self.assertEqual(rejection["limit_details"]["requested_max_completion_tokens"], 16_385)

    def test_expired_trial_deadline_rejects_before_upstream_and_records_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transport = FakeTransport(chunks=[b"unused"])
            service = self.make_service(root, transport)
            service.trial_deadline_monotonic = time.monotonic() - 0.001
            response = service.forward({"messages": [user()]})
            raw = b"".join(response.iter_bytes())
            rejection = read_json(root / "evidence" / "upstream_requests" / "attempt-0001.json")
        self.assertEqual(response.status, 408)
        self.assertEqual(json.loads(raw)["error"]["code"], "codeskill_trial_deadline_exceeded")
        self.assertEqual(transport.payloads, [])
        self.assertEqual(rejection["proxy_outcome"], "trial_deadline_rejected")

    def test_remaining_trial_deadline_bounds_the_upstream_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport(chunks=[b'{"choices":[]}'])
            service = self.make_service(Path(tmp), transport)
            service.trial_deadline_monotonic = time.monotonic() + 1.5
            list(service.forward({"messages": [user()]}).iter_bytes())
        self.assertEqual(len(transport.timeouts), 1)
        self.assertIsInstance(transport.timeouts[0], int)
        self.assertGreaterEqual(transport.timeouts[0], 1)
        self.assertLessEqual(transport.timeouts[0], 2)

    def test_deadline_is_rechecked_after_overlay_prepare_before_upstream_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = [0.0]

            def delayed_counter(_payload: dict) -> int:
                clock[0] = 2.1
                return 5

            root = Path(tmp)
            transport = FakeTransport(chunks=[b"unused"])
            service = self.make_service(root, transport, token_counter=delayed_counter)
            service.clock = lambda: clock[0]
            service.trial_deadline_monotonic = 2.0
            response = service.forward({"messages": [user()]})
            raw = b"".join(response.iter_bytes())
            self.assertEqual(response.status, 408)
            self.assertEqual(json.loads(raw)["error"]["code"], "codeskill_trial_deadline_exceeded")
            self.assertEqual(transport.payloads, [])
            rejection = read_json(root / "evidence" / "upstream_requests" / "attempt-0001.json")
            self.assertEqual(rejection["proxy_outcome"], "trial_deadline_rejected")
            self.assertEqual(
                rejection["deadline_recheck"]["phase"],
                "after_overlay_prepare_before_upstream_open",
            )


if __name__ == "__main__":
    unittest.main()
