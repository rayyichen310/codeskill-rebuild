"""Fail-closed reconciliation for a durable paid manager response.

The normal R012 journal path refuses to replay a phase when a pre-call journal
already exists.  This module provides the separate, explicit audit boundary
needed when a response reached the manager but was classified incorrectly by a
preflight accounting bug.  It never sends a chat completion request and never
changes the original request, response, journal, or ledger.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from .pipeline import validate_event_extraction_with_evidence
from .types import canonical_json, read_json, sha256_file, sha256_text, utc_now, write_json


class ManagerReconciliationError(RuntimeError):
    """A saved manager call is not safe to reconcile."""


def _object(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManagerReconciliationError(f"{field} must be an object")
    return value


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManagerReconciliationError(f"{field} must be a nonempty string")
    return value.strip()


def _ref(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise ManagerReconciliationError(f"required reconciliation file is missing: {path}")
    return {"path": str(path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def _immutable_snapshot(source: Path, target: Path) -> dict[str, Any]:
    """Copy one source file into an immutable, separately named artifact.

    The manager ledger is a live append-only file.  Reconciliation evidence
    must retain the exact bytes seen during the audit while also recording the
    live path for a later continuation.  A pre-existing target is accepted
    only when its bytes are identical, so a stale or colliding snapshot cannot
    be silently reused.
    """
    source = Path(source)
    target = Path(target)
    if not source.is_file():
        raise ManagerReconciliationError(f"cannot snapshot missing file: {source}")
    source_bytes = source.read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not target.is_file() or target.read_bytes() != source_bytes:
            raise ManagerReconciliationError(f"immutable snapshot already differs: {target}")
    else:
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_bytes(source_bytes)
        temporary.replace(target)
    return _ref(target)


def _hash_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def _load_object(path: Path, *, field: str) -> dict[str, Any]:
    try:
        value = read_json(path)
    except (OSError, ValueError) as error:
        raise ManagerReconciliationError(f"cannot read {field}: {path}: {error}") from error
    return _object(value, field=field)


def effective_request_options(request_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return every non-message field sent to the chat endpoint."""
    if not isinstance(request_payload.get("messages"), list):
        raise ManagerReconciliationError("saved manager request.messages must be a list")
    return {str(key): deepcopy(value) for key, value in request_payload.items() if key != "messages"}


def _read_path_ref(value: Any, *, field: str) -> Path:
    item = _object(value, field=field)
    path = Path(_text(item.get("path"), field=f"{field}.path"))
    stated_hash = _text(item.get("sha256"), field=f"{field}.sha256")
    if not path.is_file() or sha256_file(path) != stated_hash:
        raise ManagerReconciliationError(f"{field} path/hash changed: {path}")
    return path


def _ensure_identity(
    *,
    request_record: dict[str, Any],
    journal: dict[str, Any],
    ledger: dict[str, Any],
    expected: Mapping[str, Any],
) -> tuple[str, str, list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    request_payload = _object(request_record.get("request"), field="saved manager request.request")
    messages = request_payload.get("messages")
    if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
        raise ManagerReconciliationError("saved manager request has no valid messages list")
    call_id = _text(expected.get("call_id"), field="expected.call_id")
    purpose = _text(expected.get("purpose"), field="expected.purpose")
    if request_record.get("purpose") != purpose or journal.get("purpose") != purpose:
        raise ManagerReconciliationError("saved request/journal purpose differs from the expected phase")
    if journal.get("trial_id") != expected.get("trial_id") or journal.get("phase") != expected.get("phase"):
        raise ManagerReconciliationError("saved manager journal is bound to another trial or phase")
    messages_hash = _hash_json(messages)
    if journal.get("messages_sha256") != messages_hash:
        raise ManagerReconciliationError("saved manager journal message hash differs from the request")
    if request_record.get("call_metadata", {}).get("task_id") != expected.get("task_id"):
        raise ManagerReconciliationError("saved manager request metadata is bound to another task")
    calls = ledger.get("calls")
    if not isinstance(calls, list):
        raise ManagerReconciliationError("saved manager ledger has no calls list")
    matching = [item for item in calls if isinstance(item, dict) and item.get("call_id") == call_id]
    if len(matching) != 1:
        raise ManagerReconciliationError(f"saved manager ledger does not contain exactly one {call_id}")
    ledger_entry = matching[0]
    if ledger_entry.get("purpose") != purpose or ledger_entry.get("status") != "tokenizer_mismatch":
        raise ManagerReconciliationError("saved ledger call is not the preserved tokenizer mismatch")
    if journal.get("status") != "manager_call_raised":
        raise ManagerReconciliationError("saved manager journal is not the original raised-call record")
    response_ref = _object(expected.get("response"), field="expected.response")
    response_path = _read_path_ref(response_ref, field="expected.response")
    ledger_response_path = ledger_entry.get("response_path")
    if ledger_response_path != str(response_path):
        raise ManagerReconciliationError("saved ledger response path differs from the reconciled response")
    return call_id, purpose, messages, request_payload, deepcopy(ledger_entry)


def audit_saved_manager_call(
    *,
    request_path: Path,
    response_path: Path,
    journal_path: Path,
    ledger_path: Path,
    trace_path: Path,
    expected: Mapping[str, Any],
    tokenizer: Any,
    output_path: Path,
    tokenizer_exchange_path: Path,
    driver_stage_ref: Mapping[str, Any] | None = None,
    reconciliation_run_dir: Path | None = None,
    ledger_snapshot_path: Path | None = None,
) -> dict[str, Any]:
    """Audit one saved call and write a no-chat reconciliation manifest.

    ``tokenizer`` is intentionally supplied by the caller.  The expected
    production caller uses :class:`ServerMessageTokenCounter`, which performs
    one model-free ``/tokenize`` request using the complete saved chat shape.
    This function does not provide a fallback counter or a messages-only
    estimate.
    """
    request_path = Path(request_path)
    response_path = Path(response_path)
    journal_path = Path(journal_path)
    ledger_path = Path(ledger_path)
    trace_path = Path(trace_path)
    output_path = Path(output_path)
    tokenizer_exchange_path = Path(tokenizer_exchange_path)
    if output_path.exists():
        raise ManagerReconciliationError(f"reconciliation output already exists: {output_path}")
    if tokenizer_exchange_path.exists():
        raise ManagerReconciliationError(f"tokenizer exchange output already exists: {tokenizer_exchange_path}")
    request_record = _load_object(request_path, field="saved manager request")
    response_record = _load_object(response_path, field="saved manager response")
    journal = _load_object(journal_path, field="saved manager journal")
    ledger = _load_object(ledger_path, field="saved manager ledger")
    trace = _load_object(trace_path, field="saved live trajectory")
    source = _object(trace.get("source"), field="saved live trajectory.source")
    expected_session_id = _text(expected.get("session_id"), field="expected.session_id")
    if source.get("session_id") != expected_session_id:
        raise ManagerReconciliationError("saved live trajectory session ID differs from the expected manager session")
    session_path_value = source.get("session_path")
    session_sha_value = source.get("session_sha256")
    session_ref: dict[str, Any] | None = None
    if session_path_value is not None or session_sha_value is not None:
        session_path = Path(_text(session_path_value, field="saved live trajectory.source.session_path"))
        session_sha = _text(session_sha_value, field="saved live trajectory.source.session_sha256")
        if not session_path.is_file() or sha256_file(session_path) != session_sha:
            raise ManagerReconciliationError("saved manager session path/hash changed")
        session_ref = {"session_id": expected_session_id, **_ref(session_path)}
    call_id, purpose, messages, request_payload, ledger_entry = _ensure_identity(
        request_record=request_record,
        journal=journal,
        ledger=ledger,
        expected={**dict(expected), "response": _ref(response_path)},
    )
    if response_record.get("http_status") != 200:
        raise ManagerReconciliationError("saved manager response did not reach HTTP 200")
    if response_record.get("classification") != "tokenizer_prompt_count_mismatch":
        raise ManagerReconciliationError("saved manager response is not the preserved tokenizer mismatch")
    parsed = _object(response_record.get("parsed_response"), field="saved manager response.parsed_response")
    choices = parsed.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ManagerReconciliationError("saved manager response must contain exactly one choice")
    choice = _object(choices[0], field="saved manager response.choice")
    message = _object(choice.get("message"), field="saved manager response.message")
    content = message.get("content")
    if not isinstance(content, str):
        raise ManagerReconciliationError("saved manager response has no textual JSON content")
    try:
        model_json = json.loads(content)
    except json.JSONDecodeError as error:
        raise ManagerReconciliationError(f"saved manager response content is not JSON: {error}") from error
    model_json = _object(model_json, field="saved manager response JSON")
    usage = _object(response_record.get("usage"), field="saved manager response.usage")
    observed_prompt_tokens = usage.get("prompt_tokens")
    if isinstance(observed_prompt_tokens, bool) or not isinstance(observed_prompt_tokens, int):
        raise ManagerReconciliationError("saved manager response has no integer prompt_tokens usage")
    options = effective_request_options(request_payload)
    try:
        counted = int(tokenizer(messages, request_options=options))
    except TypeError as error:
        raise ManagerReconciliationError("reconciliation tokenizer cannot receive the complete request options") from error
    if counted != observed_prompt_tokens:
        raise ManagerReconciliationError(
            f"corrected tokenizer count {counted} differs from saved chat usage {observed_prompt_tokens}"
        )
    tokenizer_exchange = getattr(tokenizer, "last_exchange", None)
    if not isinstance(tokenizer_exchange, dict):
        raise ManagerReconciliationError("reconciliation tokenizer did not preserve its raw exchange")
    write_json(tokenizer_exchange_path, tokenizer_exchange)
    tokenizer_ref = _ref(tokenizer_exchange_path)
    try:
        validated = validate_event_extraction_with_evidence(model_json, trace, benchmark="terminal-bench")
    except (ValueError, TypeError) as error:
        raise ManagerReconciliationError(f"saved manager event output fails its validator: {error}") from error
    response_ref = _ref(response_path)
    request_ref = _ref(request_path)
    journal_ref = _ref(journal_path)
    if ledger_snapshot_path is None:
        ledger_snapshot_path = output_path.with_name(output_path.stem + "-ledger-snapshot.json")
    ledger_snapshot_path = Path(ledger_snapshot_path)
    ledger_snapshot_ref = _immutable_snapshot(ledger_path, ledger_snapshot_path)
    live_ledger_ref = _ref(ledger_path)
    trace_ref = _ref(trace_path)
    trial_id = _text(expected.get("trial_id"), field="expected.trial_id")
    task_id = _text(expected.get("task_id"), field="expected.task_id")
    phase = _text(expected.get("phase"), field="expected.phase")
    run_dir = Path(reconciliation_run_dir) if reconciliation_run_dir is not None else output_path.parents[2]
    exchange_summary = {
        "endpoint": tokenizer_exchange.get("endpoint"),
        "http_status": tokenizer_exchange.get("http_status"),
        "count": tokenizer_exchange.get("count"),
        "request_options": deepcopy(options),
        "raw_response_sha256": hashlib.sha256(str(tokenizer_exchange.get("raw_response", "")).encode("utf-8")).hexdigest(),
        "exchange_ref": tokenizer_ref,
    }
    manifest = {
        "schema_version": 1,
        "kind": "r015_c_only_manager_call_reconciliation",
        "status": "ready_for_explicit_manager_phase_resume",
        "created_at_utc": utc_now(),
        "mode": "model_free_audit_only",
        "trial": {
            "round_id": expected.get("round_id"),
            "task_id": task_id,
            "trial_id": trial_id,
            "session_id": expected.get("session_id"),
        },
        "phase": phase,
        "purpose": purpose,
        "original": {
            "call_id": call_id,
            "request": request_ref,
            "response": response_ref,
            "journal": journal_ref,
            # ``ledger`` remains the stable public field, but now points to
            # the immutable snapshot.  The mutable live ledger is recorded
            # separately below and is checked by preserved call identity.
            "ledger": ledger_snapshot_ref,
            "ledger_snapshot": ledger_snapshot_ref,
            "live_ledger": {
                **live_ledger_ref,
                "sha256_at_audit": live_ledger_ref["sha256"],
                "mutable": True,
            },
            "preserved_ledger_entry": deepcopy(ledger_entry),
            "preserved_ledger_entry_sha256": _hash_json(ledger_entry),
            "trajectory": trace_ref,
            "session": session_ref,
            "response_classification": response_record.get("classification"),
            "ledger_status": next(item.get("status") for item in ledger.get("calls", []) if item.get("call_id") == call_id),
            "journal_status": journal.get("status"),
        },
        "driver_stage": deepcopy(dict(driver_stage_ref)) if driver_stage_ref is not None else None,
        "request": {
            "messages_sha256": _hash_json(messages),
            "message_count": len(messages),
            "payload_keys": sorted(request_payload),
            "effective_request_options": deepcopy(options),
        },
        "response": {
            "http_status": response_record.get("http_status"),
            "finish_reason": choice.get("finish_reason", response_record.get("finish_reason")),
            "usage": deepcopy(usage),
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "json_keys": sorted(model_json),
            "action": model_json.get("action"),
            "validated_json": deepcopy(validated),
        },
        "corrected_preflight": {
            "method": getattr(tokenizer, "method", "exact_server_or_tokenizer"),
            "estimated_input_tokens": counted,
            "chat_usage_prompt_tokens": observed_prompt_tokens,
            "matches": True,
            "effective_request_options": deepcopy(options),
            "tokenizer_exchange": exchange_summary,
        },
        "reusable_call": {
            "call_id": call_id,
            "phase": phase,
            "purpose": purpose,
            "request": request_ref,
            "response": response_ref,
            "messages_sha256": _hash_json(messages),
            "validated_json": deepcopy(validated),
            "no_chat_completion_repeated": True,
        },
        "resume": {
            "harbor_rerun": False,
            "reuse_existing_call_id": call_id,
            "next_manager_phase": "event-002",
            "new_journal_namespace": "manager-journals-reconciliation",
            "reconciliation_journal_path": str(
                run_dir
                / "manager-journals-reconciliation"
                / task_id
                / sha256_text(trial_id)[:20]
                / f"{phase}.json"
            ),
            "original_files_immutable": True,
            "automatic_retry": False,
            "requires_parent_review_before_paid_phases": True,
        },
    }
    write_json(output_path, manifest)
    return manifest


def load_reconciliation_manifest(path: Path) -> dict[str, Any]:
    """Load a previously audited manifest and verify all referenced bytes."""
    manifest_path = Path(path)
    manifest = _load_object(manifest_path, field="manager reconciliation manifest")
    if manifest.get("kind") != "r015_c_only_manager_call_reconciliation" or manifest.get("status") != "ready_for_explicit_manager_phase_resume":
        raise ManagerReconciliationError("manager reconciliation manifest is not resume-ready")
    original = _object(manifest.get("original"), field="reconciliation.original")
    reusable = _object(manifest.get("reusable_call"), field="reconciliation.reusable_call")
    if reusable.get("no_chat_completion_repeated") is not True:
        raise ManagerReconciliationError("reconciliation manifest does not prohibit repeating the saved chat call")
    for field in ("request", "response", "journal", "ledger", "trajectory"):
        _read_path_ref(original.get(field), field=f"reconciliation.original.{field}")
    ledger_snapshot = _object(original.get("ledger_snapshot"), field="reconciliation.original.ledger_snapshot")
    if canonical_json(ledger_snapshot) != canonical_json(original.get("ledger")):
        raise ManagerReconciliationError("reconciliation ledger and ledger_snapshot references differ")
    live_ledger = _object(original.get("live_ledger"), field="reconciliation.original.live_ledger")
    live_ledger_path = Path(_text(live_ledger.get("path"), field="reconciliation.original.live_ledger.path"))
    if not live_ledger_path.is_file():
        raise ManagerReconciliationError(f"reconciliation live ledger is missing: {live_ledger_path}")
    if live_ledger_path.resolve() == Path(_text(ledger_snapshot.get("path"), field="reconciliation.original.ledger_snapshot.path")).resolve():
        raise ManagerReconciliationError("reconciliation live ledger must be separate from its immutable snapshot")
    if live_ledger.get("mutable") is not True:
        raise ManagerReconciliationError("reconciliation live ledger must be explicitly marked mutable")
    audit_hash = _text(live_ledger.get("sha256_at_audit"), field="reconciliation.original.live_ledger.sha256_at_audit")
    if audit_hash != live_ledger.get("sha256"):
        raise ManagerReconciliationError("reconciliation live ledger audit hash is inconsistent")
    preserved_entry = _object(original.get("preserved_ledger_entry"), field="reconciliation.original.preserved_ledger_entry")
    preserved_hash = _text(original.get("preserved_ledger_entry_sha256"), field="reconciliation.original.preserved_ledger_entry_sha256")
    if _hash_json(preserved_entry) != preserved_hash:
        raise ManagerReconciliationError("reconciliation preserved ledger entry hash changed")
    snapshot_value = _load_object(Path(_text(ledger_snapshot.get("path"), field="reconciliation.original.ledger_snapshot.path")), field="reconciliation immutable ledger snapshot")
    snapshot_calls = snapshot_value.get("calls")
    if not isinstance(snapshot_calls, list) or sum(1 for item in snapshot_calls if isinstance(item, dict) and item.get("call_id") == preserved_entry.get("call_id")) != 1:
        raise ManagerReconciliationError("reconciliation immutable ledger snapshot does not contain exactly one preserved call")
    snapshot_entry = next(item for item in snapshot_calls if isinstance(item, dict) and item.get("call_id") == preserved_entry.get("call_id"))
    if canonical_json(snapshot_entry) != canonical_json(preserved_entry):
        raise ManagerReconciliationError("reconciliation immutable ledger snapshot call changed")
    session = original.get("session")
    if session is not None:
        _read_path_ref(session, field="reconciliation.original.session")
    for field in ("request", "response"):
        _read_path_ref(reusable.get(field), field=f"reconciliation.reusable_call.{field}")
        if canonical_json(original.get(field)) != canonical_json(reusable.get(field)):
            raise ManagerReconciliationError(f"reconciliation original and reusable {field} references differ")
    corrected = _object(manifest.get("corrected_preflight"), field="reconciliation.corrected_preflight")
    if corrected.get("matches") is not True or corrected.get("estimated_input_tokens") != corrected.get("chat_usage_prompt_tokens"):
        raise ManagerReconciliationError("reconciliation corrected preflight does not match saved usage")
    exchange = _object(corrected.get("tokenizer_exchange"), field="reconciliation.tokenizer_exchange")
    _read_path_ref(exchange.get("exchange_ref"), field="reconciliation.tokenizer_exchange.exchange_ref")
    driver_stage = manifest.get("driver_stage")
    if driver_stage is not None:
        _read_path_ref(driver_stage, field="reconciliation.driver_stage")
    return manifest
