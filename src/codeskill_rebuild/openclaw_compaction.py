"""Fail-closed native OpenClaw session compaction evidence.

The durable overlay must never infer that a missing task or event anchor was
removed by compaction.  This module observes either the legacy native JSONL
or current SQLite ``transcript_events`` at each proxy request boundary.  It
returns evidence only for a *new* ``compaction`` entry between the prior
forwarded request and the current proxy attempt.  The native session remains
the raw record; the sidecar stores only identifiers, hashes, and source
references so it does not duplicate the summary into CODESKILL evidence.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

from .native_compaction_common import NativeCompactionEvidenceError, read_json_line
from .types import canonical_json, read_json, sha256_file, sha256_text, utc_now, write_json


SessionLocationProvider = Callable[[], dict[str, Any] | None]


def _normalise_location(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise NativeCompactionEvidenceError("native session location provider must return an object or None")
    session_file = value.get("session_file")
    if not isinstance(session_file, (str, Path)) or not str(session_file):
        raise NativeCompactionEvidenceError("native session location requires a nonempty session_file")
    result: dict[str, Any] = {"session_file": str(Path(session_file).resolve())}
    for field in ("session_id", "previous_session_id"):
        item = value.get(field)
        if item is not None and (not isinstance(item, str) or not item):
            raise NativeCompactionEvidenceError(f"native session location {field} must be a nonempty string when provided")
        result[field] = item
    previous = value.get("previous_session_file")
    if previous is not None:
        if not isinstance(previous, (str, Path)) or not str(previous):
            raise NativeCompactionEvidenceError("previous_session_file must be a nonempty path when provided")
        result["previous_session_file"] = str(Path(previous).resolve())
    else:
        result["previous_session_file"] = None
    return result


def _validated_compaction(entry: dict[str, Any], *, path: Path, line_number: int, session_id: str | None) -> dict[str, Any]:
    compaction_id = entry.get("id")
    first_kept = entry.get("firstKeptEntryId")
    timestamp = entry.get("timestamp")
    parent = entry.get("parentId")
    if not isinstance(compaction_id, str) or not compaction_id:
        raise NativeCompactionEvidenceError(f"{path}:{line_number} compaction requires nonempty id")
    if not isinstance(first_kept, str) or not first_kept:
        raise NativeCompactionEvidenceError(f"{path}:{line_number} compaction requires nonempty firstKeptEntryId")
    if not isinstance(timestamp, str) or not timestamp:
        raise NativeCompactionEvidenceError(f"{path}:{line_number} compaction requires nonempty timestamp")
    if parent is not None and (not isinstance(parent, str) or not parent):
        raise NativeCompactionEvidenceError(f"{path}:{line_number} compaction parentId must be null or a nonempty string")
    entry_hash = sha256_text(canonical_json(entry))
    identity = session_id or str(path)
    return {
        "record_key": f"{identity}:{compaction_id}",
        "compaction_id": compaction_id,
        "path": str(path),
        "line_number": line_number,
        "entry_sha256": entry_hash,
        "id": compaction_id,
        "parentId": parent,
        "firstKeptEntryId": first_kept,
        "timestamp": timestamp,
        "tokensBefore": entry.get("tokensBefore"),
    }


def _read_session(path: Path, *, expected_session_id: str | None) -> tuple[str | None, list[dict[str, Any]], str | None]:
    """Read complete JSONL entries and return only validated compactions.

    A missing file is normal before OpenClaw first creates its native session.
    Any malformed existing JSONL is evidence corruption, not an empty session.
    """
    if not path.exists():
        return expected_session_id, [], None
    if not path.is_file():
        raise NativeCompactionEvidenceError(f"native session path is not a regular file: {path}")
    header_session_id: str | None = None
    compactions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            text = raw.strip()
            if not text:
                continue
            try:
                entry = read_json_line(text)
            except ValueError as error:
                raise NativeCompactionEvidenceError(f"{path}:{line_number} invalid native session JSONL: {error}") from error
            if entry.get("type") == "session" and header_session_id is None:
                header = entry.get("id")
                if not isinstance(header, str) or not header:
                    raise NativeCompactionEvidenceError(f"{path}:{line_number} session header requires nonempty id")
                header_session_id = header
            if entry.get("type") == "compaction":
                record = _validated_compaction(entry, path=path, line_number=line_number, session_id=expected_session_id or header_session_id)
                if record["compaction_id"] in seen_ids:
                    raise NativeCompactionEvidenceError(f"{path} has duplicate compaction id {record['compaction_id']}")
                seen_ids.add(record["compaction_id"])
                compactions.append(record)
    session_id = expected_session_id or header_session_id
    if expected_session_id is not None and header_session_id is not None and expected_session_id != header_session_id:
        raise NativeCompactionEvidenceError(
            f"native session location id {expected_session_id} does not match JSONL header {header_session_id} in {path}"
        )
    return session_id, compactions, sha256_file(path)


class SessionJsonlCompactionDetector:
    """Durably observe native compaction records for one isolated trial.

    ``location_provider`` may include a session successor with
    ``previous_session_file`` and ``previous_session_id``.  A file change
    without that explicit predecessor relation is rejected: a new unrelated
    JSONL must never lend its old compaction marker to this trial.
    """

    def __init__(self, *, location_provider: SessionLocationProvider, evidence_dir: Path) -> None:
        self.location_provider = location_provider
        self.evidence_dir = Path(evidence_dir)
        self.state_path = self.evidence_dir / "native_compaction" / "detector-state.json"
        self.state = self._load_state()

    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "r008_native_session_jsonl_compaction_detector",
            "created_at_utc": utc_now(),
            "trial_id": None,
            "initialized": False,
            "active_location": None,
            "tracked_sessions": {},
            # A provider runs before overlay.prepare.  A just-observed native
            # compaction remains pending until that exact overlay attempt has
            # durably recorded a forwardable or terminal disposition.
            "pending": None,
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._empty_state()
        state = read_json(self.state_path)
        if state.get("kind") != "r008_native_session_jsonl_compaction_detector" or not isinstance(state.get("tracked_sessions"), dict):
            raise NativeCompactionEvidenceError("native compaction detector state has an invalid schema")
        return state

    def _track(self, path: str, session_id: str | None, *, compactions: list[dict[str, Any]], file_sha256: str | None) -> dict[str, Any]:
        tracked = self.state["tracked_sessions"].get(path)
        if tracked is None:
            tracked = {"session_id": session_id, "seen": {}, "last_file_sha256": None}
            self.state["tracked_sessions"][path] = tracked
        elif tracked.get("session_id") not in {None, session_id}:
            raise NativeCompactionEvidenceError(f"native session identity changed in-place for {path}")
        if tracked.get("session_id") is None:
            tracked["session_id"] = session_id
        seen = tracked.setdefault("seen", {})
        if not isinstance(seen, dict):
            raise NativeCompactionEvidenceError("native compaction detector state has invalid seen records")
        for record in compactions:
            old = seen.get(record["compaction_id"])
            if old is not None and old != record["entry_sha256"]:
                raise NativeCompactionEvidenceError(
                    f"native compaction {record['compaction_id']} changed after it was observed in {path}"
                )
        tracked["last_file_sha256"] = file_sha256
        return tracked

    @staticmethod
    def _new_records(tracked: dict[str, Any], compactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen = tracked["seen"]
        return [record for record in compactions if record["compaction_id"] not in seen]

    @staticmethod
    def _mark_seen(tracked: dict[str, Any], compactions: list[dict[str, Any]]) -> None:
        for record in compactions:
            tracked["seen"][record["compaction_id"]] = record["entry_sha256"]

    def _write_observation(self, attempt_ordinal: int, observation: dict[str, Any]) -> None:
        write_json(self.evidence_dir / "native_compaction" / f"attempt-{attempt_ordinal:04d}.json", observation)

    def confirm_transition(
        self,
        evidence: dict[str, Any],
        overlay_record: dict[str, Any],
        *,
        overlay_attempt_path: Path | None = None,
    ) -> None:
        """Consume a pending record only after the matching overlay attempt.

        ``DurableProxyService`` invokes this immediately after
        ``overlay.prepare`` has written its attempt record, including when the
        prepare path reaches a recorded terminal context/overlay error.  This
        prevents a failed preflight from silently consuming the sole native
        proof needed by its retry, while also preventing later unrelated
        anchor loss from reusing a stale compaction record.
        """
        pending = self.state.get("pending")
        if not isinstance(pending, dict):
            raise NativeCompactionEvidenceError("no pending native compaction transition is available to confirm")
        if not isinstance(evidence, dict) or evidence.get("compaction_id") != pending.get("compaction_id"):
            raise NativeCompactionEvidenceError("overlay attempted to confirm a different native compaction")
        if canonical_json(overlay_record.get("compaction_evidence")) != canonical_json(evidence):
            raise NativeCompactionEvidenceError("overlay record does not retain the pending native compaction evidence")
        if overlay_record.get("attempt_ordinal") != evidence.get("after_proxy_attempt_ordinal"):
            raise NativeCompactionEvidenceError("overlay record ordinal does not match the pending native compaction transition")
        outcome = overlay_record.get("outcome")
        if outcome not in {"forwardable", "context_or_overlay_error"}:
            raise NativeCompactionEvidenceError("overlay record has no terminal disposition for pending native compaction")

        entry = pending.get("entry")
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise NativeCompactionEvidenceError("pending native compaction state is malformed")
        tracked = self.state["tracked_sessions"].get(entry["path"])
        if not isinstance(tracked, dict):
            raise NativeCompactionEvidenceError("pending native compaction no longer has tracked session state")
        self._mark_seen(tracked, [entry])
        carried = overlay_record.get("carried_priors")
        current = carried.get("current_transition_evidence") if isinstance(carried, dict) else None
        retired = overlay_record.get("retired_event_priors")
        retired_current = retired.get("current_transition_evidence") if isinstance(retired, dict) else None
        if canonical_json(current) == canonical_json(evidence):
            disposition = "relocation_persisted"
        elif canonical_json(retired_current) == canonical_json(evidence):
            # R012 deliberately distinguishes this from task relocation: the
            # event prior is durably retired and must not be carried forward.
            disposition = "event_retirement_persisted"
        else:
            disposition = "terminal_without_relocation"
        observation_path = self.evidence_dir / "native_compaction" / f"attempt-{int(evidence['after_proxy_attempt_ordinal']):04d}.json"
        observation = read_json(observation_path)
        observation["overlay_disposition"] = {
            "kind": disposition,
            "overlay_attempt_ordinal": overlay_record["attempt_ordinal"],
            "overlay_outcome": outcome,
            "overlay_attempt_path": str(overlay_attempt_path) if overlay_attempt_path is not None else None,
            "overlay_attempt_sha256": sha256_text(canonical_json(overlay_record)),
            "confirmed_at_utc": utc_now(),
        }
        self._write_observation(int(evidence["after_proxy_attempt_ordinal"]), observation)
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
            raise NativeCompactionEvidenceError("trial_id is required for native compaction evidence")
        if not isinstance(before_forwarded_request_ordinal, int) or before_forwarded_request_ordinal < 0:
            raise NativeCompactionEvidenceError("before_forwarded_request_ordinal must be a nonnegative integer")
        if not isinstance(after_proxy_attempt_ordinal, int) or after_proxy_attempt_ordinal <= before_forwarded_request_ordinal:
            raise NativeCompactionEvidenceError("after_proxy_attempt_ordinal must be later than before_forwarded_request_ordinal")
        if self.state.get("trial_id") not in {None, trial_id}:
            raise NativeCompactionEvidenceError("native compaction detector state belongs to a different trial")

        location = _normalise_location(self.location_provider())
        observation: dict[str, Any] = {
            "schema_version": 1,
            "kind": "r008_native_compaction_observation",
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
            self.state["trial_id"] = trial_id
            self.state["initialized"] = True
            write_json(self.state_path, self.state)
            return None

        active = self.state.get("active_location")
        current_path = location["session_file"]
        current_changed = isinstance(active, dict) and active.get("session_file") != current_path
        previous_path = location.get("previous_session_file")
        if current_changed:
            if not previous_path or previous_path != active.get("session_file"):
                raise NativeCompactionEvidenceError("native session file changed without an explicit predecessor relationship")
            if location.get("previous_session_id") != active.get("session_id"):
                raise NativeCompactionEvidenceError("native session successor does not name the prior active session id")

        paths_to_observe = [current_path]
        if current_changed and previous_path is not None:
            paths_to_observe.append(previous_path)
        snapshot: dict[str, tuple[str | None, list[dict[str, Any]], str | None]] = {}
        for path_string in paths_to_observe:
            expected_id = location.get("session_id") if path_string == current_path else location.get("previous_session_id")
            snapshot[path_string] = _read_session(Path(path_string), expected_session_id=expected_id)

        # If the first few proxy boundaries did not know the session path,
        # later discovering an already-populated session cannot turn historic
        # compaction into current evidence.  Adapter setup must provide the
        # planned file before the first call when it needs first-call proof.
        if not self.state.get("initialized") or active is None:
            for path_string, (session_id, compactions, file_sha256) in snapshot.items():
                tracked = self._track(path_string, session_id, compactions=compactions, file_sha256=file_sha256)
                self._mark_seen(tracked, compactions)
            self.state.update({"trial_id": trial_id, "initialized": True, "active_location": deepcopy(location)})
            observation["outcome"] = "baseline_established"
            observation["baseline_compaction_ids"] = sorted(
                record["compaction_id"] for _sid, records, _sha in snapshot.values() for record in records
            )
            self._write_observation(after_proxy_attempt_ordinal, observation)
            write_json(self.state_path, self.state)
            return None

        pending = self.state.get("pending")
        if pending is not None:
            raise NativeCompactionEvidenceError(
                "pending native compaction evidence lacks the matching overlay disposition; refusing stale reuse"
            )

        new_records: list[dict[str, Any]] = []
        for path_string, (session_id, compactions, file_sha256) in snapshot.items():
            tracked = self._track(path_string, session_id, compactions=compactions, file_sha256=file_sha256)
            new_records.extend(self._new_records(tracked, compactions))
        if len(new_records) > 1:
            raise NativeCompactionEvidenceError("multiple new native compactions occurred in one proxy transition")

        self.state.update({"trial_id": trial_id, "active_location": deepcopy(location)})

        if not new_records:
            observation["outcome"] = "no_new_compaction"
            self._write_observation(after_proxy_attempt_ordinal, observation)
            write_json(self.state_path, self.state)
            return None

        selected = new_records[0]
        observation.update(
            {
                "outcome": "new_compaction_confirmed",
                "new_compaction_ids": [selected["compaction_id"]],
                "selected_compaction_id": selected["compaction_id"],
                "selected_native_session_event_ref": f"{selected['path']}#line={selected['line_number']}#sha256={selected['entry_sha256']}",
            }
        )
        if current_changed:
            observation["session_rotation"] = {
                "from_session_file": active["session_file"],
                "from_session_id": active.get("session_id"),
                "to_session_file": current_path,
                "to_session_id": location.get("session_id"),
            }
        self.state["pending"] = {
            "compaction_id": selected["compaction_id"],
            "entry": deepcopy(selected),
            "created_at_utc": observation["observed_at_utc"],
            "before_forwarded_request_ordinal": before_forwarded_request_ordinal,
            "after_proxy_attempt_ordinal": after_proxy_attempt_ordinal,
        }
        self._write_observation(after_proxy_attempt_ordinal, observation)
        write_json(self.state_path, self.state)
        result: dict[str, Any] = {
            "kind": "native_compaction",
            "confirmed": True,
            "compaction_id": selected["compaction_id"],
            "native_session_event_ref": observation["selected_native_session_event_ref"],
            "observed_at_utc": observation["observed_at_utc"],
            "before_forwarded_request_ordinal": before_forwarded_request_ordinal,
            "after_proxy_attempt_ordinal": after_proxy_attempt_ordinal,
            "entry": {
                key: selected[key]
                for key in ("id", "parentId", "firstKeptEntryId", "timestamp", "tokensBefore", "entry_sha256")
            },
        }
        if current_changed:
            result["session_rotation"] = deepcopy(observation["session_rotation"])
        return result


# Keep the existing compaction module as the public integration surface while
# placing the SQLite-specific parser in a focused, independently testable file.
from .sqlite_compaction import SqliteTranscriptCompactionDetector  # noqa: E402
