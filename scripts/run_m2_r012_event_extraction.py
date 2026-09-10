"""Plan or explicitly execute R012 event extraction with R006/D03 fallback.

The normal path sends the complete normalized source trace.  Only an exact
message-token preflight that exceeds the declared allowance may select the
evidence-compacted path.  That path keeps the raw trace durable, summarizes
complete action-observation segments, and sends only model-selected original
fragments to the final extraction call.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from codeskill_rebuild.compaction import action_observation_segments
from codeskill_rebuild.context import ContextBlocked
from codeskill_rebuild.event_extraction import EventExtractionSchedule, MAX_INITIAL_EVENT_EXTRACTION_ATTEMPTS
from codeskill_rebuild.manager import ManagerCallError, ManagerClient, ManagerProfile, ServerMessageTokenCounter, update_development_ledger_limit
from codeskill_rebuild.pipeline import compacted_event_extraction_messages, validate_event_extraction_with_evidence, validate_r006_budget_summary
from codeskill_rebuild.types import canonical_instance_id, canonical_json, contract_from_files, read_json, sha256_file, sha256_text, utc_now, write_contract_snapshot, write_json


R006_SUMMARY_CONTRACT = """Return JSON only. `covered_step_ids` must contain every supplied segment step exactly once. Select `verbatim_evidence_step_ids` only from supplied source entries and select no more than three complete action-observation pairs after matching calls/results are expanded. Do not infer unobserved causes, results, or fixes. For the final segment, include its final original toolResult in `verbatim_evidence_step_ids`."""


def file_ref(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": sha256_file(path)}


def _sources(path: Path) -> list[dict[str, Any]]:
    manifest = read_json(path)
    values = manifest.get("sources")
    if not isinstance(values, list):
        raise ValueError("source manifest needs a sources list")
    result: list[dict[str, Any]] = []
    for source in values:
        if not isinstance(source, dict) or not isinstance(source.get("normalized_path"), str):
            raise ValueError("every source needs normalized_path")
        trace_path = Path(source["normalized_path"])
        trace = read_json(trace_path)
        source_id = canonical_instance_id(str(source.get("canonical_instance_id", "")))
        if canonical_instance_id(trace["source"]["canonical_instance_id"]) != source_id:
            raise ValueError(f"source identity mismatch for {trace_path}")
        result.append({"source_instance_id": source_id, "trace_path": trace_path, "trace": trace})
    return sorted(result, key=lambda item: item["source_instance_id"])


def summary_messages(trace: dict[str, Any], segment_steps: list[dict[str, Any]], prompt: str, *, final_segment: bool) -> list[dict[str, str]]:
    """Build a source-only R006 summary request for one safe segment."""
    return [
        {"role": "system", "content": prompt + "\n\n" + R006_SUMMARY_CONTRACT},
        {"role": "user", "content": json.dumps({"task_context": trace["instruction"], "source": trace["source"], "segment_steps": segment_steps, "final_segment": final_segment}, ensure_ascii=False)},
    ]


def _allowance(profile: ManagerProfile) -> int:
    return profile.manager_context_tokens - profile.max_output_tokens - profile.safety_tokens


def _count_exact(counter: ServerMessageTokenCounter, messages: list[dict[str, str]]) -> tuple[int, dict[str, Any] | None]:
    """Use the same server counter without reserving a manager completion."""
    try:
        return counter(messages), counter.last_exchange
    except Exception as error:
        exchange = getattr(counter, "last_exchange", None)
        raise ContextBlocked(json.dumps({"state": "tokenizer_unavailable", "error": str(error), "tokenizer_exchange": exchange})) from error


def _context_detail(error: ContextBlocked, *, phase: str, manager_call_ids: list[str]) -> ContextBlocked:
    """Retain whether an overflow occurred before or after paid summaries."""
    try:
        detail = json.loads(str(error))
    except json.JSONDecodeError:
        detail = {"state": "context_blocked", "error": str(error)}
    if not isinstance(detail, dict):
        detail = {"state": "context_blocked", "error": str(error)}
    detail.update(
        {
            "phase": phase,
            "manager_call_ids_before_block": list(manager_call_ids),
            "manager_call_count_before_block": len(manager_call_ids),
        }
    )
    return ContextBlocked(json.dumps(detail, ensure_ascii=False))


def _full_or_compacted_call(*, client: ManagerClient, profile: ManagerProfile, trace: dict[str, Any], runtime_prompt: str, compaction_prompt: str | None, prompt_refs: dict[str, Any], schedule: EventExtractionSchedule, source_dir: Path, source_trace_ref: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a validated event call and durable D03 context accounting."""
    ordinal = schedule.next_initial_attempt_ordinal
    if ordinal is None:
        raise ValueError("cannot call a stopped event extraction schedule")
    allowance = _allowance(profile)
    full_messages = schedule.messages_for_next_initial_attempt(runtime_prompt=runtime_prompt)
    full_counter = ServerMessageTokenCounter(profile.base_url)
    full_tokens, full_exchange = _count_exact(full_counter, full_messages)
    base_accounting: dict[str, Any] = {
        "allowance_tokens": allowance,
        "original_full_request_tokens": full_tokens,
        "full_request_tokenizer_exchange": full_exchange,
        "original_step_ids": [str(step["source_entry_id"]) for step in trace["steps"]],
        "prior_event_candidate_ids": schedule.prior_candidate_ids,
        "prior_event_candidates": schedule.prior_candidate_summaries,
        "full_messages_sha256": sha256_text(canonical_json(full_messages)),
        "composed_prompt_refs": prompt_refs,
    }
    if full_tokens <= allowance:
        call = client.call_json(
            purpose=f"r012_event_initial:{schedule.source_instance_id}:{ordinal}", messages=full_messages,
            call_metadata={"r012_initial_attempt_ordinal": ordinal, "trajectory_input_mode": "full", "source_trace": source_trace_ref, "original_full_request_tokens": full_tokens},
        )
        visible = {str(step["source_entry_id"]) for step in trace["steps"]}
        checked = validate_event_extraction_with_evidence(call["json"], trace, benchmark="terminal-bench", visible_step_ids=visible)
        return checked, {**base_accounting, "trajectory_input_mode": "full", "forwarded_request_tokens": call["preflight"]["estimated_input_tokens"], "forwarded_messages_sha256": sha256_text(canonical_json(full_messages)), "retained_step_ids": sorted(visible), "omitted_step_ids": [], "omission_reason": None, "model_call_id": call["call_id"], "manager_preflight": call["preflight"]}

    if compaction_prompt is None:
        raise ContextBlocked(json.dumps({"state": "context_blocked", **base_accounting, "trajectory_input_mode": "context_blocked", "phase": "full_preflight", "manager_call_ids_before_block": [], "manager_call_count_before_block": 0, "reason": "full request exceeds allowance and no --evidence-compaction-prompt was supplied"}))
    segment_counter = ServerMessageTokenCounter(profile.base_url)
    segments = action_observation_segments(
        trace["steps"],
        message_count=lambda messages: _count_exact(segment_counter, messages)[0],
        # Use the final-segment variant during packing as well, so the actual
        # final segment request cannot gain tokens after a "fits" decision.
        messages_for_segment=lambda steps: summary_messages(trace, steps, compaction_prompt, final_segment=True),
        max_input_tokens=allowance,
    )
    summaries: list[dict[str, Any]] = []
    fragments_by_id: dict[str, dict[str, Any]] = {}
    segment_records: list[dict[str, Any]] = []
    summary_call_ids: list[str] = []
    for index, segment in enumerate(segments, start=1):
        final_segment = index == len(segments)
        messages = summary_messages(trace, segment["steps"], compaction_prompt, final_segment=final_segment)
        try:
            summary_call = client.call_json(
                purpose=f"r012_event_r006_summary:{schedule.source_instance_id}:{ordinal}:{index}", messages=messages,
                call_metadata={"r012_initial_attempt_ordinal": ordinal, "trajectory_input_mode": "evidence_compacted", "phase": "R006_segment_summary", "source_trace": source_trace_ref, "segment": {key: value for key, value in segment.items() if key != "steps"}, "final_segment": final_segment},
            )
        except ContextBlocked as error:
            raise _context_detail(error, phase="summary_preflight", manager_call_ids=summary_call_ids) from error
        summary_call_ids.append(summary_call["call_id"])
        summary, fragments = validate_r006_budget_summary(summary_call["json"], segment_steps=segment["steps"], final_segment=final_segment)
        for fragment in fragments:
            fragments_by_id[str(fragment["source_entry_id"])] = fragment
        record = {
            "kind": "r012_r006_segment_summary", "model_call_id": summary_call["call_id"], "segment": {key: value for key, value in segment.items() if key != "steps"}, "final_segment": final_segment,
            "summary": summary, "expanded_original_fragment_step_ids": [str(item["source_entry_id"]) for item in fragments], "expanded_complete_tool_pair_count": sum(1 for item in fragments if item.get("role") == "toolResult"), "manager_preflight": summary_call["preflight"],
        }
        write_json(source_dir / "summaries" / f"initial-{ordinal:02d}-segment-{index:02d}.json", record)
        segment_records.append(record)
        summaries.append({**summary, "segment_step_ids": segment["step_ids"], "model_call_id": summary_call["call_id"]})
    fragments = [step for step in trace["steps"] if str(step["source_entry_id"]) in fragments_by_id]
    final_messages = compacted_event_extraction_messages(trace, evidence_summaries=summaries, original_fragments=fragments, paper_prompt=runtime_prompt, prior_event_ids=schedule.prior_candidate_ids, prior_event_candidates=schedule.prior_candidate_summaries)
    final_counter = ServerMessageTokenCounter(profile.base_url)
    try:
        final_tokens, final_exchange = _count_exact(final_counter, final_messages)
    except ContextBlocked as error:
        raise _context_detail(error, phase="final_tokenizer_after_summaries", manager_call_ids=summary_call_ids) from error
    final_preflight = {"exact_tokenizer_verified": True, "estimated_input_tokens": final_tokens, "allowed_estimated_input_tokens": allowance, "tokenizer_exchange": final_exchange}
    write_json(source_dir / f"initial-{ordinal:02d}-final-preflight.json", {"state": "within_budget" if final_tokens <= allowance else "context_blocked", **final_preflight})
    if final_tokens > allowance:
        raise ContextBlocked(json.dumps({"state": "context_blocked", **base_accounting, "trajectory_input_mode": "evidence_compacted", "phase": "final_preflight_after_summaries", "manager_call_ids_before_block": summary_call_ids, "manager_call_count_before_block": len(summary_call_ids), "forwarded_request_tokens": final_tokens, "retained_step_ids": [str(item["source_entry_id"]) for item in fragments], "omission_reason": "R006 summaries plus bounded original fragments still exceed the exact allowance"}))
    try:
        call = client.call_json(
            purpose=f"r012_event_initial:{schedule.source_instance_id}:{ordinal}", messages=final_messages,
            call_metadata={"r012_initial_attempt_ordinal": ordinal, "trajectory_input_mode": "evidence_compacted", "source_trace": source_trace_ref, "summary_records": [str(source_dir / "summaries" / f"initial-{ordinal:02d}-segment-{index:02d}.json") for index in range(1, len(segment_records) + 1)], "original_full_request_tokens": full_tokens, "forwarded_request_tokens": final_tokens},
        )
    except ContextBlocked as error:
        raise _context_detail(error, phase="final_manager_preflight_after_summaries", manager_call_ids=summary_call_ids) from error
    visible = {str(step["source_entry_id"]) for step in fragments}
    checked = validate_event_extraction_with_evidence(call["json"], trace, benchmark="terminal-bench", visible_step_ids=visible)
    all_ids = {str(step["source_entry_id"]) for step in trace["steps"]}
    return checked, {**base_accounting, "trajectory_input_mode": "evidence_compacted", "forwarded_request_tokens": call["preflight"]["estimated_input_tokens"], "forwarded_messages_sha256": sha256_text(canonical_json(final_messages)), "retained_step_ids": sorted(visible), "omitted_step_ids": sorted(all_ids - visible), "omission_reason": "D03/R006: covered by step-complete summaries; original fragments are model-selected and bounded to three complete pairs per segment", "summary_call_ids": summary_call_ids, "summary_records": segment_records, "final_preflight": final_preflight, "model_call_id": call["call_id"], "manager_preflight": call["preflight"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--runtime-prompt", type=Path, required=True)
    parser.add_argument("--evidence-compaction-prompt", type=Path)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--prior-run-reference", type=Path, action="append", default=[])
    parser.add_argument("--execute-manager", action="store_true")
    parser.add_argument("--activate-unlimited-development-ledger", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--ledger", type=Path)
    args = parser.parse_args()
    if args.run_dir.exists():
        raise FileExistsError(args.run_dir)
    if args.execute_manager and (args.config is None or args.ledger is None):
        raise ValueError("--execute-manager requires --config and --ledger")
    if args.execute_manager and not args.activate_unlimited_development_ledger:
        raise ValueError("--execute-manager requires --activate-unlimited-development-ledger; no finite ledger is silently changed")
    if args.activate_unlimited_development_ledger and not args.execute_manager:
        raise ValueError("--activate-unlimited-development-ledger only applies with --execute-manager")

    sources = _sources(args.source_manifest)
    prompt = args.runtime_prompt.read_text(encoding="utf-8")
    compaction_prompt = args.evidence_compaction_prompt.read_text(encoding="utf-8") if args.evidence_compaction_prompt else None
    prompt_refs = {
        "runtime_prompt": file_ref(args.runtime_prompt),
        "evidence_compaction_prompt": file_ref(args.evidence_compaction_prompt) if args.evidence_compaction_prompt else None,
    }
    contract = contract_from_files(args.spec, args.decisions)
    args.run_dir.mkdir(parents=True)
    contract_snapshot = write_contract_snapshot(args.run_dir, args.spec, args.decisions, contract)
    prior_references = [file_ref(path) for path in args.prior_run_reference]
    schedules = [EventExtractionSchedule(source["trace"], source_run_references=[{"normalized_trace": file_ref(source["trace_path"]), "prior_runs": prior_references}]) for source in sources]
    manifest = {
        "schema_version": 2, "kind": "m2_r012_bounded_event_extraction", "created_at_utc": utc_now(), "contract": contract, "contract_snapshot": contract_snapshot, "live_manager": args.execute_manager,
        "source_manifest": file_ref(args.source_manifest), "runtime_prompt": file_ref(args.runtime_prompt), "evidence_compaction_prompt": file_ref(args.evidence_compaction_prompt) if args.evidence_compaction_prompt else None,
        "prior_run_references": prior_references, "source_order": [schedule.source_instance_id for schedule in schedules], "maximum_initial_event_attempts_per_source": MAX_INITIAL_EVENT_EXTRACTION_ATTEMPTS,
        "aggregate_development_call_cap": None,
        "method_call_limits": {
            "initial_event_candidates_per_source": MAX_INITIAL_EVENT_EXTRACTION_ATTEMPTS,
            "stop_on_skip_or_duplicate": True,
            "r006_summary_calls": "one per exact-token-packed action-observation segment; no aggregate cost cap",
            "final_event_call": "one after all valid segment summaries and an exact final preflight",
        },
        "retry_policy": "repair and transport retries are separate records and never consume or reset initial exploration slots",
        "reuse_policy": "prior runs are preserved references; this runner does not silently reuse a prior result without an explicit input-equivalence audit", "context_policy": "full first; exact overflow only may use D03/R006 evidence_compacted fallback; final claims cite supplied original fragments only", "development_ledger_policy": "unlimited requires the explicit activation flag and preserves the durable finite history",
    }
    write_json(args.run_dir / "manifest.json", manifest)
    if not args.execute_manager:
        write_json(args.run_dir / "run-status.json", {"status": "planned_no_model_calls", "schedules": [schedule.manifest() for schedule in schedules]})
        return

    service = read_json(args.config)["services"]["deepseek_flash"]
    profile = ManagerProfile(base_url=service["base_url"], model=service["model_id"], max_total_calls=None)
    ledger = update_development_ledger_limit(args.ledger, new_limit=None, reason="R014 explicit unlimited-development-ledger activation for R012 full-first/D03 fallback", contract=contract)
    write_json(args.run_dir / "development-ledger-activation.json", {"kind": "r014_unlimited_development_ledger_activation", "ledger_path": str(args.ledger), "ledger_limit": ledger["limit"], "calls_preserved": len(ledger["calls"]), "limit_history": ledger.get("limit_history", [])})
    client = ManagerClient(profile, args.run_dir, contract, args.ledger, exact_token_counter=ServerMessageTokenCounter(profile.base_url))
    for source, schedule in zip(sources, schedules, strict=True):
        source_dir = args.run_dir / "extraction" / "event" / schedule.source_instance_id
        source_ref = file_ref(source["trace_path"])
        while schedule.next_initial_attempt_ordinal is not None:
            ordinal = schedule.next_initial_attempt_ordinal
            try:
                checked, accounting = _full_or_compacted_call(client=client, profile=profile, trace=schedule.trace, runtime_prompt=prompt, compaction_prompt=compaction_prompt, prompt_refs=prompt_refs, schedule=schedule, source_dir=source_dir, source_trace_ref=source_ref)
                record = schedule.record_initial_result(checked, model_call_id=accounting["model_call_id"], evidence={"messages_sha256": accounting["forwarded_messages_sha256"], **accounting})
            except BaseException as error:
                context: dict[str, Any] = {}
                if isinstance(error, ContextBlocked):
                    try:
                        parsed = json.loads(str(error))
                        context = parsed if isinstance(parsed, dict) else {}
                    except json.JSONDecodeError:
                        context = {}
                call_count = context.get("manager_call_count_before_block", 0)
                if isinstance(error, ContextBlocked):
                    classification = "context_blocked_after_manager_summary_calls" if isinstance(call_count, int) and call_count > 0 else "context_blocked_before_manager_call"
                elif isinstance(error, ManagerCallError):
                    classification = "manager_call_failed_or_invalid_output"
                else:
                    classification = "post_call_validation_or_runner_failure"
                failure = {"kind": "r012_event_extraction_execution_failure", "source_instance_id": schedule.source_instance_id, "initial_attempt_ordinal": ordinal, "classification": classification, "error_type": type(error).__name__, "error": str(error), "context_block_detail": context or None, "automatic_retry": False, "next_step": "preserve this artifact and obtain an explicit retry/repair decision; do not consume another initial attempt automatically"}
                write_json(source_dir / f"initial-{ordinal:02d}-failure.json", failure)
                write_json(args.run_dir / "run-status.json", {"status": "blocked_or_failed", "failure": failure, "completed_schedules": [item.manifest() for item in schedules]})
                raise
            write_json(source_dir / f"initial-{ordinal:02d}.json", record)
        write_json(source_dir / "schedule.json", schedule.manifest())
    write_json(args.run_dir / "run-status.json", {"status": "completed", "schedules": [schedule.manifest() for schedule in schedules]})


if __name__ == "__main__":
    main()
