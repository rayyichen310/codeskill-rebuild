"""Correct the M2 event branch without rewriting the original raw call evidence."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from codeskill_rebuild.bank import SkillBank
from codeskill_rebuild.manager import ManagerClient, ManagerProfile, ServerMessageTokenCounter
from codeskill_rebuild.pipeline import (
    event_extraction_with_evidence_messages,
    maintenance_messages,
    validate_event_extraction_with_evidence,
)
from codeskill_rebuild.types import contract_from_files, read_json, sha256_file, sha256_text, utc_now, write_contract_snapshot, write_json


def file_ref(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": sha256_file(path)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--decisions", required=True, type=Path)
    parser.add_argument("--prompts-root", required=True, type=Path)
    parser.add_argument("--source-id", default="fix-git")
    args = parser.parse_args()
    run_dir = args.run_dir
    trace_path = run_dir / "trajectories" / "normalized" / f"{args.source_id}.json"
    if not trace_path.is_file():
        raise FileNotFoundError(trace_path)
    corrected_path = run_dir / "extraction" / "event-d04-corrected.json"
    if corrected_path.exists():
        raise FileExistsError("refusing to overwrite corrected extraction evidence")
    trace = read_json(trace_path)
    config = read_json(args.config)["services"]["deepseek_flash"]
    profile = ManagerProfile(base_url=config["base_url"], model=config["model_id"])
    contract = contract_from_files(args.spec, args.decisions)
    manager = ManagerClient(
        profile,
        run_dir,
        contract,
        args.ledger,
        ServerMessageTokenCounter(profile.base_url),
    )
    paper_path = args.prompts_root / "paper" / "fig07_event_extraction.md"
    delta_path = args.prompts_root / "custom" / "m2_event_evidence_sidecar.md"
    paper_prompt = paper_path.read_text(encoding="utf-8")
    delta = delta_path.read_text(encoding="utf-8")
    runtime_prompt = paper_prompt + "\n\n--- D04 runtime sidecar delta ---\n\n" + delta
    runtime_ref = {
        "paper_prompt": file_ref(paper_path),
        "custom_delta": file_ref(delta_path),
        "composed_runtime_prompt_sha256": sha256_text(runtime_prompt),
    }
    contract_snapshot = write_contract_snapshot(run_dir, args.spec, args.decisions, contract)
    write_json(
        run_dir / "operations" / "event-granularity-review.json",
        {
            "kind": "parent_review_granularity_concern",
            "created_at_utc": utc_now(),
            "contract": contract,
            "contract_snapshot": contract_snapshot,
            "original_event_record": str(run_dir / "extraction" / "event.json"),
            "original_pilot_bank": str(run_dir / "bank_snapshots" / "skill-bank.json"),
            "finding": "The original candidate replayed the initial task-level git recovery workflow and had no D04 local trigger-response-outcome evidence sidecar.",
            "disposition": "Preserved as historical pilot evidence; excluded from formal validated-skill claims. Correction uses an isolated bank.",
        },
    )
    call = manager.call_json(
        purpose=f"m2_event_extract_d04_corrected:{args.source_id}",
        messages=event_extraction_with_evidence_messages(trace, runtime_prompt=runtime_prompt, prior_event_ids=[]),
        call_metadata={"prompt": runtime_ref, "source_trace": file_ref(trace_path), "phase": "D04_corrected_event_extract"},
    )
    result = validate_event_extraction_with_evidence(call["json"], trace, benchmark="terminal-bench")
    record = {"kind": "live_manager_event_extraction_d04_corrected", "model_call_id": call["call_id"], "source_instance_ids": [args.source_id], "runtime_prompt": runtime_ref, "result": result}
    write_json(corrected_path, record)
    if result["action"] == "skip":
        write_json(run_dir / "operations" / "event-d04-correction-status.json", {"status": "completed_skip", "finished_at_utc": utc_now(), "event_record": str(corrected_path), "maintenance": "not_called_for_skip"})
        return

    bank = SkillBank.empty("terminal-bench")
    bank_path = run_dir / "bank_snapshots" / "corrected-event-bank.json"
    bank.save(bank_path)
    maintenance_path = args.prompts_root / "paper" / "fig09_maintenance.md"
    maintenance_prompt = maintenance_path.read_text(encoding="utf-8")
    maintenance_call = manager.call_json(
        purpose=f"m2_maintenance_d04_corrected:{args.source_id}",
        messages=maintenance_messages(result["skill"], [], paper_prompt=maintenance_prompt),
        call_metadata={"prompt": file_ref(maintenance_path), "candidate_record": str(corrected_path), "retrieval": {"retrieved_skill_ids": [], "reason": "isolated_corrected_bank_empty"}, "phase": "P07_corrected_maintenance"},
    )
    decision = maintenance_call["json"].get("action") if isinstance(maintenance_call["json"], dict) else None
    if decision not in {"add", "drop"}:
        raise ValueError("empty corrected bank permits only add or drop maintenance decisions")
    operation = bank.apply_and_save(
        bank_path,
        operation_id=f"m2-d04-corrected-{maintenance_call['call_id']}",
        decision=decision,
        candidate=result["skill"],
        source_instance_ids=[args.source_id],
        evidence={"event_record": str(corrected_path), "event_model_call_id": call["call_id"], "maintenance_model_call_id": maintenance_call["call_id"]},
    )
    write_json(run_dir / "operations" / "maintenance-d04-corrected.json", {"model_call_id": maintenance_call["call_id"], "model_decision": maintenance_call["json"], "operation": operation, "bank_snapshot": bank.snapshot()})
    write_json(run_dir / "operations" / "event-d04-correction-status.json", {"status": "completed", "finished_at_utc": utc_now(), "event_record": str(corrected_path), "maintenance": "completed", "corrected_bank": str(bank_path)})


if __name__ == "__main__":
    main()
