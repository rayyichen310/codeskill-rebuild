"""Execute the D03/V02 budget-stress fallback in an isolated, capped run."""

from __future__ import annotations

import argparse
from pathlib import Path

from codeskill_rebuild.compaction import EvidenceCompactionError, action_observation_segments, expand_evidence_fragments
from codeskill_rebuild.manager import ManagerClient, ManagerProfile, ServerMessageTokenCounter
from codeskill_rebuild.pipeline import (
    compacted_event_extraction_messages,
    event_extraction_messages,
    validate_event_extraction_with_evidence,
    validate_evidence_summary,
)
from codeskill_rebuild.types import read_json, sha256_file, sha256_text, utc_now, write_json


def file_ref(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": sha256_file(path)}


def summary_messages(trace: dict, segment_steps: list[dict], prompt: str) -> list[dict[str, str]]:
    import json

    return [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task_context": trace["instruction"],
                    "source": trace["source"],
                    "segment_steps": segment_steps,
                    "full_trace_available_after_summary": False,
                },
                ensure_ascii=False,
            ),
        },
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--source-trace", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--decisions", required=True, type=Path)
    parser.add_argument("--prompts-root", required=True, type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir
    if run_dir.exists():
        raise FileExistsError(f"stress run already exists: {run_dir}")
    trace = read_json(args.source_trace)
    config = read_json(args.config)["services"]["deepseek_flash"]
    paper_path = args.prompts_root / "paper" / "fig07_event_extraction.md"
    d04_path = args.prompts_root / "custom" / "m2_event_evidence_sidecar.md"
    compact_path = args.prompts_root / "custom" / "m2_evidence_compaction.md"
    paper_prompt = paper_path.read_text(encoding="utf-8")
    d04_delta = d04_path.read_text(encoding="utf-8")
    compact_prompt = compact_path.read_text(encoding="utf-8")
    runtime_final_prompt = paper_prompt + "\n\n--- D04 runtime sidecar delta ---\n\n" + d04_delta + "\n\n--- V02 evidence-compaction delta ---\n\nThe full trajectory is replaced only by step-cited evidence summaries and their cited original fragments because this declared budget-stress variant could not fit it. Apply the same extraction rules only to this supplied evidence; skip if it is insufficient."
    summary_profile = ManagerProfile(base_url=config["base_url"], model=config["model_id"], manager_context_tokens=24000, max_output_tokens=2048)
    final_profile = ManagerProfile(base_url=config["base_url"], model=config["model_id"], manager_context_tokens=24000, max_output_tokens=8192)
    contract = {"version": "v0.5", "reproduction_spec_sha256": sha256_file(args.spec), "research_decisions_sha256": sha256_file(args.decisions)}
    preflight_counter = ServerMessageTokenCounter(final_profile.base_url)
    full_messages = event_extraction_messages(trace, paper_prompt=paper_prompt, prior_event_ids=[])
    full_count = preflight_counter(full_messages)
    final_limit = final_profile.manager_context_tokens - final_profile.max_output_tokens - final_profile.safety_tokens
    if full_count <= final_limit:
        raise EvidenceCompactionError("stress variant requires a genuinely over-budget full request at its declared cap")
    segment_limit = 10000
    planning_counter = ServerMessageTokenCounter(summary_profile.base_url)
    segments = action_observation_segments(
        trace["steps"],
        message_count=planning_counter,
        messages_for_segment=lambda values: summary_messages(trace, values, compact_prompt),
        max_input_tokens=segment_limit,
    )
    if len(segments) > 3:
        raise EvidenceCompactionError(f"stress cap would need {len(segments)} summaries plus final extraction, exceeding the approved four calls")
    run_dir.mkdir(parents=True)
    write_json(
        run_dir / "manifest.json",
        {
            "schema_version": 1,
            "kind": "m2_budget_stress_variant",
            "created_at_utc": utc_now(),
            "historical_source": True,
            "fixture": False,
            "live_manager": True,
            "quality_claim": "none; this run validates context fallback mechanics only",
            "source_trace": file_ref(args.source_trace),
            "contract": contract,
            "profiles": {"summary": summary_profile.__dict__, "final": final_profile.__dict__},
            "full_request_exact_tokens": full_count,
            "final_input_limit": final_limit,
            "full_request_state": "context_blocked_under_declared_stress_cap",
            "segment_input_limit": segment_limit,
            "segment_count": len(segments),
            "approved_max_manager_calls": 4,
            "prompts": {"paper_event": file_ref(paper_path), "d04_delta": file_ref(d04_path), "compaction": file_ref(compact_path), "final_runtime_sha256": sha256_text(runtime_final_prompt)},
        },
    )
    write_json(run_dir / "normal-full-preflight.json", {"count": full_count, "limit": final_limit, "tokenizer_exchange": preflight_counter.last_exchange})
    try:
        summary_manager = ManagerClient(summary_profile, run_dir, contract, args.ledger, ServerMessageTokenCounter(summary_profile.base_url))
        summaries = []
        for ordinal, segment in enumerate(segments, start=1):
            call = summary_manager.call_json(
                purpose=f"m2_stress_evidence_summary:{ordinal}",
                messages=summary_messages(trace, segment["steps"], compact_prompt),
                call_metadata={"prompt": file_ref(compact_path), "source_trace": file_ref(args.source_trace), "segment": {key: value for key, value in segment.items() if key != "steps"}, "phase": "V02_segment_summary"},
            )
            summary = validate_evidence_summary(call["json"], trace)
            summaries.append({"segment": {key: value for key, value in segment.items() if key != "steps"}, "model_call_id": call["call_id"], **summary})
            write_json(run_dir / "summaries" / f"segment-{ordinal:02d}.json", summaries[-1])
        cited_ids = [step_id for summary in summaries for step_id in summary["evidence_step_ids"]]
        fragments = expand_evidence_fragments(trace["steps"], cited_ids)
        final_messages = compacted_event_extraction_messages(
            trace,
            evidence_summaries=summaries,
            original_fragments=fragments,
            paper_prompt=runtime_final_prompt,
            prior_event_ids=[],
        )
        final_counter = ServerMessageTokenCounter(final_profile.base_url)
        final_count = final_counter(final_messages)
        write_json(run_dir / "final-preflight.json", {"count": final_count, "limit": final_limit, "state": "within_budget" if final_count <= final_limit else "context_blocked", "tokenizer_exchange": final_counter.last_exchange, "cited_fragment_step_ids": [entry["source_entry_id"] for entry in fragments]})
        if final_count > final_limit:
            raise EvidenceCompactionError("summaries plus cited fragments still exceed stress final budget; no extraction sent")
        final_manager = ManagerClient(final_profile, run_dir, contract, args.ledger, ServerMessageTokenCounter(final_profile.base_url))
        final_call = final_manager.call_json(
            purpose="m2_stress_compacted_event_extract",
            messages=final_messages,
            call_metadata={"paper_prompt": file_ref(paper_path), "d04_delta": file_ref(d04_path), "compaction_prompt": file_ref(compact_path), "source_trace": file_ref(args.source_trace), "summaries": [str(run_dir / "summaries" / f"segment-{index:02d}.json") for index in range(1, len(summaries) + 1)], "phase": "V02_compacted_event_extract"},
        )
        result = validate_event_extraction_with_evidence(final_call["json"], trace, benchmark="terminal-bench")
        write_json(run_dir / "extraction" / "event.json", {"kind": "live_manager_compacted_event_extract", "model_call_id": final_call["call_id"], "result": result, "summary_count": len(summaries), "cited_fragment_step_ids": [entry["source_entry_id"] for entry in fragments]})
        write_json(run_dir / "run-status.json", {"status": "completed", "finished_at_utc": utc_now(), "manager_calls": len(summaries) + 1, "result_action": result["action"], "quality_claim": "none; budget-stress context mechanism only"})
    except BaseException as error:
        write_json(run_dir / "run-status.json", {"status": "failed", "failed_at_utc": utc_now(), "error_type": type(error).__name__, "error": str(error), "quality_claim": "none; budget-stress context mechanism only"})
        raise


if __name__ == "__main__":
    main()
