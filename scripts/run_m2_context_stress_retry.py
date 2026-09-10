"""R006 retry of the isolated D03/V02 stress run after preserved over-budget failure."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from codeskill_rebuild.compaction import EvidenceCompactionError, action_observation_segments, expand_evidence_fragments
from codeskill_rebuild.manager import ManagerClient, ManagerProfile, ServerMessageTokenCounter
from codeskill_rebuild.pipeline import (
    compacted_event_extraction_messages,
    event_extraction_with_evidence_messages,
    validate_budget_summary,
    validate_event_extraction_with_evidence,
)
from codeskill_rebuild.types import contract_from_files, read_json, sha256_file, sha256_text, utc_now, write_contract_snapshot, write_json


def file_ref(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": sha256_file(path)}


def summary_messages(trace: dict, segment_steps: list[dict], prompt: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps({"task_context": trace["instruction"], "source": trace["source"], "segment_steps": segment_steps}, ensure_ascii=False)},
    ]


def tool_pair_count(fragments: list[dict]) -> int:
    calls: set[str] = set()
    for step in fragments:
        if step.get("role") == "assistant":
            calls.update(call["tool_call_id"] for call in step.get("assistant", {}).get("tool_calls", []) if isinstance(call.get("tool_call_id"), str))
        elif step.get("role") == "toolResult":
            call_id = step.get("tool_result", {}).get("tool_call_id")
            if isinstance(call_id, str):
                calls.add(call_id)
    return len(calls)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--prior-failed-run", required=True, type=Path)
    parser.add_argument("--source-trace", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--decisions", required=True, type=Path)
    parser.add_argument("--prompts-root", required=True, type=Path)
    args = parser.parse_args()
    if args.run_dir.exists():
        raise FileExistsError(args.run_dir)
    prior_status = read_json(args.prior_failed_run / "run-status.json")
    if prior_status.get("status") != "failed":
        raise ValueError("R006 retry requires the preserved failed stress run")
    trace = read_json(args.source_trace)
    config = read_json(args.config)["services"]["deepseek_flash"]
    paper_path = args.prompts_root / "paper" / "fig07_event_extraction.md"
    d04_path = args.prompts_root / "custom" / "m2_event_evidence_sidecar.md"
    compact_path = args.prompts_root / "custom" / "m2_evidence_compaction_v2.md"
    paper_prompt = paper_path.read_text(encoding="utf-8")
    d04_delta = d04_path.read_text(encoding="utf-8")
    summary_prompt = compact_path.read_text(encoding="utf-8")
    final_runtime_prompt = paper_prompt + "\n\n--- D04 runtime sidecar delta ---\n\n" + d04_delta + "\n\n--- R006/V02 compaction input delta ---\n\nThe user provides coverage summaries and a bounded set of verbatim original action-observation fragments. Use summaries only for context. Every trigger, response, outcome, and rule evidence ID in a generated skill must come from the supplied original fragments; skip when those fragments are insufficient."
    # R005 raises the global development cap from R001's original 30 to 60.
    # Keep that policy-specific cap explicit rather than changing the reusable
    # profile default used to reproduce earlier R001 artifacts.
    summary_profile = ManagerProfile(base_url=config["base_url"], model=config["model_id"], manager_context_tokens=24000, max_output_tokens=2048, max_total_calls=60)
    final_profile = ManagerProfile(base_url=config["base_url"], model=config["model_id"], manager_context_tokens=24000, max_output_tokens=8192, max_total_calls=60)
    contract = contract_from_files(args.spec, args.decisions)
    full_counter = ServerMessageTokenCounter(final_profile.base_url)
    full_messages = event_extraction_with_evidence_messages(trace, runtime_prompt=final_runtime_prompt, prior_event_ids=[])
    full_count = full_counter(full_messages)
    final_limit = final_profile.manager_context_tokens - final_profile.max_output_tokens - final_profile.safety_tokens
    if full_count <= final_limit:
        raise EvidenceCompactionError("retry must remain over budget at its declared stress cap")
    segment_target = 12000
    planning_counter = ServerMessageTokenCounter(summary_profile.base_url)
    segments = action_observation_segments(
        trace["steps"],
        message_count=planning_counter,
        messages_for_segment=lambda steps: summary_messages(trace, steps, summary_prompt),
        max_input_tokens=segment_target,
    )
    if len(segments) > 2:
        raise EvidenceCompactionError(f"R006 allows at most two summaries plus final extraction, found {len(segments)} segments")
    args.run_dir.mkdir(parents=True)
    contract_snapshot = write_contract_snapshot(args.run_dir, args.spec, args.decisions, contract)
    write_json(
        args.run_dir / "manifest.json",
        {
            "schema_version": 1,
            "kind": "m2_budget_stress_retry_r006",
            "created_at_utc": utc_now(),
            "historical_source": True,
            "fixture": False,
            "live_manager": True,
            "quality_claim": "none; validates context mechanism under declared stress only",
            "prior_failed_run": file_ref(args.prior_failed_run / "run-status.json"),
            "source_trace": file_ref(args.source_trace),
            "contract": contract,
            "contract_snapshot": contract_snapshot,
            "profiles": {"summary": summary_profile.__dict__, "final": final_profile.__dict__},
            "full_request_exact_tokens": full_count,
            "final_input_limit": final_limit,
            "full_request_state": "context_blocked_under_declared_stress_cap",
            "segment_target": segment_target,
            "segment_count": len(segments),
            "approved_max_manager_calls": 3,
            "prompts": {"paper_event": file_ref(paper_path), "d04_delta": file_ref(d04_path), "r006_summary": file_ref(compact_path), "final_runtime_sha256": sha256_text(final_runtime_prompt)},
        },
    )
    write_json(args.run_dir / "normal-full-preflight.json", {"count": full_count, "limit": final_limit, "tokenizer_exchange": full_counter.last_exchange})
    try:
        summary_manager = ManagerClient(summary_profile, args.run_dir, contract, args.ledger, ServerMessageTokenCounter(summary_profile.base_url))
        summaries = []
        selected_fragments: list[dict] = []
        last_tool_result_id = next((str(entry["source_entry_id"]) for entry in reversed(trace["steps"]) if entry.get("role") == "toolResult"), None)
        for ordinal, segment in enumerate(segments, start=1):
            call = summary_manager.call_json(
                purpose=f"m2_stress_r006_summary:{ordinal}",
                messages=summary_messages(trace, segment["steps"], summary_prompt),
                call_metadata={"prompt": file_ref(compact_path), "source_trace": file_ref(args.source_trace), "segment": {key: value for key, value in segment.items() if key != "steps"}, "phase": "R006_V02_segment_summary"},
            )
            summary = validate_budget_summary(call["json"], segment_step_ids=segment["step_ids"])
            fragments = expand_evidence_fragments(trace["steps"], summary["verbatim_evidence_step_ids"])
            if tool_pair_count(fragments) > 3:
                raise EvidenceCompactionError("one R006 segment selected more than three complete tool pairs")
            if ordinal == len(segments) and last_tool_result_id not in {str(item["source_entry_id"]) for item in fragments}:
                raise EvidenceCompactionError("final segment omitted the original final tool result")
            record = {"segment": {key: value for key, value in segment.items() if key != "steps"}, "model_call_id": call["call_id"], **summary, "expanded_fragment_step_ids": [entry["source_entry_id"] for entry in fragments], "expanded_tool_pair_count": tool_pair_count(fragments)}
            summaries.append(record)
            selected_fragments.extend(fragments)
            write_json(args.run_dir / "summaries" / f"segment-{ordinal:02d}.json", record)
        chosen_ids = {str(entry["source_entry_id"]) for entry in selected_fragments}
        fragments = [entry for entry in trace["steps"] if str(entry["source_entry_id"]) in chosen_ids]
        final_messages = compacted_event_extraction_messages(trace, evidence_summaries=summaries, original_fragments=fragments, paper_prompt=final_runtime_prompt, prior_event_ids=[])
        final_counter = ServerMessageTokenCounter(final_profile.base_url)
        final_count = final_counter(final_messages)
        write_json(args.run_dir / "final-preflight.json", {"count": final_count, "limit": final_limit, "state": "within_budget" if final_count <= final_limit else "context_blocked", "tokenizer_exchange": final_counter.last_exchange, "verbatim_fragment_step_ids": [entry["source_entry_id"] for entry in fragments]})
        if final_count > final_limit:
            raise EvidenceCompactionError("R006 summaries plus bounded original fragments still exceed the stress limit")
        final_manager = ManagerClient(final_profile, args.run_dir, contract, args.ledger, ServerMessageTokenCounter(final_profile.base_url))
        final_call = final_manager.call_json(
            purpose="m2_stress_r006_compacted_event_extract",
            messages=final_messages,
            call_metadata={"paper_prompt": file_ref(paper_path), "d04_delta": file_ref(d04_path), "r006_summary_prompt": file_ref(compact_path), "source_trace": file_ref(args.source_trace), "summary_paths": [str(args.run_dir / "summaries" / f"segment-{index:02d}.json") for index in range(1, len(summaries) + 1)], "phase": "R006_V02_compacted_event_extract"},
        )
        result = validate_event_extraction_with_evidence(final_call["json"], trace, benchmark="terminal-bench", visible_step_ids={str(entry["source_entry_id"]) for entry in fragments})
        write_json(args.run_dir / "extraction" / "event.json", {"kind": "live_manager_compacted_event_extract_r006", "model_call_id": final_call["call_id"], "result": result, "summary_count": len(summaries), "verbatim_fragment_step_ids": [entry["source_entry_id"] for entry in fragments]})
        write_json(args.run_dir / "run-status.json", {"status": "completed", "finished_at_utc": utc_now(), "manager_calls": len(summaries) + 1, "result_action": result["action"], "quality_claim": "none; context mechanism stress only"})
    except BaseException as error:
        write_json(args.run_dir / "run-status.json", {"status": "failed", "failed_at_utc": utc_now(), "error_type": type(error).__name__, "error": str(error), "quality_claim": "none; context mechanism stress only"})
        raise


if __name__ == "__main__":
    main()
