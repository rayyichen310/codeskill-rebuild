"""Create a derived, evidence-only continuation of a blocked R012 M2 run.

This entrypoint never writes into the stopped run.  It reconciles exactly one
post-call validation failure, records the paid failed initial slot, and may
make one R011-style evidence-only repair which preserves the generated skill
content exactly.  Only then can it continue the remaining unspent initial
slots.  A new validation failure again stops durably; it is never retried by
this process.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from codeskill_rebuild.context import ContextBlocked
from codeskill_rebuild.event_extraction import EventExtractionSchedule
from codeskill_rebuild.manager import ManagerCallError, ManagerClient, ManagerProfile, ServerMessageTokenCounter, update_development_ledger_limit
from codeskill_rebuild.pipeline import event_evidence_repair_messages, validate_event_evidence_repair
from codeskill_rebuild.types import canonical_instance_id, contract_from_files, read_json, sha256_file, utc_now, write_contract_snapshot, write_json
from run_m2_r012_event_extraction import _full_or_compacted_call, _sources, file_ref


class ResumeError(RuntimeError):
    pass


def _object(path: Path, *, what: str) -> dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise ResumeError(f"{what} must be a JSON object: {path}")
    return value


def _same_file_hash(actual: Any, expected: dict[str, str], *, field: str) -> None:
    """Compare immutable content identities while allowing an equivalent copy."""
    if not isinstance(actual, dict) or not isinstance(actual.get("sha256"), str):
        raise ResumeError(f"prior manifest lacks a valid {field} file reference")
    if actual["sha256"] != expected["sha256"]:
        raise ResumeError(f"{field} differs from the stopped run")


def _require_exact_prior_inputs(
    *,
    prior_manifest: dict[str, Any],
    prior_schedules: dict[str, dict[str, Any]],
    sources: list[dict[str, Any]],
    source_manifest: Path,
    runtime_prompt: Path,
    evidence_compaction_prompt: Path | None,
) -> None:
    """Reject changed prompt/trace inputs before creating a client or run dir."""
    _same_file_hash(prior_manifest.get("source_manifest"), file_ref(source_manifest), field="source_manifest")
    _same_file_hash(prior_manifest.get("runtime_prompt"), file_ref(runtime_prompt), field="runtime_prompt")
    expected_compaction = file_ref(evidence_compaction_prompt) if evidence_compaction_prompt else None
    prior_compaction = prior_manifest.get("evidence_compaction_prompt")
    if prior_compaction is None and expected_compaction is None:
        pass
    elif prior_compaction is None or expected_compaction is None:
        raise ResumeError("evidence_compaction_prompt differs from the stopped run")
    else:
        _same_file_hash(prior_compaction, expected_compaction, field="evidence_compaction_prompt")
    current_order = [item["source_instance_id"] for item in sources]
    if prior_manifest.get("source_order") != current_order:
        raise ResumeError("source_order differs from the stopped run")
    for source in sources:
        source_id = source["source_instance_id"]
        schedule = prior_schedules.get(source_id)
        if not isinstance(schedule, dict):
            raise ResumeError(f"prior schedule is absent for {source_id}")
        refs = schedule.get("source_run_references")
        if not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], dict):
            raise ResumeError(f"prior schedule has invalid source trace reference for {source_id}")
        _same_file_hash(refs[0].get("normalized_trace"), file_ref(source["trace_path"]), field=f"normalized trace for {source_id}")


def _call_for_failed_initial(prior_run: Path, *, source_id: str, ordinal: int) -> tuple[str, Path, Path, dict[str, Any]]:
    matches: list[tuple[str, Path, Path, dict[str, Any]]] = []
    purpose = f"r012_event_initial:{source_id}:{ordinal}"
    for request_path in sorted((prior_run / "model_calls").glob("call-*/request.json")):
        request = _object(request_path, what="prior manager request")
        if request.get("purpose") != purpose:
            continue
        response_path = request_path.with_name("response.json")
        if not response_path.is_file():
            raise ResumeError(f"{request_path}: prior failed call has no durable response")
        call_id = request_path.parent.name
        matches.append((call_id, request_path, response_path, _object(response_path, what="prior manager response")))
    if len(matches) != 1:
        raise ResumeError(f"expected exactly one durable original call for {purpose}, found {len(matches)}")
    return matches[0]


def _original_output(response: dict[str, Any]) -> dict[str, Any]:
    parsed = response.get("parsed_response")
    try:
        content = parsed["choices"][0]["message"]["content"]
        value = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise ResumeError("failed initial call has no recoverable JSON object for evidence-only repair") from error
    if not isinstance(value, dict):
        raise ResumeError("failed initial call output must be a JSON object")
    return value


def _write_schedule(directory: Path, schedule: EventExtractionSchedule) -> None:
    write_json(directory / "schedule.json", schedule.manifest())


def _latest_created_call_id(client: ManagerClient, before: int) -> str | None:
    return f"call-{client.call_count:04d}" if client.call_count > before else None


def _failure_classification(error: BaseException) -> str:
    if isinstance(error, ContextBlocked):
        return "context_blocked"
    if isinstance(error, ManagerCallError):
        return "manager_call_failed_or_invalid_output"
    return "post_call_validation_or_runner_failure"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prior-run-dir", type=Path, required=True)
    parser.add_argument("--derived-run-dir", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--runtime-prompt", type=Path, required=True)
    parser.add_argument("--evidence-compaction-prompt", type=Path)
    parser.add_argument("--repair-prompt", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--execute-manager", action="store_true")
    parser.add_argument("--activate-unlimited-development-ledger", action="store_true")
    args = parser.parse_args()
    if args.derived_run_dir.exists():
        raise FileExistsError(args.derived_run_dir)
    if not args.execute_manager or not args.activate_unlimited_development_ledger:
        raise ResumeError("derived continuation requires explicit --execute-manager and --activate-unlimited-development-ledger")

    prior_status = _object(args.prior_run_dir / "run-status.json", what="prior run status")
    failure = prior_status.get("failure")
    if prior_status.get("status") != "blocked_or_failed" or not isinstance(failure, dict):
        raise ResumeError("prior run is not a durably blocked R012 extraction run")
    if failure.get("classification") != "post_call_validation_or_runner_failure":
        raise ResumeError("only a post-call validation failure is eligible for the one evidence-only repair")
    source_id = canonical_instance_id(str(failure.get("source_instance_id", "")))
    ordinal = failure.get("initial_attempt_ordinal")
    if not source_id or not isinstance(ordinal, int) or ordinal <= 0:
        raise ResumeError("prior failure lacks source_instance_id or initial_attempt_ordinal")
    failure_path = args.prior_run_dir / "extraction" / "event" / source_id / f"initial-{ordinal:02d}-failure.json"
    prior_failure = _object(failure_path, what="prior initial failure")
    if prior_failure.get("error") != failure.get("error") or prior_failure.get("source_instance_id") != source_id:
        raise ResumeError("prior run status and failure artifact disagree")

    sources = _sources(args.source_manifest)
    by_source = {item["source_instance_id"]: item for item in sources}
    if source_id not in by_source:
        raise ResumeError("failed source is absent from the requested source manifest")
    schedules_value = prior_status.get("completed_schedules")
    if not isinstance(schedules_value, list):
        raise ResumeError("prior run has no durable source schedules")
    prior_schedules = {str(item.get("source_instance_id")): item for item in schedules_value if isinstance(item, dict)}
    if set(prior_schedules) != set(by_source):
        raise ResumeError("prior schedules and source manifest differ; refusing to change source set")

    current_contract = contract_from_files(args.spec, args.decisions)
    prior_contract_snapshot = _object(args.prior_run_dir / "contract" / "contract.json", what="prior contract snapshot")
    if prior_contract_snapshot.get("contract") != current_contract:
        raise ResumeError("contract changed since the stopped run; evidence-only continuation is forbidden")
    prior_manifest = _object(args.prior_run_dir / "manifest.json", what="prior run manifest")
    if prior_manifest.get("contract") != current_contract:
        raise ResumeError("prior manifest contract differs from the stopped run contract snapshot")
    _require_exact_prior_inputs(
        prior_manifest=prior_manifest,
        prior_schedules=prior_schedules,
        sources=sources,
        source_manifest=args.source_manifest,
        runtime_prompt=args.runtime_prompt,
        evidence_compaction_prompt=args.evidence_compaction_prompt,
    )
    call_id, request_path, response_path, response = _call_for_failed_initial(args.prior_run_dir, source_id=source_id, ordinal=ordinal)
    original_output = _original_output(response)

    args.derived_run_dir.mkdir(parents=True)
    snapshot = write_contract_snapshot(args.derived_run_dir, args.spec, args.decisions, current_contract)
    runtime_prompt = args.runtime_prompt.read_text(encoding="utf-8")
    compaction_prompt = args.evidence_compaction_prompt.read_text(encoding="utf-8") if args.evidence_compaction_prompt else None
    repair_prompt = args.repair_prompt.read_text(encoding="utf-8")
    prompt_refs = {
        "runtime_prompt": file_ref(args.runtime_prompt),
        "evidence_compaction_prompt": file_ref(args.evidence_compaction_prompt) if args.evidence_compaction_prompt else None,
        "repair_prompt": file_ref(args.repair_prompt),
    }
    schedules: dict[str, EventExtractionSchedule] = {
        item["source_instance_id"]: EventExtractionSchedule.from_manifest(item["trace"], prior_schedules[item["source_instance_id"]])
        for item in sources
    }
    failed_schedule = schedules[source_id]
    if failed_schedule.next_initial_attempt_ordinal != ordinal:
        raise ResumeError("prior schedule next ordinal does not match the recorded failed call")
    original_attempt = failed_schedule.record_initial_failure(
        model_call_id=call_id,
        error=ValueError(str(failure["error"])),
        evidence={
            "kind": "r015_reconciled_paid_post_call_validation_failure",
            "prior_run": file_ref(args.prior_run_dir / "manifest.json"),
            "prior_run_status": file_ref(args.prior_run_dir / "run-status.json"),
            "prior_failure": file_ref(failure_path),
            "original_manager_request": file_ref(request_path),
            "original_manager_response": file_ref(response_path),
            "original_model_output": original_output,
            "automatic_retry": False,
        },
    )
    source_dir = args.derived_run_dir / "extraction" / "event" / source_id
    write_json(source_dir / f"initial-{ordinal:02d}-reconciled-failure.json", original_attempt)
    _write_schedule(source_dir, failed_schedule)

    service = _object(args.config, what="manager config")["services"]["deepseek_flash"]
    profile = ManagerProfile(base_url=service["base_url"], model=service["model_id"], max_total_calls=None)
    ledger = update_development_ledger_limit(
        args.ledger,
        new_limit=None,
        reason="R015 explicit unlimited-development-ledger activation for one evidence-only R011 repair and bounded R012 continuation",
        contract=current_contract,
    )
    write_json(
        args.derived_run_dir / "development-ledger-activation.json",
        {"kind": "r015_unlimited_development_ledger_activation", "ledger_path": str(args.ledger), "ledger_limit": ledger["limit"], "calls_preserved": len(ledger["calls"]), "limit_history": ledger.get("limit_history", [])},
    )
    client = ManagerClient(profile, args.derived_run_dir, current_contract, args.ledger, exact_token_counter=ServerMessageTokenCounter(profile.base_url))
    manifest = {
        "schema_version": 1,
        "kind": "r015_derived_r012_event_extraction_continuation",
        "created_at_utc": utc_now(),
        "live_manager": True,
        "fixture": False,
        "prior_run": file_ref(args.prior_run_dir / "manifest.json"),
        "prior_status": file_ref(args.prior_run_dir / "run-status.json"),
        "source_manifest": file_ref(args.source_manifest),
        "contract": current_contract,
        "contract_snapshot": snapshot,
        "prompts": prompt_refs,
        "reconciliation": {
            "source_instance_id": source_id,
            "initial_attempt_ordinal": ordinal,
            "prior_failure": file_ref(failure_path),
            "original_manager_call_id": call_id,
            "original_manager_request": file_ref(request_path),
            "original_manager_response": file_ref(response_path),
            "one_evidence_only_repair": True,
            "no_completed_initial_call_reissued": True,
        },
        "continuation_policy": "repair is a separate retry record; its success resolves the paid original slot, its cannot_repair result preserves failure and leaves only later unused initial slots",
    }
    write_json(args.derived_run_dir / "manifest.json", manifest)

    repair_messages = event_evidence_repair_messages(
        by_source[source_id]["trace"],
        repair_prompt=repair_prompt,
        original_model_output=original_output,
        validator_error=str(failure["error"]),
    )
    before = client.call_count
    try:
        repair_call = client.call_json(
            purpose=f"r012_event_evidence_repair:{source_id}:{ordinal}",
            messages=repair_messages,
            retry_of=call_id,
            call_metadata={
                "retry_kind": "evidence_only_sidecar_repair",
                "original_manager_call_id": call_id,
                "original_response": file_ref(response_path),
                "initial_attempt_ordinal": ordinal,
            },
        )
        repaired = validate_event_evidence_repair(
            repair_call["json"],
            by_source[source_id]["trace"],
            original_model_output=original_output,
            benchmark="terminal-bench",
        )
        repair_evidence = {
            "kind": "r015_evidence_only_sidecar_repair",
            "repair_request": file_ref(args.derived_run_dir / "model_calls" / repair_call["call_id"] / "request.json"),
            "repair_response": file_ref(args.derived_run_dir / "model_calls" / repair_call["call_id"] / "response.json"),
            "original_skill_json_preserved": repaired.get("action") != "cannot_repair_evidence",
            "original_model_call_id": call_id,
        }
        if repaired.get("action") == "cannot_repair_evidence":
            retry = failed_schedule.record_retry(
                retry_of=original_attempt,
                retry_kind="evidence_only_sidecar_repair_cannot_repair",
                model_call_id=repair_call["call_id"],
                evidence={**repair_evidence, "result": repaired},
            )
            write_json(source_dir / f"initial-{ordinal:02d}-repair-cannot-repair.json", retry)
        else:
            resolved = failed_schedule.resolve_evidence_only_repair(
                retry_of=original_attempt,
                model_call_id=repair_call["call_id"],
                repaired_result=repaired,
                evidence=repair_evidence,
            )
            write_json(source_dir / f"initial-{ordinal:02d}-repair-resolution.json", resolved)
    except BaseException as error:
        repair_call_id = _latest_created_call_id(client, before)
        retry = failed_schedule.record_retry(
            retry_of=original_attempt,
            retry_kind="evidence_only_sidecar_repair_failed",
            model_call_id=repair_call_id,
            evidence={"error_type": type(error).__name__, "error": str(error), "automatic_retry": False},
        )
        write_json(source_dir / f"initial-{ordinal:02d}-repair-failure.json", retry)
        _write_schedule(source_dir, failed_schedule)
        write_json(args.derived_run_dir / "run-status.json", {"status": "blocked_or_failed", "phase": "evidence_only_repair", "failure": {"source_instance_id": source_id, "initial_attempt_ordinal": ordinal, "error_type": type(error).__name__, "error": str(error), "automatic_retry": False}, "schedules": [schedule.manifest() for schedule in schedules.values()]})
        raise
    _write_schedule(source_dir, failed_schedule)

    for source in sources:
        schedule = schedules[source["source_instance_id"]]
        output_dir = args.derived_run_dir / "extraction" / "event" / schedule.source_instance_id
        if schedule.source_instance_id == source_id:
            pass
        while schedule.next_initial_attempt_ordinal is not None:
            next_ordinal = schedule.next_initial_attempt_ordinal
            before = client.call_count
            try:
                checked, accounting = _full_or_compacted_call(
                    client=client,
                    profile=profile,
                    trace=source["trace"],
                    runtime_prompt=runtime_prompt,
                    compaction_prompt=compaction_prompt,
                    prompt_refs=prompt_refs,
                    schedule=schedule,
                    source_dir=output_dir,
                    source_trace_ref=file_ref(source["trace_path"]),
                )
                record = schedule.record_initial_result(
                    checked,
                    model_call_id=accounting["model_call_id"],
                    evidence={"messages_sha256": accounting["forwarded_messages_sha256"], **accounting},
                )
                write_json(output_dir / f"initial-{next_ordinal:02d}.json", record)
                _write_schedule(output_dir, schedule)
            except BaseException as error:
                failed_call_id = _latest_created_call_id(client, before)
                failed = schedule.record_initial_failure(
                    model_call_id=failed_call_id,
                    error=error,
                    evidence={"classification": _failure_classification(error), "automatic_retry": False},
                )
                write_json(output_dir / f"initial-{next_ordinal:02d}-failure.json", failed)
                _write_schedule(output_dir, schedule)
                write_json(args.derived_run_dir / "run-status.json", {"status": "blocked_or_failed", "phase": "continuation_initial", "failure": {"source_instance_id": schedule.source_instance_id, "initial_attempt_ordinal": next_ordinal, "classification": _failure_classification(error), "error_type": type(error).__name__, "error": str(error), "automatic_retry": False}, "schedules": [item.manifest() for item in schedules.values()]})
                raise
    write_json(args.derived_run_dir / "run-status.json", {"status": "completed", "schedules": [schedule.manifest() for schedule in schedules.values()], "prior_run_immutable": True})


if __name__ == "__main__":
    main()
