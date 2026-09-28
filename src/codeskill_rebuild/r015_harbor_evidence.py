"""Fail-closed import of one isolated Harbor/OpenClaw development trial.

The Harbor adapter owns solver execution and the official verifier.  This
module only packages the resulting *already completed* trial for R012's
durable lifecycle ``finish`` command.  It refuses to treat an absent verifier,
an arbitrary JSON file, or a sidecar record from another assignment as finish
evidence.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from .traces import TraceImportError, normalize_openclaw_trial
from .types import canonical_instance_id, read_json, sha256_file, utc_now, write_json


class HarborTrialEvidenceError(RuntimeError):
    """A completed Harbor trial cannot be safely attached to its assignment."""


_NON_FORWARDED_PROXY_OUTCOMES = frozenset(
    {
        "request_limit_rejected",
        "output_limit_rejected",
        "trial_deadline_rejected",
        "native_compaction_evidence_rejected",
        "native_summary_permit_rejected",
        "native_summary_prepare_rejected",
        "native_compaction_confirmation_rejected",
    }
)


def _parse_non_forwarded_terminal_disposition(value: dict[str, Any]) -> dict[str, Any] | None:
    """Parse the terminal schema without deciding whether it is mixed.

    The public record parser and the two importer boundaries need different
    answers for a contradictory record: a record with a normal boundary and a
    terminal rejection must be rejected as *mixed*, while a record with no
    boundary may be accepted only when it is a complete preflight rejection.
    Keeping parsing separate from the boundary-presence rule prevents the
    mixed-record check from becoming unreachable.
    """

    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    if kind == "r008_proxy_limit_rejection":
        outcome = value.get("proxy_outcome")
        if (
            outcome not in _NON_FORWARDED_PROXY_OUTCOMES
            or not isinstance(value.get("native_request"), dict)
            or not isinstance(value.get("error_code"), str)
            or not value["error_code"]
            or not isinstance(value.get("error"), str)
            or not value["error"]
        ):
            return None
        return {
            "kind": "r008_proxy_limit_rejection",
            "forwarded": False,
            "outcome": outcome,
            "reason": "public_preflight_rejection",
        }
    if kind != "r012_actual_upstream_request":
        return None
    outcome = value.get("outcome")
    error_type = value.get("error_type")
    if outcome == "context_or_overlay_error" and error_type == "OverlayInputLimitError":
        candidate = value.get("preflight_candidate_forwarded_request")
        exact = value.get("exact_forwarded_input_tokens")
        maximum = value.get("max_input_tokens")
        uncommitted = value.get("uncommitted_selection")
        error = value.get("error")
        match = re.fullmatch(r"forwarded input (\d+) exceeds configured input budget (\d+)", str(error))
        if (
            not isinstance(value.get("native_request"), dict)
            or not isinstance(candidate, dict)
            or not isinstance(candidate.get("messages"), list)
            or not all(isinstance(message, dict) for message in candidate["messages"])
            or isinstance(exact, bool)
            or not isinstance(exact, int)
            or isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or exact <= maximum
            or not isinstance(uncommitted, dict)
            or match is None
            or int(match.group(1)) != exact
            or int(match.group(2)) != maximum
        ):
            return None
        return {
            "kind": "r012_actual_upstream_request",
            "forwarded": False,
            "outcome": outcome,
            "error_type": error_type,
            "reason": "input_budget_rejected_before_provider_boundary",
        }
    if outcome == "event_skill_budget_error" and error_type == "OverlayEventSkillBudgetError":
        budget = value.get("event_skill_token_budget")
        if not isinstance(budget, dict):
            return None
        active = budget.get("active_event_block_tokens")
        maximum = budget.get("budget")
        uncommitted = value.get("uncommitted_selection")
        if (
            isinstance(active, bool)
            or not isinstance(active, int)
            or isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or active <= maximum
            or not isinstance(uncommitted, dict)
            or not isinstance(value.get("native_request"), dict)
            or not isinstance(value.get("error"), str)
        ):
            return None
        return {
            "kind": "r012_actual_upstream_request",
            "forwarded": False,
            "outcome": outcome,
            "error_type": error_type,
            "reason": "event_skill_budget_rejected_before_provider_boundary",
        }
    return None


def non_forwarded_terminal_disposition(value: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the small set of public preflight records with no boundary.

    A missing ``normal_call_boundary`` is meaningful only when the public
    overlay recorded a rejection before it could open the upstream provider.
    A caller cannot turn an arbitrary JSON object, an uncertain transport
    failure, or a partially forwarded request into a task result by omitting
    the boundary.  The returned value is derived metadata; the source record
    remains byte-for-byte untouched.
    """

    # These fields are written only after the upstream stream has opened or
    # completed.  Their presence makes a no-boundary record ambiguous, even
    # when their value happens to be null.
    if not isinstance(value, dict):
        return None
    if any(
        key in value
        for key in (
            "normal_call_boundary",
            "forwarded_request",
            "raw_upstream_response",
            "upstream_usage",
            "upstream",
        )
    ):
        return None
    return _parse_non_forwarded_terminal_disposition(value)


def non_forwarded_terminal_schema(value: dict[str, Any]) -> dict[str, Any] | None:
    """Return a validated terminal schema even when it is mixed with a boundary.

    Callers use this only to reject contradictory records.  A value returned
    here must never be treated as an admissible no-boundary terminal record
    unless :func:`non_forwarded_terminal_disposition` also accepts it.
    """

    return _parse_non_forwarded_terminal_disposition(value)


def _copy_file(source: Path, destination: Path) -> dict[str, str]:
    """Copy an immutable raw artifact atomically and retain both hashes."""
    if not source.is_file():
        raise HarborTrialEvidenceError(f"required artifact is not a file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle, source.open("rb") as input_handle:
            shutil.copyfileobj(input_handle, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    source_hash = sha256_file(source)
    destination_hash = sha256_file(destination)
    if source_hash != destination_hash:
        raise HarborTrialEvidenceError(f"copied artifact hash differs from source: {source}")
    return {"source_path": str(source), "source_sha256": source_hash, "path": str(destination), "sha256": destination_hash}


def _proxy_attempts(directory: Path, *, trial_id: str) -> list[tuple[Path, dict[str, Any]]]:
    path = directory / "upstream_requests"
    if not path.is_dir():
        raise HarborTrialEvidenceError("sidecar evidence has no upstream_requests directory")
    values: list[tuple[Path, dict[str, Any]]] = []
    for candidate in sorted(path.glob("attempt-*.json")):
        try:
            value = read_json(candidate)
        except (OSError, ValueError) as error:
            raise HarborTrialEvidenceError(f"cannot parse sidecar request record {candidate}: {error}") from error
        if not isinstance(value, dict):
            raise HarborTrialEvidenceError(f"sidecar request record must be an object: {candidate}")
        if value.get("trial_id") != trial_id:
            raise HarborTrialEvidenceError(
                f"sidecar request record belongs to a different trial: {candidate}"
            )
        values.append((candidate, value))
    if not values:
        raise HarborTrialEvidenceError("finished Harbor trial has no durable sidecar request records")
    return values


def _configured_session_id(config_path: Path) -> str:
    """Read the one session binding passed to Harbor's agent constructor."""
    try:
        config = read_json(config_path)
    except (OSError, ValueError) as error:
        raise HarborTrialEvidenceError(f"cannot parse Harbor config for session binding: {config_path}: {error}") from error
    if not isinstance(config, dict):
        raise HarborTrialEvidenceError("Harbor config for session binding must be an object")
    candidates: list[tuple[str, str]] = []

    def collect(label: str, value: Any) -> None:
        if not isinstance(value, dict):
            return
        kwargs = value.get("kwargs")
        if not isinstance(kwargs, dict):
            return
        session_id = kwargs.get("session_id")
        if isinstance(session_id, str) and session_id:
            candidates.append((label, session_id))

    collect("agent.kwargs", config.get("agent"))
    agents = config.get("agents")
    if isinstance(agents, list):
        for index, agent in enumerate(agents):
            collect(f"agents[{index}].kwargs", agent)
    if not candidates:
        raise HarborTrialEvidenceError("Harbor config has no nonempty agent.kwargs.session_id binding")
    session_ids = {session_id for _label, session_id in candidates}
    if len(session_ids) != 1:
        raise HarborTrialEvidenceError("Harbor config has multiple session_id bindings")
    return next(iter(session_ids))


def _jsonl_session_id(session_path: Path) -> str:
    """Extract the single native session control identity from JSONL."""
    try:
        lines = session_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise HarborTrialEvidenceError(f"cannot read OpenClaw session JSONL: {session_path}: {error}") from error
    session_ids: list[str] = []
    for lineno, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise HarborTrialEvidenceError(f"invalid OpenClaw session JSONL at line {lineno}: {error}") from error
        if not isinstance(event, dict) or event.get("type") != "session":
            continue
        session_id = event.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise HarborTrialEvidenceError(f"OpenClaw session control at line {lineno} has no nonempty id")
        session_ids.append(session_id)
    unique = set(session_ids)
    if len(unique) != 1:
        raise HarborTrialEvidenceError("OpenClaw session JSONL must contain exactly one nonempty session control id")
    return next(iter(unique))


def _proxy_session_binding(
    attempts: list[tuple[Path, dict[str, Any]]], *, trial_id: str
) -> dict[str, Any]:
    """Bind every public normal-call record to one same-trial session.

    Local cap rejections can happen before the public provider wrapper runs and
    therefore legitimately have no normal-call boundary.  They remain listed
    as unbound attempts; at least one actual wrapper boundary is required to
    establish the trial's session identity, and every boundary that exists must
    agree with it.
    """
    session_ids: set[str] = set()
    boundary_attempts: list[str] = []
    missing_attempts: list[str] = []
    for path, value in attempts:
        boundary = value.get("normal_call_boundary")
        if boundary is None:
            disposition = non_forwarded_terminal_disposition(value)
            if disposition is None:
                raise HarborTrialEvidenceError(
                    f"unbound sidecar attempt has no validated non-forwarded terminal evidence: {path}"
                )
            missing_attempts.append(path.name)
            continue
        if not isinstance(boundary, dict):
            raise HarborTrialEvidenceError(f"normal_call_boundary must be an object: {path}")
        if non_forwarded_terminal_schema(value) is not None:
            raise HarborTrialEvidenceError(
                f"sidecar attempt cannot contain both a normal_call_boundary and terminal rejection evidence: {path}"
            )
        if boundary.get("trial_id") != trial_id:
            raise HarborTrialEvidenceError(f"normal_call_boundary belongs to a different trial: {path}")
        session_id = boundary.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise HarborTrialEvidenceError(f"normal_call_boundary has no nonempty session_id: {path}")
        session_ids.add(session_id)
        boundary_attempts.append(path.name)
    if not session_ids:
        raise HarborTrialEvidenceError("sidecar evidence has no public normal_call_boundary session binding")
    if len(session_ids) != 1:
        raise HarborTrialEvidenceError("sidecar evidence has multiple normal_call_boundary session IDs")
    return {
        "session_id": next(iter(session_ids)),
        "boundary_attempts": boundary_attempts,
        "unbound_attempts": missing_attempts,
        "boundary_kind": "public_normal_call_boundary",
    }


def _materialize_sqlite_session(database: Path, destination: Path) -> dict[str, Any]:
    """Materialize an exact session JSONL view from an immutable OpenClaw DB.

    Older R015 runs used a public adapter revision whose stdout parser missed
    the bound session after a trailing diagnostic line.  The SQLite transcript
    is still the raw runtime record in those runs.  Require one session and
    valid JSON objects for every row before deriving the importer view.
    """

    if not database.is_file():
        raise HarborTrialEvidenceError(f"session source is not a file: {database}")
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise HarborTrialEvidenceError(f"cannot open SQLite session source: {error}") from error
    try:
        try:
            rows = connection.execute(
                "SELECT session_id, seq, event_json FROM transcript_events ORDER BY session_id, seq"
            ).fetchall()
        except sqlite3.Error as error:
            raise HarborTrialEvidenceError(f"SQLite session source has no usable transcript_events table: {error}") from error
    finally:
        connection.close()
    if not rows:
        raise HarborTrialEvidenceError("SQLite session source has no transcript events")
    session_ids = {row[0] for row in rows}
    if len(session_ids) != 1 or not next(iter(session_ids)):
        raise HarborTrialEvidenceError("SQLite session source must contain exactly one nonempty session")
    events: list[str] = []
    previous_seq: int | None = None
    for session_id, seq, event_json in rows:
        if not isinstance(session_id, str) or not isinstance(seq, int) or not isinstance(event_json, str):
            raise HarborTrialEvidenceError("SQLite session source has an invalid transcript row")
        if previous_seq is not None and seq <= previous_seq:
            raise HarborTrialEvidenceError("SQLite session source has non-increasing transcript sequence")
        previous_seq = seq
        try:
            event = json.loads(event_json)
        except json.JSONDecodeError as error:
            raise HarborTrialEvidenceError(f"SQLite session source has invalid event JSON: {error}") from error
        if not isinstance(event, dict):
            raise HarborTrialEvidenceError("SQLite session source event must be an object")
        events.append(event_json)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(events) + "\n", encoding="utf-8")
    return {
        "kind": "sqlite_transcript_events_derived_session_jsonl",
        "source_path": str(database),
        "source_sha256": sha256_file(database),
        "derived_path": str(destination),
        "derived_sha256": sha256_file(destination),
        "session_id": next(iter(session_ids)),
        "event_count": len(events),
    }


def import_harbor_openclaw_trial(
    *,
    trial_id: str,
    instance_id: str,
    harbor_trial_dir: Path,
    sidecar_evidence_dir: Path,
    output_dir: Path,
    session_source_path: Path | None = None,
) -> dict[str, Any]:
    """Create one immutable R015 finish packet from the authorized inputs.

    The packet can be passed directly to ``run_m3_r012_lifecycle.py finish``:
    ``trial-result.json``, all copied proxy attempts, and
    ``trajectory-evidence.json``.  The raw official artifacts are copied too
    so the normalized trace is reviewable against the original session.
    """
    trial_id = str(trial_id)
    if not trial_id:
        raise HarborTrialEvidenceError("trial_id must be nonempty")
    expected_instance = canonical_instance_id(instance_id)
    if output_dir.exists():
        raise HarborTrialEvidenceError(f"finish packet output already exists: {output_dir}")
    required = {
        "result": harbor_trial_dir / "result.json",
        "reward": harbor_trial_dir / "verifier" / "reward.txt",
        "instruction": harbor_trial_dir / "agent" / "instruction.txt",
        "config": harbor_trial_dir / "config.json",
    }
    for label, path in required.items():
        if not path.is_file():
            raise HarborTrialEvidenceError(f"Harbor trial has no required {label} artifact: {path}")
    configured_session_id = _configured_session_id(required["config"])
    attempts = _proxy_attempts(sidecar_evidence_dir, trial_id=trial_id)
    proxy_binding = _proxy_session_binding(attempts, trial_id=trial_id)
    session_path = harbor_trial_dir / "agent" / "openclaw.session.jsonl"
    sqlite_path = (
        Path(session_source_path)
        if session_source_path is not None
        else harbor_trial_dir / "agent" / "codeskill-openclaw-state" / "openclaw-agent.sqlite"
    )
    if session_source_path is not None and not sqlite_path.is_file():
        raise HarborTrialEvidenceError(f"explicit session source is not a file: {sqlite_path}")
    session_staging: tempfile.TemporaryDirectory[str] | None = None
    session_derivation: dict[str, Any] | None = None
    if session_source_path is not None:
        session_staging = tempfile.TemporaryDirectory(prefix="r015-session-")
        normalized_session_path = Path(session_staging.name) / "openclaw.session.jsonl"
        session_derivation = _materialize_sqlite_session(sqlite_path, normalized_session_path)
    elif session_path.is_file():
        normalized_session_path = session_path
    elif sqlite_path.is_file():
        session_staging = tempfile.TemporaryDirectory(prefix="r015-session-")
        normalized_session_path = Path(session_staging.name) / "openclaw.session.jsonl"
        session_derivation = _materialize_sqlite_session(sqlite_path, normalized_session_path)
    else:
        raise HarborTrialEvidenceError(
            f"Harbor trial has no required session artifact: {session_path} (SQLite fallback also absent: {sqlite_path})"
        )
    source_session_id = (
        session_derivation["session_id"]
        if session_derivation is not None
        else _jsonl_session_id(normalized_session_path)
    )
    binding_ids = {
        "config_agent_kwargs": configured_session_id,
        "session_source": source_session_id,
        "proxy_normal_call_boundary": proxy_binding["session_id"],
    }
    if len(set(binding_ids.values())) != 1:
        if session_staging is not None:
            session_staging.cleanup()
        raise HarborTrialEvidenceError(
            "session binding mismatch across Harbor config, raw session, and public normal_call_boundary: "
            + json.dumps(binding_ids, ensure_ascii=False, sort_keys=True)
        )
    session_binding = {
        "session_id": source_session_id,
        "config_agent_kwargs_session_id": configured_session_id,
        "proxy": proxy_binding,
        "source_kind": session_derivation["kind"] if session_derivation is not None else "jsonl",
        "binding_rule": "config.agent.kwargs.session_id == raw_session.session_id == proxy.normal_call_boundary.session_id",
    }
    try:
        trace = normalize_openclaw_trial(
            harbor_trial_dir,
            session_path_override=normalized_session_path,
        )
    except TraceImportError as error:
        if session_staging is not None:
            session_staging.cleanup()
        raise HarborTrialEvidenceError(f"invalid official OpenClaw trial: {error}") from error
    source = trace.get("source")
    if not isinstance(source, dict) or canonical_instance_id(str(source.get("canonical_instance_id", ""))) != expected_instance:
        if session_staging is not None:
            session_staging.cleanup()
        raise HarborTrialEvidenceError("official OpenClaw trial task differs from the frozen instance")
    if session_derivation is not None:
        source["session_path"] = str(sqlite_path)
        source["session_sha256"] = session_derivation["source_sha256"]
        source["session_source_kind"] = session_derivation["kind"]
        source["derived_session_jsonl_sha256"] = session_derivation["derived_sha256"]
        source["derived_session_event_count"] = session_derivation["event_count"]
    source["session_id"] = source_session_id
    source["session_binding"] = deepcopy(session_binding)
    outcome = trace.get("outcome")
    if not isinstance(outcome, dict) or outcome.get("official_reward") in {None, ""}:
        if session_staging is not None:
            session_staging.cleanup()
        raise HarborTrialEvidenceError("official verifier reward is missing or empty")
    output_dir.mkdir(parents=True)
    raw = {
        label: _copy_file(path, output_dir / "raw-harbor" / path.relative_to(harbor_trial_dir))
        for label, path in required.items()
    }
    raw["session"] = _copy_file(
        normalized_session_path,
        output_dir / "raw-harbor" / "agent" / "openclaw.session.jsonl",
    )
    if session_derivation is not None:
        session_derivation["derived_path"] = str(output_dir / "raw-harbor" / "agent" / "openclaw.session.jsonl")
        raw["session"]["source_kind"] = session_derivation["kind"]
        raw["session"]["derived_from"] = {
            "path": str(sqlite_path),
            "sha256": session_derivation["source_sha256"],
        }
        raw["session_source"] = _copy_file(
            sqlite_path,
            output_dir / "raw-harbor" / "agent" / "codeskill-openclaw-state" / "openclaw-agent.sqlite",
        )
    ctrf = harbor_trial_dir / "verifier" / "ctrf.json"
    if ctrf.is_file():
        raw["ctrf"] = _copy_file(ctrf, output_dir / "raw-harbor" / "verifier" / "ctrf.json")
    write_json(output_dir / "trajectory-evidence.json", trace)
    copied_attempts: list[dict[str, Any]] = []
    for ordinal, (path, value) in enumerate(attempts, start=1):
        destination = output_dir / "proxy-attempts" / f"attempt-{ordinal:04d}.json"
        write_json(destination, value)
        copied_attempts.append(
            {
                "source_path": str(path),
                "source_sha256": sha256_file(path),
                "path": str(destination),
                "sha256": sha256_file(destination),
                "value": deepcopy(value),
            }
        )
    result_value = {
        "kind": "r015_official_harbor_trial_result",
        "trial_id": trial_id,
        "instance_id": expected_instance,
        "official_reward": outcome["official_reward"],
        "outcome": deepcopy(outcome),
        "session_binding": deepcopy(session_binding),
        "raw_harbor_artifacts": raw,
    }
    write_json(output_dir / "trial-result.json", result_value)
    manifest = {
        "schema_version": 1,
        "kind": "r015_harbor_openclaw_finish_packet",
        "created_at_utc": utc_now(),
        "trial_id": trial_id,
        "instance_id": expected_instance,
        "harbor_trial_dir": str(harbor_trial_dir),
        "sidecar_evidence_dir": str(sidecar_evidence_dir),
        "official_reward": outcome["official_reward"],
        "session_binding": deepcopy(session_binding),
        "raw_harbor_artifacts": raw,
        "trajectory_evidence": {
            "path": str(output_dir / "trajectory-evidence.json"),
            "sha256": sha256_file(output_dir / "trajectory-evidence.json"),
        },
        "proxy_attempts": [
            {key: value for key, value in attempt.items() if key != "value"}
            for attempt in copied_attempts
        ],
        "trial_result": {
            "path": str(output_dir / "trial-result.json"),
            "sha256": sha256_file(output_dir / "trial-result.json"),
        },
    }
    if session_derivation is not None:
        manifest["session_derivation"] = session_derivation
    write_json(output_dir / "manifest.json", manifest)
    if session_staging is not None:
        session_staging.cleanup()
    return manifest
