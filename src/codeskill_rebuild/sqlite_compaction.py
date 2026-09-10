"""Read-only evidence adapter for OpenClaw's SQLite transcript backend.

OpenClaw now stores session transcript rows in an agent SQLite database and
identifies the active session with ``sqlite:<agent>:<session>:<store-path>``.
The adapter mirrors OpenClaw's marker and store-path resolution, then reads
only the target session's additive ``transcript_events`` rows.  It never
persists a compaction summary in CODESKILL evidence.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
import re
import sqlite3
from typing import Any

from .native_compaction_common import NativeCompactionEvidenceError, read_json_line
from .types import canonical_json, read_json, sha256_text, utc_now, write_json


SessionLocationProvider = Callable[[], dict[str, Any] | None]


def _normalize_agent_id(value: str | None) -> str:
    """Match OpenClaw normalization-core's filesystem-safe agent ID."""
    trimmed = (value or "").strip()
    if not trimmed:
        return "main"
    normalized = trimmed.lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", trimmed, flags=re.IGNORECASE):
        return normalized
    normalized = re.sub(r"[^a-z0-9_-]+", "-", normalized)
    normalized = re.sub(r"^-+", "", normalized)
    normalized = re.sub(r"-+$", "", normalized)
    return normalized[:64] or "main"


def _agent_id_from_database_path(database_path: Path) -> str | None:
    """Match OpenClaw's direct ``openclaw-agent.sqlite`` owner inference."""
    if database_path.name != "openclaw-agent.sqlite" or database_path.parent.name != "agent":
        return None
    agent_dir = database_path.parent.parent
    if agent_dir.parent.name != "agents":
        return None
    return _normalize_agent_id(agent_dir.name)


def _resolve_database_path(store_path: Path, *, agent_id: str) -> Path:
    """Mirror OpenClaw ``resolveSqliteTargetFromSessionStorePath``."""
    if store_path.name == "openclaw-agent.sqlite" or store_path.suffix == ".sqlite":
        owner = _agent_id_from_database_path(store_path)
        if owner is not None and owner != agent_id:
            raise NativeCompactionEvidenceError(
                f"native SQLite marker agent {agent_id} disagrees with store-path agent {owner}"
            )
        return store_path
    sessions_dir = store_path.parent
    if store_path.name == "sessions.json" and sessions_dir.name == "sessions":
        agent_dir = sessions_dir.parent
        if agent_dir.parent.name == "agents":
            owner = _normalize_agent_id(agent_dir.name)
            if owner != agent_id:
                raise NativeCompactionEvidenceError(
                    f"native SQLite marker agent {agent_id} disagrees with store-path agent {owner}"
                )
            return (agent_dir / "agent" / "openclaw-agent.sqlite").resolve()
    # OpenClaw treats a non-canonical ``sessions.json`` as the historical
    # agent-store alias, whose database base name is ``openclaw-agent``.
    # Other legacy names use their own filename stem.
    sqlite_stem = "openclaw-agent" if store_path.name == "sessions.json" else store_path.stem or "openclaw-agent"
    if agent_id != "main" and _normalize_agent_id(sqlite_stem) != agent_id:
        sqlite_stem = f"{sqlite_stem}.{agent_id}"
    return (sessions_dir / f"{sqlite_stem}.sqlite").resolve()


def _normalise_location(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise NativeCompactionEvidenceError("native SQLite session location provider must return an object or None")
    marker = value.get("session_file")
    if not isinstance(marker, str) or not marker.strip():
        raise NativeCompactionEvidenceError("native SQLite session location requires a nonempty sqlite session marker")
    marker = marker.strip()
    if not marker.startswith("sqlite:"):
        raise NativeCompactionEvidenceError("native SQLite session location requires an OpenClaw sqlite marker")
    parts = marker.split(":", 3)
    if len(parts) != 4 or parts[0] != "sqlite" or not parts[1] or not parts[2] or not parts[3]:
        raise NativeCompactionEvidenceError("native SQLite session marker is malformed")
    raw_agent_id, session_id, store_path_text = parts[1:]
    agent_id = _normalize_agent_id(raw_agent_id)
    store_path = Path(store_path_text).resolve()
    database_path = _resolve_database_path(store_path, agent_id=agent_id)
    return {
        "session_file": marker,
        "agent_id": agent_id,
        "session_id": session_id,
        "store_path": str(store_path),
        "database_path": str(database_path),
    }


def _validated_compaction(
    entry: dict[str, Any], *, database_path: Path, session_id: str, seq: int, raw_event_json: str
) -> dict[str, Any]:
    compaction_id = entry.get("id")
    first_kept = entry.get("firstKeptEntryId")
    timestamp = entry.get("timestamp")
    parent = entry.get("parentId")
    if not isinstance(compaction_id, str) or not compaction_id:
        raise NativeCompactionEvidenceError(f"{database_path}:seq={seq} compaction requires nonempty id")
    if not isinstance(first_kept, str) or not first_kept:
        raise NativeCompactionEvidenceError(f"{database_path}:seq={seq} compaction requires nonempty firstKeptEntryId")
    if not isinstance(timestamp, str) or not timestamp:
        raise NativeCompactionEvidenceError(f"{database_path}:seq={seq} compaction requires nonempty timestamp")
    if parent is not None and (not isinstance(parent, str) or not parent):
        raise NativeCompactionEvidenceError(f"{database_path}:seq={seq} compaction parentId must be null or a nonempty string")
    # Hash the exact stored text, rather than a normalized rendering.  The
    # summary stays inside the native row; the sidecar gets only this hash.
    entry_hash = sha256_text(raw_event_json)
    session_key = f"{database_path}#session={session_id}"
    return {
        "record_key": f"{session_key}#seq={seq}",
        "session_key": session_key,
        "compaction_id": compaction_id,
        "database_path": str(database_path),
        "session_id": session_id,
        "seq": seq,
        "entry_sha256": entry_hash,
        "id": compaction_id,
        "parentId": parent,
        "firstKeptEntryId": first_kept,
        "timestamp": timestamp,
        "tokensBefore": entry.get("tokensBefore"),
    }


def _read_compactions(location: dict[str, Any]) -> list[dict[str, Any]]:
    database_path = Path(location["database_path"])
    session_id = location["session_id"]
    if not database_path.exists():
        raise NativeCompactionEvidenceError(f"native SQLite database does not exist: {database_path}")
    if not database_path.is_file():
        raise NativeCompactionEvidenceError(f"native SQLite database is not a regular file: {database_path}")
    try:
        connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True, timeout=1)
    except (OSError, sqlite3.Error) as error:
        raise NativeCompactionEvidenceError(f"cannot open native SQLite database read-only: {database_path}: {error}") from error
    try:
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute(
            "SELECT seq, event_json FROM transcript_events WHERE session_id = ? ORDER BY seq ASC",
            (session_id,),
        ).fetchall()
    except sqlite3.Error as error:
        raise NativeCompactionEvidenceError(
            f"cannot read native SQLite transcript_events for session {session_id}: {error}"
        ) from error
    finally:
        connection.close()

    previous_seq = -1
    compactions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_seq, raw_event_json in rows:
        if isinstance(raw_seq, bool) or not isinstance(raw_seq, int) or raw_seq < 0 or raw_seq <= previous_seq:
            raise NativeCompactionEvidenceError(f"native SQLite transcript has invalid sequence for session {session_id}")
        previous_seq = raw_seq
        if not isinstance(raw_event_json, str):
            raise NativeCompactionEvidenceError(f"native SQLite transcript seq={raw_seq} has a non-text event_json")
        try:
            entry = read_json_line(raw_event_json)
        except ValueError as error:
            raise NativeCompactionEvidenceError(
                f"native SQLite transcript seq={raw_seq} has invalid transcript event JSON: {error}"
            ) from error
        if entry.get("type") != "compaction":
            continue
        record = _validated_compaction(
            entry,
            database_path=database_path,
            session_id=session_id,
            seq=raw_seq,
            raw_event_json=raw_event_json,
        )
        if record["compaction_id"] in seen_ids:
            raise NativeCompactionEvidenceError(
                f"native SQLite transcript has duplicate compaction id {record['compaction_id']} for session {session_id}"
            )
        seen_ids.add(record["compaction_id"])
        compactions.append(record)
    return compactions


class SqliteTranscriptCompactionDetector:
    """Fail-closed, read-only detector for OpenClaw SQLite transcript rows.

    The current supported native path retains one SQLite session across
    compaction.  A marker change after baseline therefore fails closed instead
    of assuming an unrelated session is a successor.
    """

    def __init__(self, *, location_provider: SessionLocationProvider, evidence_dir: Path) -> None:
        self.location_provider = location_provider
        self.evidence_dir = Path(evidence_dir)
        self.state_path = self.evidence_dir / "native_compaction" / "detector-state.json"
        self.state = self._load_state()

    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "r012_native_sqlite_transcript_compaction_detector",
            "created_at_utc": utc_now(),
            "trial_id": None,
            "initialized": False,
            "active_location": None,
            "tracked_sessions": {},
            "pending": None,
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._empty_state()
        state = read_json(self.state_path)
        if state.get("kind") != "r012_native_sqlite_transcript_compaction_detector" or not isinstance(
            state.get("tracked_sessions"), dict
        ):
            raise NativeCompactionEvidenceError("native SQLite compaction detector state has an invalid schema")
        return state

    def _track(self, location: dict[str, Any], compactions: list[dict[str, Any]]) -> dict[str, Any]:
        session_key = f"{location['database_path']}#session={location['session_id']}"
        tracked = self.state["tracked_sessions"].get(session_key)
        if tracked is None:
            tracked = {
                "agent_id": location["agent_id"],
                "session_id": location["session_id"],
                "database_path": location["database_path"],
                "seen": {},
                "seen_by_seq": {},
            }
            self.state["tracked_sessions"][session_key] = tracked
        elif (
            tracked.get("agent_id") != location["agent_id"]
            or tracked.get("session_id") != location["session_id"]
            or tracked.get("database_path") != location["database_path"]
        ):
            raise NativeCompactionEvidenceError("native SQLite transcript identity changed in-place")
        seen = tracked.get("seen")
        seen_by_seq = tracked.get("seen_by_seq")
        if not isinstance(seen, dict) or not isinstance(seen_by_seq, dict):
            raise NativeCompactionEvidenceError("native SQLite compaction detector state has invalid seen records")
        current_by_seq = {str(record["seq"]): record for record in compactions}
        for seq, old_hash in seen_by_seq.items():
            current = current_by_seq.get(seq)
            if current is None:
                raise NativeCompactionEvidenceError(f"native SQLite compaction seq={seq} disappeared after it was observed")
            if current["entry_sha256"] != old_hash:
                raise NativeCompactionEvidenceError(f"native SQLite compaction seq={seq} changed after it was observed")
        for record in compactions:
            old = seen.get(record["compaction_id"])
            if old is not None and old != record["entry_sha256"]:
                raise NativeCompactionEvidenceError(
                    f"native SQLite compaction {record['compaction_id']} changed after it was observed"
                )
        return tracked

    @staticmethod
    def _mark_seen(tracked: dict[str, Any], compactions: list[dict[str, Any]]) -> None:
        for record in compactions:
            tracked["seen"][record["compaction_id"]] = record["entry_sha256"]
            tracked["seen_by_seq"][str(record["seq"])] = record["entry_sha256"]

    def _write_observation(self, attempt_ordinal: int, observation: dict[str, Any]) -> None:
        write_json(self.evidence_dir / "native_compaction" / f"attempt-{attempt_ordinal:04d}.json", observation)

    def confirm_transition(
        self,
        evidence: dict[str, Any],
        overlay_record: dict[str, Any],
        *,
        overlay_attempt_path: Path | None = None,
    ) -> None:
        pending = self.state.get("pending")
        if not isinstance(pending, dict):
            raise NativeCompactionEvidenceError("no pending native SQLite compaction transition is available to confirm")
        if not isinstance(evidence, dict) or evidence.get("compaction_id") != pending.get("compaction_id"):
            raise NativeCompactionEvidenceError("overlay attempted to confirm a different native SQLite compaction")
        if canonical_json(overlay_record.get("compaction_evidence")) != canonical_json(evidence):
            raise NativeCompactionEvidenceError("overlay record does not retain the pending native SQLite compaction evidence")
        if overlay_record.get("attempt_ordinal") != evidence.get("after_proxy_attempt_ordinal"):
            raise NativeCompactionEvidenceError("overlay record ordinal does not match the pending native SQLite transition")
        if overlay_record.get("outcome") not in {"forwardable", "context_or_overlay_error"}:
            raise NativeCompactionEvidenceError("overlay record has no terminal disposition for pending native SQLite compaction")
        entry = pending.get("entry")
        if not isinstance(entry, dict) or not isinstance(entry.get("session_key"), str):
            raise NativeCompactionEvidenceError("pending native SQLite compaction state is malformed")
        tracked = self.state["tracked_sessions"].get(entry["session_key"])
        if not isinstance(tracked, dict):
            raise NativeCompactionEvidenceError("pending native SQLite compaction no longer has tracked session state")
        self._mark_seen(tracked, [entry])
        carried = overlay_record.get("carried_priors")
        current = carried.get("current_transition_evidence") if isinstance(carried, dict) else None
        retired = overlay_record.get("retired_event_priors")
        retired_current = retired.get("current_transition_evidence") if isinstance(retired, dict) else None
        disposition = (
            "relocation_persisted"
            if canonical_json(current) == canonical_json(evidence)
            else "event_retirement_persisted"
            if canonical_json(retired_current) == canonical_json(evidence)
            else "terminal_without_relocation"
        )
        attempt = int(evidence["after_proxy_attempt_ordinal"])
        observation = read_json(self.evidence_dir / "native_compaction" / f"attempt-{attempt:04d}.json")
        observation["overlay_disposition"] = {
            "kind": disposition,
            "overlay_attempt_ordinal": overlay_record["attempt_ordinal"],
            "overlay_outcome": overlay_record["outcome"],
            "overlay_attempt_path": str(overlay_attempt_path) if overlay_attempt_path is not None else None,
            "overlay_attempt_sha256": sha256_text(canonical_json(overlay_record)),
            "confirmed_at_utc": utc_now(),
        }
        self._write_observation(attempt, observation)
        self.state["pending"] = None
        write_json(self.state_path, self.state)

    def __call__(
        self,
        trial_id: str,
        before_forwarded_request_ordinal: int,
        after_proxy_attempt_ordinal: int,
        _native_payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not isinstance(trial_id, str) or not trial_id:
            raise NativeCompactionEvidenceError("trial_id is required for native SQLite compaction evidence")
        if not isinstance(before_forwarded_request_ordinal, int) or before_forwarded_request_ordinal < 0:
            raise NativeCompactionEvidenceError("before_forwarded_request_ordinal must be a nonnegative integer")
        if not isinstance(after_proxy_attempt_ordinal, int) or after_proxy_attempt_ordinal <= before_forwarded_request_ordinal:
            raise NativeCompactionEvidenceError("after_proxy_attempt_ordinal must be later than before_forwarded_request_ordinal")
        if self.state.get("trial_id") not in {None, trial_id}:
            raise NativeCompactionEvidenceError("native SQLite compaction detector state belongs to a different trial")

        location = _normalise_location(self.location_provider())
        observation: dict[str, Any] = {
            "schema_version": 1,
            "kind": "r012_native_sqlite_compaction_observation",
            "trial_id": trial_id,
            "before_forwarded_request_ordinal": before_forwarded_request_ordinal,
            "after_proxy_attempt_ordinal": after_proxy_attempt_ordinal,
            "observed_at_utc": utc_now(),
            "location": deepcopy(location),
            "new_compaction_ids": [],
            "selected_compaction_id": None,
        }
        if location is None:
            observation["outcome"] = "no_native_session_location"
            self._write_observation(after_proxy_attempt_ordinal, observation)
            self.state.update({"trial_id": trial_id, "initialized": True})
            write_json(self.state_path, self.state)
            return None

        active = self.state.get("active_location")
        if isinstance(active, dict) and active.get("session_file") != location["session_file"]:
            raise NativeCompactionEvidenceError(
                "native SQLite session marker changed after baseline without native predecessor evidence"
            )
        compactions = _read_compactions(location)
        tracked = self._track(location, compactions)
        if not self.state.get("initialized") or active is None:
            self._mark_seen(tracked, compactions)
            self.state.update({"trial_id": trial_id, "initialized": True, "active_location": deepcopy(location)})
            observation["outcome"] = "baseline_established"
            observation["baseline_compaction_ids"] = sorted(record["compaction_id"] for record in compactions)
            self._write_observation(after_proxy_attempt_ordinal, observation)
            write_json(self.state_path, self.state)
            return None
        if self.state.get("pending") is not None:
            raise NativeCompactionEvidenceError(
                "pending native SQLite compaction evidence lacks the matching overlay disposition; refusing stale reuse"
            )
        new_records = [record for record in compactions if record["compaction_id"] not in tracked["seen"]]
        if len(new_records) > 1:
            raise NativeCompactionEvidenceError("multiple new native SQLite compactions occurred in one proxy transition")
        self.state.update({"trial_id": trial_id, "active_location": deepcopy(location)})
        if not new_records:
            observation["outcome"] = "no_new_compaction"
            self._write_observation(after_proxy_attempt_ordinal, observation)
            write_json(self.state_path, self.state)
            return None

        selected = new_records[0]
        native_ref = (
            f"{selected['database_path']}#session={selected['session_id']}#seq={selected['seq']}"
            f"#sha256={selected['entry_sha256']}"
        )
        observation.update(
            {
                "outcome": "new_compaction_confirmed",
                "new_compaction_ids": [selected["compaction_id"]],
                "selected_compaction_id": selected["compaction_id"],
                "selected_native_session_event_ref": native_ref,
            }
        )
        self.state["pending"] = {
            "compaction_id": selected["compaction_id"],
            "entry": deepcopy(selected),
            "created_at_utc": observation["observed_at_utc"],
            "before_forwarded_request_ordinal": before_forwarded_request_ordinal,
            "after_proxy_attempt_ordinal": after_proxy_attempt_ordinal,
        }
        self._write_observation(after_proxy_attempt_ordinal, observation)
        write_json(self.state_path, self.state)
        return {
            "kind": "native_compaction",
            "confirmed": True,
            "compaction_id": selected["compaction_id"],
            "native_session_event_ref": native_ref,
            "observed_at_utc": observation["observed_at_utc"],
            "before_forwarded_request_ordinal": before_forwarded_request_ordinal,
            "after_proxy_attempt_ordinal": after_proxy_attempt_ordinal,
            "entry": {
                key: selected[key]
                for key in ("id", "parentId", "firstKeptEntryId", "timestamp", "tokensBefore", "seq", "entry_sha256")
            },
        }
