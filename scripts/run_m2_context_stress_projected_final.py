"""Independent projected-renderer stress continuation after preserved R007 block."""

from __future__ import annotations

import argparse
from pathlib import Path

from codeskill_rebuild.compaction import EvidenceCompactionError, expand_evidence_fragments
from codeskill_rebuild.manager import ManagerClient, ManagerProfile, ServerMessageTokenCounter
from codeskill_rebuild.manager_projection import assistant_tool_calls, project_trace_for_manager, tool_result_call_id
from codeskill_rebuild.pipeline import compacted_event_extraction_messages, event_extraction_with_evidence_messages, validate_budget_summary, validate_event_extraction_with_evidence
from codeskill_rebuild.types import contract_from_files, read_json, sha256_file, sha256_text, utc_now, write_contract_snapshot, write_json


def file_ref(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": sha256_file(path)}


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
    parser.add_argument("--prior-r007-run", required=True, type=Path)
    parser.add_argument("--source-trace", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--decisions", required=True, type=Path)
    parser.add_argument("--prompts-root", required=True, type=Path)
    args = parser.parse_args()
    if args.run_dir.exists():
        raise FileExistsError(args.run_dir)
    if read_json(args.prior_r007_run / "run-status.json").get("status") != "failed":
        raise ValueError("projected continuation requires preserved R007 failure")
    reused_path = args.prior_r006_run / "summaries" / "segment-01.json"
    second_path = args.prior_r007_run / "summaries" / "segment-02.json"
    if not reused_path.is_file() or not second_path.is_file():
        raise FileNotFoundError("both valid summary checkpoints are required")
    raw_trace = read_json(args.source_trace)
    projection = project_trace_for_manager(raw_trace)
    trace = projection["manager_trace"]
    config = read_json(args.config)["services"]["deepseek_flash"]
    paper_path = args.prompts_root / "paper" / "fig07_event_extraction.md"
    d04_path = args.prompts_root / "custom" / "m2_event_evidence_sidecar.md"
    paper_prompt = paper_path.read_text(encoding="utf-8")
    d04_delta = d04_path.read_text(encoding="utf-8")
    final_runtime_prompt = paper_prompt + "\n\n--- D04 runtime sidecar delta ---\n\n" + d04_delta + "\n\n--- R006/V02 compaction input delta ---\n\nThe user provides coverage summaries and a bounded set of verbatim original action-observation fragments. Use summaries only for context. Every trigger, response, outcome, and rule evidence ID in a generated skill must come from the supplied original fragments; skip when those fragments are insufficient."
    profile = ManagerProfile(base_url=config["base_url"], model=config["model_id"], manager_context_tokens=24000, max_output_tokens=8192, max_total_calls=60)
    final_limit = profile.manager_context_tokens - profile.max_output_tokens - profile.safety_tokens
    contract = contract_from_files(args.spec, args.decisions)
    raw_counter = ServerMessageTokenCounter(profile.base_url)
    raw_full_count = raw_counter(event_extraction_with_evidence_messages(raw_trace, runtime_prompt=final_runtime_prompt, prior_event_ids=[]))
    projected_counter = ServerMessageTokenCounter(profile.base_url)
    projected_full_count = projected_counter(event_extraction_with_evidence_messages(trace, runtime_prompt=final_runtime_prompt, prior_event_ids=[]))
    if raw_full_count <= final_limit:
        raise EvidenceCompactionError("preserved unprojected full request no longer represents an over-budget stress source")
    reused = read_json(reused_path)
    second = read_json(second_path)
    first_ids = reused.get("covered_step_ids")
    second_ids = second.get("covered_step_ids")
    if not isinstance(first_ids, list) or not isinstance(second_ids, list):
        raise EvidenceCompactionError("summary checkpoints omit covered_step_ids")
    reused_summary = validate_budget_summary(reused, segment_step_ids=first_ids)
    second_summary = validate_budget_summary(second, segment_step_ids=second_ids)
    if set(first_ids) | set(second_ids) != {str(step["source_entry_id"]) for step in trace["steps"]} or set(first_ids) & set(second_ids):
        raise EvidenceCompactionError("checkpoint coverage does not partition the projected source steps")
    first_fragments = expand_evidence_fragments(trace["steps"], reused_summary["verbatim_evidence_step_ids"])
    second_fragments = expand_evidence_fragments(trace["steps"], second_summary["verbatim_evidence_step_ids"])
    if tool_pair_count(first_fragments) > 3 or tool_pair_count(second_fragments) > 3:
        raise EvidenceCompactionError("checkpoint selects too many complete tool pairs")
    final_tool_result = next((str(step["source_entry_id"]) for step in reversed(trace["steps"]) if step.get("role") == "toolResult"), None)
    if final_tool_result not in {str(step["source_entry_id"]) for step in second_fragments}:
        raise EvidenceCompactionError("second checkpoint lacks the original final observed result")
    fragments_by_id = {str(step["source_entry_id"]): step for step in first_fragments + second_fragments}
    fragments = [step for step in trace["steps"] if str(step["source_entry_id"]) in fragments_by_id]
    final_messages = compacted_event_extraction_messages(trace, evidence_summaries=[reused_summary, second_summary], original_fragments=fragments, paper_prompt=final_runtime_prompt, prior_event_ids=[])
    final_counter = ServerMessageTokenCounter(profile.base_url)
    final_count = final_counter(final_messages)
    ledger = read_json(args.ledger)
    previous_stress_calls = [item for item in ledger.get("calls", []) if str(item.get("purpose", "")).startswith("m2_stress")]
    if len(previous_stress_calls) != 6:
        raise EvidenceCompactionError(f"projected continuation expected six preserved stress completions, found {len(previous_stress_calls)}")
    args.run_dir.mkdir(parents=True)
    contract_snapshot = write_contract_snapshot(args.run_dir, args.spec, args.decisions, contract)
    write_json(args.run_dir / "projection-mapping.json", projection["mapping"])
    write_json(
        args.run_dir / "manifest.json",
        {
            "schema_version": 1,
            "kind": "m2_budget_stress_projected_final",
            "created_at_utc": utc_now(),
            "historical_source": True,
            "fixture": False,
            "live_manager": True,
            "quality_claim": "none; validates lossless renderer plus context mechanism under declared stress only",
            "contract": contract,
            "contract_snapshot": contract_snapshot,
            "source_trace": file_ref(args.source_trace),
            "projection_version": projection["projection_version"],
            "projection_mapping": file_ref(args.run_dir / "projection-mapping.json"),
            "prior_r006_summary": file_ref(reused_path),
            "prior_r007_summary": file_ref(second_path),
            "prior_r007_final_preflight": file_ref(args.prior_r007_run / "final-preflight.json"),
            "profile": profile.__dict__,
            "final_input_limit": final_limit,
            "unprojected_full_exact_tokens": raw_full_count,
            "projected_full_exact_tokens": projected_full_count,
            "projected_compacted_final_exact_tokens": final_count,
            "projected_compacted_final_state": "within_budget" if final_count <= final_limit else "context_blocked",
            "stress_completion_budget": {"limit": 12, "consumed_before_run": len(previous_stress_calls), "max_new_completions": 1, "maximum_after_run": len(previous_stress_calls) + 1},
            "prompts": {"paper_event": file_ref(paper_path), "d04_delta": file_ref(d04_path), "final_runtime_sha256": sha256_text(final_runtime_prompt)},
        },
    )
    write_json(args.run_dir / "raw-full-preflight.json", {"count": raw_full_count, "limit": final_limit, "tokenizer_exchange": raw_counter.last_exchange})
    write_json(args.run_dir / "projected-full-preflight.json", {"count": projected_full_count, "limit": final_limit, "tokenizer_exchange": projected_counter.last_exchange})
    write_json(args.run_dir / "final-preflight.json", {"count": final_count, "limit": final_limit, "state": "within_budget" if final_count <= final_limit else "context_blocked", "tokenizer_exchange": final_counter.last_exchange, "verbatim_fragment_step_ids": [step["source_entry_id"] for step in fragments]})
    if final_count > final_limit:
        write_json(args.run_dir / "run-status.json", {"status": "blocked", "blocked_at_utc": utc_now(), "reason": "projected_compacted_final_still_over_budget", "quality_claim": "none; context mechanism stress only"})
        return
    try:
        manager = ManagerClient(profile, args.run_dir, contract, args.ledger, ServerMessageTokenCounter(profile.base_url))
        call = manager.call_json(
            purpose="m2_stress_projected_final_extract",
            messages=final_messages,
            call_metadata={"source_trace": file_ref(args.source_trace), "projection_mapping": file_ref(args.run_dir / "projection-mapping.json"), "reused_summary": file_ref(reused_path), "second_summary": file_ref(second_path), "phase": "lossless_projection_v1_compacted_event_extract"},
        )
        result = validate_event_extraction_with_evidence(call["json"], trace, benchmark="terminal-bench", visible_step_ids={str(step["source_entry_id"]) for step in fragments})
        write_json(args.run_dir / "extraction" / "event.json", {"kind": "live_manager_projected_compacted_event_extract", "model_call_id": call["call_id"], "result": result, "verbatim_fragment_step_ids": [step["source_entry_id"] for step in fragments]})
        write_json(args.run_dir / "run-status.json", {"status": "completed", "finished_at_utc": utc_now(), "manager_calls": 1, "result_action": result["action"], "quality_claim": "none; context mechanism stress only"})
    except BaseException as error:
        write_json(args.run_dir / "run-status.json", {"status": "failed", "failed_at_utc": utc_now(), "error_type": type(error).__name__, "error": str(error), "quality_claim": "none; context mechanism stress only"})
        raise


if __name__ == "__main__":
    main()
