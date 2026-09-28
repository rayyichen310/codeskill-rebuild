from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codeskill_rebuild.openclaw_compaction import (
    NativeCompactionEvidenceError,
    SessionJsonlCompactionDetector,
    SqliteTranscriptCompactionDetector,
)
from codeskill_rebuild.types import read_json


def append_jsonl(path: Path, *entries: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def session(session_id: str) -> dict:
    return {"type": "session", "id": session_id, "timestamp": "2026-09-09T00:00:00.000Z"}


def compaction(compaction_id: str, *, parent_id: str = "assistant-1", first_kept: str = "user-2") -> dict:
    return {
        "type": "compaction",
        "id": compaction_id,
        "parentId": parent_id,
        "timestamp": "2026-09-09T00:01:00.000Z",
        "summary": "native summary is retained only in the source JSONL",
        "firstKeptEntryId": first_kept,
        "tokensBefore": 123,
    }


def append_sqlite_event(database_path: Path, session_id: str, seq: int, entry: dict) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS transcript_events (session_id TEXT NOT NULL, seq INTEGER NOT NULL, event_json TEXT NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY (session_id, seq))"
        )
        connection.execute(
            "INSERT INTO transcript_events (session_id, seq, event_json, created_at) VALUES (?, ?, ?, ?)",
            (session_id, seq, json.dumps(entry, ensure_ascii=False), 1_725_724_800_000 + seq),
        )
        connection.commit()
    finally:
        connection.close()


def update_sqlite_event(database_path: Path, session_id: str, seq: int, entry: dict) -> None:
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "UPDATE transcript_events SET event_json = ? WHERE session_id = ? AND seq = ?",
            (json.dumps(entry, ensure_ascii=False), session_id, seq),
        )
        connection.commit()
    finally:
        connection.close()


def sqlite_location(database_path: Path, session_id: str = "native-1") -> dict:
    return {"session_file": f"sqlite:main:{session_id}:{database_path}"}


class SessionJsonlCompactionDetectorTest(unittest.TestCase):
    def test_new_compaction_is_bound_to_the_request_transition_and_durable_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_file = root / "session.jsonl"
            append_jsonl(session_file, session("native-1"))
            detector = SessionJsonlCompactionDetector(
                location_provider=lambda: {"session_file": session_file, "session_id": "native-1"},
                evidence_dir=root / "evidence",
            )
            self.assertIsNone(detector("trial-1", 0, 1, {"messages": []}))
            append_jsonl(session_file, compaction("compact-1"))
            evidence = detector("trial-1", 1, 2, {"messages": [{"role": "assistant", "content": "next"}]})
            self.assertIsNotNone(evidence)
            assert evidence is not None
            self.assertEqual(evidence["kind"], "native_compaction")
            self.assertTrue(evidence["confirmed"])
            self.assertEqual(evidence["compaction_id"], "compact-1")
            self.assertEqual(evidence["before_forwarded_request_ordinal"], 1)
            self.assertEqual(evidence["after_proxy_attempt_ordinal"], 2)
            self.assertEqual(evidence["entry"]["firstKeptEntryId"], "user-2")
            self.assertIn("line=2", evidence["native_session_event_ref"])
            self.assertNotIn("native summary", json.dumps(evidence))
            pending = read_json(root / "evidence" / "native_compaction" / "detector-state.json")
            self.assertEqual(pending["pending"]["compaction_id"], "compact-1")
            detector.confirm_transition(
                evidence,
                {
                    "attempt_ordinal": 2,
                    "outcome": "forwardable",
                    "compaction_evidence": evidence,
                },
            )
            # A compaction record may persist through normal later requests;
            # it cannot authorize a second unrelated anchor disappearance.
            self.assertIsNone(detector("trial-1", 2, 3, {"messages": []}))
            observation = read_json(root / "evidence" / "native_compaction" / "attempt-0002.json")
            self.assertEqual(observation["new_compaction_ids"], ["compact-1"])
            self.assertEqual(observation["selected_compaction_id"], "compact-1")
            self.assertEqual(observation["overlay_disposition"]["kind"], "terminal_without_relocation")

    def test_pending_evidence_is_not_consumed_before_the_matching_overlay_disposition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_file = root / "session.jsonl"
            append_jsonl(session_file, session("native-1"))
            detector = SessionJsonlCompactionDetector(
                location_provider=lambda: {"session_file": session_file, "session_id": "native-1"},
                evidence_dir=root / "evidence",
            )
            detector("trial-1", 0, 1, {"messages": []})
            append_jsonl(session_file, compaction("compact-1"))
            evidence = detector("trial-1", 1, 2, {"messages": []})
            assert evidence is not None
            # A process crash between provider and overlay.prepare must fail
            # closed, not lend this record to a different request ordinal.
            with self.assertRaisesRegex(NativeCompactionEvidenceError, "pending"):
                detector("trial-1", 1, 3, {"messages": []})
            detector.confirm_transition(
                evidence,
                {
                    "attempt_ordinal": 2,
                    "outcome": "context_or_overlay_error",
                    "compaction_evidence": evidence,
                    "carried_priors": {"current_transition_evidence": evidence},
                },
            )
            self.assertIsNone(detector("trial-1", 1, 3, {"messages": []}))

    def test_preexisting_compaction_is_baselined_not_reused_as_current_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_file = root / "session.jsonl"
            append_jsonl(session_file, session("native-1"), compaction("old-compact"))
            detector = SessionJsonlCompactionDetector(
                location_provider=lambda: {"session_file": session_file, "session_id": "native-1"},
                evidence_dir=root / "evidence",
            )
            self.assertIsNone(detector("trial-1", 0, 1, {"messages": []}))
            self.assertIsNone(detector("trial-1", 1, 2, {"messages": []}))

    def test_first_discovered_session_is_baselined_after_a_missing_initial_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_file = root / "session.jsonl"
            current: dict | None = None
            detector = SessionJsonlCompactionDetector(location_provider=lambda: current, evidence_dir=root / "evidence")
            self.assertIsNone(detector("trial-1", 0, 1, {"messages": []}))
            append_jsonl(session_file, session("native-1"), compaction("historic-before-attachment"))
            current = {"session_file": session_file, "session_id": "native-1"}
            self.assertIsNone(detector("trial-1", 1, 2, {"messages": []}))
            self.assertIsNone(detector("trial-1", 2, 3, {"messages": []}))

    def test_rotation_requires_explicit_predecessor_and_can_use_new_record_in_predecessor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_file = root / "old.jsonl"
            new_file = root / "new.jsonl"
            append_jsonl(old_file, session("native-old"))
            current = {"session_file": old_file, "session_id": "native-old"}
            detector = SessionJsonlCompactionDetector(location_provider=lambda: current, evidence_dir=root / "evidence")
            self.assertIsNone(detector("trial-1", 0, 1, {"messages": []}))
            append_jsonl(old_file, compaction("compact-on-old"))
            append_jsonl(new_file, session("native-new"))
            current = {
                "session_file": new_file,
                "session_id": "native-new",
                "previous_session_file": old_file,
                "previous_session_id": "native-old",
            }
            evidence = detector("trial-1", 1, 2, {"messages": []})
            assert evidence is not None
            self.assertEqual(evidence["compaction_id"], "compact-on-old")
            self.assertEqual(evidence["session_rotation"]["from_session_id"], "native-old")
            self.assertEqual(evidence["session_rotation"]["to_session_id"], "native-new")

    def test_rotation_without_predecessor_or_malformed_compaction_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.jsonl"
            second = root / "second.jsonl"
            append_jsonl(first, session("native-1"))
            current = {"session_file": first, "session_id": "native-1"}
            detector = SessionJsonlCompactionDetector(location_provider=lambda: current, evidence_dir=root / "evidence")
            detector("trial-1", 0, 1, {"messages": []})
            append_jsonl(second, session("native-2"))
            current = {"session_file": second, "session_id": "native-2"}
            with self.assertRaisesRegex(NativeCompactionEvidenceError, "predecessor"):
                detector("trial-1", 1, 2, {"messages": []})

            # A malformed new record cannot be silently treated as absent.
            current = {"session_file": first, "session_id": "native-1"}
            append_jsonl(first, {"type": "compaction", "id": "bad", "timestamp": "now"})
            with self.assertRaisesRegex(NativeCompactionEvidenceError, "firstKeptEntryId"):
                detector("trial-1", 1, 3, {"messages": []})


class SqliteTranscriptCompactionDetectorTest(unittest.TestCase):
    def test_fresh_process_imports_both_public_sqlite_entrypoints(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src"
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(source_root) + os.pathsep + environment.get("PYTHONPATH", "")
        for module_name in ("codeskill_rebuild.openclaw_compaction", "codeskill_rebuild.sqlite_compaction"):
            result = subprocess.run(
                [sys.executable, "-c", f"import {module_name}"],
                check=False,
                capture_output=True,
                env=environment,
            )
            stderr = result.stderr.decode("utf-8", errors="replace")
            self.assertEqual(result.returncode, 0, msg=f"{module_name}: {stderr}")

    def test_sqlite_marker_paths_follow_openclaw_resolution_rules(self) -> None:
        from codeskill_rebuild.sqlite_compaction import _normalise_location

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            custom_sessions = root / "custom" / "sessions.json"
            custom = _normalise_location({"session_file": f"sqlite:main:s1:{custom_sessions}"})
            assert custom is not None
            self.assertEqual(custom["database_path"], str((root / "custom" / "openclaw-agent.sqlite").resolve()))

            named_store = root / "custom" / "run-state.json"
            normalized = _normalise_location({"session_file": f"sqlite: OPS :s2:{named_store}"})
            assert normalized is not None
            self.assertEqual(normalized["agent_id"], "ops")
            self.assertEqual(normalized["database_path"], str((root / "custom" / "run-state.ops.sqlite").resolve()))

            canonical_store = root / "agents" / "ops" / "sessions" / "sessions.json"
            canonical = _normalise_location({"session_file": f"sqlite:OPS:s3:{canonical_store}"})
            assert canonical is not None
            self.assertEqual(canonical["database_path"], str((root / "agents" / "ops" / "agent" / "openclaw-agent.sqlite").resolve()))

            direct = root / "agents" / "ops" / "agent" / "openclaw-agent.sqlite"
            direct_location = _normalise_location({"session_file": f"sqlite:ops:s4:{direct}"})
            assert direct_location is not None
            self.assertEqual(direct_location["database_path"], str(direct.resolve()))
            with self.assertRaisesRegex(NativeCompactionEvidenceError, "disagrees"):
                _normalise_location({"session_file": f"sqlite:main:s5:{direct}"})

    def test_sqlite_marker_resolution_retries_transient_container_permission_denial(self) -> None:
        from codeskill_rebuild.sqlite_compaction import _normalise_location

        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "openclaw-agent.sqlite"
            resolved = database_path.absolute()
            with patch(
                "codeskill_rebuild.sqlite_compaction.Path.resolve",
                side_effect=[PermissionError(13, "permission denied", str(database_path)), resolved],
            ), patch("codeskill_rebuild.sqlite_compaction.sleep") as wait:
                location = _normalise_location({"session_file": f"sqlite:main:s1:{database_path}"})
            assert location is not None
            self.assertEqual(location["database_path"], str(resolved))
            wait.assert_called_once_with(0.1)

    def test_sqlite_database_open_retries_transient_container_startup_failure(self) -> None:
        import codeskill_rebuild.sqlite_compaction as sqlite_compaction

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database_path = root / "openclaw-agent.sqlite"
            append_sqlite_event(database_path, "native-1", 1, {"type": "message", "id": "user-1"})
            detector = SqliteTranscriptCompactionDetector(
                location_provider=lambda: sqlite_location(database_path),
                evidence_dir=root / "evidence",
            )
            real_connect = sqlite_compaction.sqlite3.connect
            call_count = 0

            def connect_with_startup_race(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                # The official Docker bind mount can keep the database
                # unreadable for several seconds while it changes ownership.
                # Keep this longer than the previously observed 12-second
                # retry window so the test protects the live race fix.
                if call_count <= 250:
                    raise sqlite3.OperationalError("unable to open database file")
                return real_connect(*args, **kwargs)

            with patch.object(sqlite_compaction.sqlite3, "connect", side_effect=connect_with_startup_race), patch(
                "codeskill_rebuild.sqlite_compaction.sleep"
            ) as wait:
                self.assertIsNone(detector("trial-1", 0, 1, {"messages": []}))
            self.assertEqual(call_count, 251)
            self.assertEqual(wait.call_count, 250)
            wait.assert_called_with(0.1)

    def test_sqlite_query_retries_until_official_schema_install_finishes(self) -> None:
        import codeskill_rebuild.sqlite_compaction as sqlite_compaction

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database_path = root / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
            database_path.parent.mkdir(parents=True)
            database_path.touch()
            detector = SqliteTranscriptCompactionDetector(
                location_provider=lambda: sqlite_location(database_path),
                evidence_dir=root / "evidence",
            )
            with patch.object(sqlite_compaction, "sleep") as wait:
                def finish_schema_install(_delay: float) -> None:
                    append_sqlite_event(database_path, "native-1", 1, {"type": "message", "id": "user-1"})

                wait.side_effect = finish_schema_install
                self.assertIsNone(detector("trial-1", 0, 1, {"messages": []}))
            wait.assert_called_once_with(0.1)

    def test_canonical_openclaw_sessions_marker_resolves_its_agent_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_path = root / "agents" / "main" / "sessions" / "sessions.json"
            database_path = root / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
            append_sqlite_event(database_path, "native-1", 1, {"type": "message", "id": "user-1"})
            detector = SqliteTranscriptCompactionDetector(
                location_provider=lambda: {"session_file": f"sqlite:main:native-1:{store_path}"},
                evidence_dir=root / "evidence",
            )
            self.assertIsNone(detector("trial-1", 0, 1, {"messages": []}))
            state = read_json(root / "evidence" / "native_compaction" / "detector-state.json")
        self.assertEqual(state["active_location"]["database_path"], str(database_path.resolve()))

    def test_new_sqlite_compaction_is_bound_to_one_proxy_transition_without_copying_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database_path = root / "openclaw-agent.sqlite"
            append_sqlite_event(database_path, "native-1", 1, {"type": "message", "id": "user-1"})
            detector = SqliteTranscriptCompactionDetector(
                location_provider=lambda: sqlite_location(database_path),
                evidence_dir=root / "evidence",
            )
            self.assertIsNone(detector("trial-1", 0, 1, {"messages": []}))
            append_sqlite_event(database_path, "native-1", 2, compaction("compact-1"))
            evidence = detector("trial-1", 1, 2, {"messages": [{"role": "assistant", "content": "next"}]})
            assert evidence is not None
            self.assertEqual(evidence["kind"], "native_compaction")
            self.assertEqual(evidence["compaction_id"], "compact-1")
            self.assertIn("#session=native-1#seq=2#sha256=", evidence["native_session_event_ref"])
            self.assertEqual(evidence["entry"]["seq"], 2)
            self.assertNotIn("native summary", json.dumps(evidence))
            pending = read_json(root / "evidence" / "native_compaction" / "detector-state.json")
            self.assertEqual(pending["pending"]["compaction_id"], "compact-1")
            detector.confirm_transition(
                evidence,
                {
                    "attempt_ordinal": 2,
                    "outcome": "forwardable",
                    "compaction_evidence": evidence,
                },
            )
            self.assertIsNone(detector("trial-1", 2, 3, {"messages": []}))

    def test_preexisting_sqlite_compaction_is_baselined_and_a_rewrite_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database_path = root / "openclaw-agent.sqlite"
            original = compaction("compact-1")
            append_sqlite_event(database_path, "native-1", 1, original)
            detector = SqliteTranscriptCompactionDetector(
                location_provider=lambda: sqlite_location(database_path),
                evidence_dir=root / "evidence",
            )
            self.assertIsNone(detector("trial-1", 0, 1, {"messages": []}))
            self.assertIsNone(detector("trial-1", 1, 2, {"messages": []}))
            rewritten = {**original, "summary": "the source event changed after baseline"}
            update_sqlite_event(database_path, "native-1", 1, rewritten)
            with self.assertRaisesRegex(NativeCompactionEvidenceError, "changed after it was observed"):
                detector("trial-1", 2, 3, {"messages": []})

    def test_sqlite_compaction_sequence_relocation_preserves_identity_and_allows_new_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database_path = root / "openclaw-agent.sqlite"
            original = compaction("compact-1")
            append_sqlite_event(database_path, "native-1", 1, {"type": "message", "id": "user-1"})
            append_sqlite_event(database_path, "native-1", 2, original)
            detector = SqliteTranscriptCompactionDetector(
                location_provider=lambda: sqlite_location(database_path),
                evidence_dir=root / "evidence",
            )
            self.assertIsNone(detector("trial-1", 0, 1, {"messages": []}))

            connection = sqlite3.connect(database_path)
            try:
                raw = connection.execute(
                    "SELECT event_json, created_at FROM transcript_events WHERE session_id = ? AND seq = ?",
                    ("native-1", 2),
                ).fetchone()
                assert raw is not None
                connection.execute(
                    "DELETE FROM transcript_events WHERE session_id = ? AND seq = ?",
                    ("native-1", 2),
                )
                connection.execute(
                    "INSERT INTO transcript_events (session_id, seq, event_json, created_at) VALUES (?, ?, ?, ?)",
                    ("native-1", 5, raw[0], raw[1]),
                )
                connection.commit()
            finally:
                connection.close()

            self.assertIsNone(detector("trial-1", 1, 2, {"messages": []}))
            state = read_json(root / "evidence" / "native_compaction" / "detector-state.json")
            tracked = next(iter(state["tracked_sessions"].values()))
            self.assertEqual(tracked["seen_by_seq"], {"5": tracked["seen"]["compact-1"]})
            self.assertEqual(tracked["seq_relocations"][0]["from_seq"], 2)
            self.assertEqual(tracked["seq_relocations"][0]["to_seq"], 5)

            append_sqlite_event(database_path, "native-1", 6, compaction("compact-2"))
            evidence = detector("trial-1", 2, 3, {"messages": []})
            assert evidence is not None
            self.assertEqual(evidence["compaction_id"], "compact-2")
            self.assertEqual(evidence["entry"]["seq"], 6)

    def test_sqlite_compaction_sequence_disappearance_without_identity_replacement_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database_path = root / "openclaw-agent.sqlite"
            append_sqlite_event(database_path, "native-1", 1, compaction("compact-1"))
            detector = SqliteTranscriptCompactionDetector(
                location_provider=lambda: sqlite_location(database_path),
                evidence_dir=root / "evidence",
            )
            self.assertIsNone(detector("trial-1", 0, 1, {"messages": []}))
            connection = sqlite3.connect(database_path)
            try:
                connection.execute(
                    "DELETE FROM transcript_events WHERE session_id = ? AND seq = ?",
                    ("native-1", 1),
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(NativeCompactionEvidenceError, "disappeared after it was observed"):
                detector("trial-1", 1, 2, {"messages": []})

    def test_malformed_sqlite_marker_or_event_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            malformed_marker = SqliteTranscriptCompactionDetector(
                location_provider=lambda: {"session_file": "sqlite:main:missing"},
                evidence_dir=root / "marker-evidence",
            )
            with self.assertRaisesRegex(NativeCompactionEvidenceError, "marker"):
                malformed_marker("trial-1", 0, 1, {"messages": []})

            database_path = root / "openclaw-agent.sqlite"
            append_sqlite_event(database_path, "native-1", 1, {"type": "message", "id": "user-1"})
            connection = sqlite3.connect(database_path)
            try:
                connection.execute(
                    "INSERT INTO transcript_events (session_id, seq, event_json, created_at) VALUES (?, ?, ?, ?)",
                    ("native-1", 2, "{", 1_725_724_800_002),
                )
                connection.commit()
            finally:
                connection.close()
            malformed_event = SqliteTranscriptCompactionDetector(
                location_provider=lambda: sqlite_location(database_path),
                evidence_dir=root / "event-evidence",
            )
            with self.assertRaisesRegex(NativeCompactionEvidenceError, "invalid transcript event JSON"):
                malformed_event("trial-1", 0, 1, {"messages": []})


if __name__ == "__main__":
    unittest.main()
