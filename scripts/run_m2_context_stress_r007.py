"""R007 stress continuation: reuse valid summary-1, retry only segment-2 and final."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from codeskill_rebuild.compaction import EvidenceCompactionError, action_observation_segments, expand_evidence_fragments
from codeskill_rebuild.manager import ManagerClient, ManagerProfile, ServerMessageTokenCounter
from codeskill_rebuild.manager_projection import assistant_tool_calls, tool_result_call_id
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
            calls.update(call["tool_call_id"] for call in assistant_tool_calls(step) if isinstance(call.get("tool_call_id"), str))
        elif step.get("role") == "toolResult":
            call_id = tool_result_call_id(step)
            if isinstance(call_id, str):
                calls.add(call_id)
    return len(calls)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--prior-r006-run", required=True, type=Path)
    parser.add_argument("--source-trace", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--decisions", required=True, type=Path)
    parser.add_argument("--prompts-root", required=True, type=Path)
    args = parser.parse_args()
    if args.run_dir.exists():
        raise FileExistsError(args.run_dir)
    if read_json(args.prior_r006_run / "run-status.json").get("status") != "failed":
        raise ValueError("R007 continuation requires the preserved failed R006 run")
    reused_summary_path = args.prior_r006_run / "summaries" / "segment-01.json"
    if not reused_summary_path.is_file():
        raise FileNotFoundError("R007 requires R006's valid segment-01 checkpoint")
    trace = read_json(args.source_trace)
    config = read_json(args.config)["services"]["deepseek_flash"]
    paper_path = args.prompts_root / "paper" / "fig07_event_extraction.md"
    d04_path = args.prompts_root / "custom" / "m2_event_evidence_sidecar.md"
    compact_path = args.prompts_root / "custom" / "m2_evidence_compaction_v2.md"
    paper_prompt = paper_path.read_text(encoding="utf-8")
    d04_delta = d04_path.read_text(encoding="utf-8")
    summary_prompt = compact_path.read_text(encoding="utf-8")
    final_runtime_prompt = paper_prompt + "\n\n--- D04 runtime sidecar delta ---\n\n" + d04_delta + "\n\n--- R006/V02 compaction input delta ---\n\nThe user provides coverage summaries and a bounded set of verbatim original action-observation fragments. Use summaries only for context. Every trigger, response, outcome, and rule evidence ID in a generated skill must come from the supplied original fragments; skip when those fragments are insufficient."
    # R007 raises summary output to the same 8192 inclusive-of-reasoning limit
    # used by final extraction.  The exact 24k preflight allowance therefore
    # remains 11712 for each new summary and final request.
    profile = ManagerProfile(base_url=config["base_url"], model=config["model_id"], manager_context_tokens=24000, max_output_tokens=8192, max_total_calls=60)
    final_limit = profile.manager_context_tokens - profile.max_output_tokens - profile.safety_tokens
    contract = contract_from_files(args.spec, args.decisions)
    full_counter = ServerMessageTokenCounter(profile.base_url)
    full_count = full_counter(event_extraction_with_evidence_messages(trace, runtime_prompt=final_runtime_prompt, prior_event_ids=[]))
    if full_count <= final_limit:
        raise EvidenceCompactionError("R007 must retain a full raw request over the declared final stress allowance")
    segment_target = final_limit
    segment_counter = ServerMessageTokenCounter(profile.base_url)
    segments = action_observation_segments(
        trace["steps"],
        message_count=segment_counter,
        messages_for_segment=lambda steps: summary_messages(trace, steps, summary_prompt),
        max_input_tokens=segment_target,
    )
    if len(segments) != 2:
        raise EvidenceCompactionError(f"R007 checkpoint reuse requires exactly two segments, found {len(segments)}")
    reused = read_json(reused_summary_path)
    reused_summary = validate_budget_summary(reused, segment_step_ids=segments[0]["step_ids"])
    reused_fragments = expand_evidence_fragments(trace["steps"], reused_summary["verbatim_evidence_step_ids"])
    if tool_pair_count(reused_fragments) > 3:
        raise EvidenceCompactionError("reused R006 summary has more than three complete tool pairs")
    ledger = read_json(args.ledger)
    previous_stress_calls = [item for item in ledger.get("calls", []) if str(item.get("purpose", "")).startswith("m2_stress")]
    if len(previous_stress_calls) != 5:
        raise EvidenceCompactionError(f"R007 expected five preserved stress completions, found {len(previous_stress_calls)}")
    args.run_dir.mkdir(parents=True)
    contract_snapshot = write_contract_snapshot(args.run_dir, args.spec, args.decisions, contract)
    write_json(
        args.run_dir / "manifest.json",
        {
            "schema_version": 1,
            "kind": "m2_budget_stress_continuation_r007",
            "created_at_utc": utc_now(),
            "historical_source": True,
            "fixture": False,
            "live_manager": True,
            "quality_claim": "none; validates context mechanism under declared stress only",
            "contract": contract,
            "contract_snapshot": contract_snapshot,
            "source_trace": file_ref(args.source_trace),
            "prior_r006_run": file_ref(args.prior_r006_run / "run-status.json"),
            "reused_summary": file_ref(reused_summary_path),
            "reused_summary_model_call_id": reused.get("model_call_id"),
            "profile": profile.__dict__,
            "full_request_exact_tokens": full_count,
            "final_input_limit": final_limit,
            "full_request_state": "context_blocked_under_declared_stress_cap",
            "segment_target": segment_target,
            "segment_exact_input_tokens": [segment["exact_input_tokens"] for segment in segments],
            "stress_completion_budget": {"limit": 12, "consumed_before_run": len(previous_stress_calls), "max_new_completions": 2, "maximum_after_run": len(previous_stress_calls) + 2},
            "prompts": {"paper_event": file_ref(paper_path), "d04_delta": file_ref(d04_path), "r006_summary": file_ref(compact_path), "final_runtime_sha256": sha256_text(final_runtime_prompt)},
        },
    )
    write_json(args.run_dir / "normal-full-preflight.json", {"count": full_count, "limit": final_limit, "tokenizer_exchange": full_counter.last_exchange})
    write_json(args.run_dir / "summaries" / "segment-01-reused.json", {"kind": "reused_valid_r006_summary", "source": file_ref(reused_summary_path), "summary": reused_summary, "expanded_fragment_step_ids": [entry["source_entry_id"] for entry in reused_fragments], "expanded_tool_pair_count": tool_pair_count(reused_fragments)})
    try:
        manager = ManagerClient(profile, args.run_dir, contract, args.ledger, ServerMessageTokenCounter(profile.base_url))
        segment = segments[1]
        summary_call = manager.call_json(
            purpose="m2_stress_r007_summary:2",
            messages=summary_messages(trace, segment["steps"], summary_prompt),
            call_metadata={"prompt": file_ref(compact_path), "source_trace": file_ref(args.source_trace), "segment": {key: value for key, value in segment.items() if key != "steps"}, "phase": "R007_segment_summary", "reused_summary": file_ref(reused_summary_path)},
        )
        summary = validate_budget_summary(summary_call["json"], segment_step_ids=segment["step_ids"])
        new_fragments = expand_evidence_fragments(trace["steps"], summary["verbatim_evidence_step_ids"])
        if tool_pair_count(new_fragments) > 3:
            raise EvidenceCompactionError("R007 segment-02 selected more than three complete tool pairs")
        last_tool_result_id = next((str(entry["source_entry_id"]) for entry in reversed(trace["steps"]) if entry.get("role") == "toolResult"), None)
        if last_tool_result_id not in {str(item["source_entry_id"]) for item in new_fragments}:
            raise EvidenceCompactionError("R007 final segment omitted the original final tool result")
        new_record = {"kind": "live_manager_r007_summary", "segment": {key: value for key, value in segment.items() if key != "steps"}, "model_call_id": summary_call["call_id"], **summary, "expanded_fragment_step_ids": [entry["source_entry_id"] for entry in new_fragments], "expanded_tool_pair_count": tool_pair_count(new_fragments)}
        write_json(args.run_dir / "summaries" / "segment-02.json", new_record)
        summaries = [reused_summary, summary]
        chosen_ids = {str(entry["source_entry_id"]) for entry in reused_fragments + new_fragments}
        fragments = [entry for entry in trace["steps"] if str(entry["source_entry_id"]) in chosen_ids]
        final_messages = compacted_event_extraction_messages(trace, evidence_summaries=summaries, original_fragments=fragments, paper_prompt=final_runtime_prompt, prior_event_ids=[])
        final_counter = ServerMessageTokenCounter(profile.base_url)
        final_count = final_counter(final_messages)
        write_json(args.run_dir / "final-preflight.json", {"count": final_count, "limit": final_limit, "state": "within_budget" if final_count <= final_limit else "context_blocked", "tokenizer_exchange": final_counter.last_exchange, "verbatim_fragment_step_ids": [entry["source_entry_id"] for entry in fragments]})
        if final_count > final_limit:
            raise EvidenceCompactionError("R007 summaries plus bounded original fragments still exceed the stress allowance")
        final_call = manager.call_json(
            purpose="m2_stress_r007_compacted_event_extract",
            messages=final_messages,
            call_metadata={"paper_prompt": file_ref(paper_path), "d04_delta": file_ref(d04_path), "r006_summary_prompt": file_ref(compact_path), "source_trace": file_ref(args.source_trace), "reused_summary": file_ref(reused_summary_path), "new_summary": file_ref(args.run_dir / "summaries" / "segment-02.json"), "phase": "R007_compacted_event_extract"},
        )
        result = validate_event_extraction_with_evidence(final_call["json"], trace, benchmark="terminal-bench", visible_step_ids={str(entry["source_entry_id"]) for entry in fragments})
        write_json(args.run_dir / "extraction" / "event.json", {"kind": "live_manager_compacted_event_extract_r007", "model_call_id": final_call["call_id"], "result": result, "summary_count": 2, "reused_summary_source": file_ref(reused_summary_path), "verbatim_fragment_step_ids": [entry["source_entry_id"] for entry in fragments]})
        write_json(args.run_dir / "run-status.json", {"status": "completed", "finished_at_utc": utc_now(), "manager_calls": 2, "result_action": result["action"], "quality_claim": "none; context mechanism stress only"})
    except BaseException as error:
        write_json(args.run_dir / "run-status.json", {"status": "failed", "failed_at_utc": utc_now(), "error_type": type(error).__name__, "error": str(error), "quality_claim": "none; context mechanism stress only"})
        raise


if __name__ == "__main__":
    main()
