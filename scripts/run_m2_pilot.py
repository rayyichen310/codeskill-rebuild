"""Run the bounded R001 normal-context M2 pipeline without legacy code reuse."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from codeskill_rebuild.bank import SkillBank
from codeskill_rebuild.manager import ManagerClient, ManagerProfile, ServerMessageTokenCounter
from codeskill_rebuild.pipeline import (
    description_messages,
    event_extraction_messages,
    maintenance_messages,
    pairing_messages,
    task_extraction_messages,
    validate_description,
    validate_extraction,
    validate_pairing,
)
from codeskill_rebuild.retrieval import MiniLMEncoder, cosine
from codeskill_rebuild.types import canonical_instance_id, contract_from_files, read_json, sha256_file, utc_now, write_contract_snapshot, write_json


def file_ref(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": sha256_file(path)}


def read_prompt(path: Path) -> tuple[str, dict[str, str]]:
    return path.read_text(encoding="utf-8"), file_ref(path)


def initialize_ledger(path: Path) -> None:
    """Account for the two completed M1 manager completions before M2 starts."""
    if path.exists():
        ledger = read_json(path)
        if len(ledger.get("calls", [])) < 2:
            raise ValueError("existing ledger omits completed M1 manager calls")
        return
    write_json(
        path,
        {
            "schema_version": 1,
            "limit": 30,
            "calls": [
                {
                    "run_dir": "runs/m1-manager-shortprobe-20260905-01",
                    "call_id": "call-0001",
                    "purpose": "m1_manager_transport_probe",
                    "status": "succeeded",
                    "response_path": "runs/m1-manager-shortprobe-20260905-01/model_calls/call-0001/response.json",
                },
                {
                    "run_dir": "runs/m1-manager-high-reasoning-20260905-01",
                    "call_id": "call-0001",
                    "purpose": "m1_manager_reasoning_probe",
                    "status": "succeeded",
                    "response_path": "runs/m1-manager-high-reasoning-20260905-01/model_calls/call-0001/response.json",
                },
            ],
        },
    )


def maintenance_candidates(
    bank: SkillBank,
    candidate: dict[str, Any],
    candidate_sources: list[str],
    encoder: MiniLMEncoder,
    *,
    threshold: float,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    active = [
        skill
        for skill in bank.skills
        if skill.get("status") == "active"
        and skill.get("granularity") == candidate["granularity"]
        and not (set(skill.get("provenance", {}).get("source_instance_ids", [])) & set(candidate_sources))
    ]
    candidate_vector, candidate_record = encoder.index_skill(candidate)
    indexed = [encoder.index_skill(skill) for skill in active]
    scores = []
    for skill, (vector, record) in zip(active, indexed, strict=True):
        score = cosine(candidate_vector, vector)
        if score >= threshold:
            scores.append({"skill": skill, "score": score, "index_record": record})
    scores.sort(key=lambda item: (-item["score"], item["skill"]["skill_id"]))
    selected = scores[:limit]
    return [item["skill"] for item in selected], {
        "kind": "m2_unfrozen_pilot_maintenance_retrieval",
        "threshold": threshold,
        "limit": limit,
        "candidate_index_record": candidate_record,
        "ranked": [
            {"skill_id": item["skill"]["skill_id"], "score": item["score"], "index_record": item["index_record"]}
            for item in selected
        ],
        "excluded_overlap_source_ids": sorted(set(candidate_sources)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--decisions", required=True, type=Path)
    parser.add_argument("--prompts-root", required=True, type=Path)
    parser.add_argument("--anchor-id", required=True)
    args = parser.parse_args()
    run_dir = args.run_dir
    source_manifest_path = run_dir / "source-manifest.json"
    if not source_manifest_path.is_file():
        raise FileNotFoundError("import_m2_sources.py must create source-manifest.json first")
    if (run_dir / "manifest.json").exists():
        raise FileExistsError("refusing to overwrite an existing M2 run manifest")
    source_manifest = read_json(source_manifest_path)
    sources = source_manifest.get("sources", [])
    if not 1 <= len(sources) <= 4 or not all(item.get("text_manager_eligible") for item in sources):
        raise ValueError("M2 requires 1–4 text-manager eligible imported sources")
    source_by_id = {item["canonical_instance_id"]: item for item in sources}
    anchor_id = canonical_instance_id(args.anchor_id)
    if anchor_id not in source_by_id:
        raise ValueError("anchor must be an imported canonical source ID")
    initialize_ledger(args.ledger)
    service = read_json(args.config)["services"]["deepseek_flash"]
    profile = ManagerProfile(base_url=service["base_url"], model=service["model_id"])
    contract = contract_from_files(args.spec, args.decisions)
    manager = ManagerClient(
        profile,
        run_dir,
        contract,
        args.ledger,
        ServerMessageTokenCounter(profile.base_url),
    )
    description_prompt, description_prompt_ref = read_prompt(args.prompts_root / "custom" / "m2_description.md")
    pairing_prompt, pairing_prompt_ref = read_prompt(args.prompts_root / "custom" / "m2_task_pairing.md")
    task_prompt, task_prompt_ref = read_prompt(args.prompts_root / "paper" / "fig06_task_extraction.md")
    event_prompt, event_prompt_ref = read_prompt(args.prompts_root / "paper" / "fig07_event_extraction.md")
    maintenance_prompt, maintenance_prompt_ref = read_prompt(args.prompts_root / "paper" / "fig09_maintenance.md")
    contract_snapshot = write_contract_snapshot(run_dir, args.spec, args.decisions, contract)
    write_json(
        run_dir / "manifest.json",
        {
            "schema_version": 1,
            "kind": "m2_normal_context_pilot",
            "created_at_utc": utc_now(),
            "historical_source": True,
            "live_manager": True,
            "fixture": False,
            "source_manifest": file_ref(source_manifest_path),
            "contract": contract,
            "contract_snapshot": contract_snapshot,
            "ledger": file_ref(args.ledger),
            "anchor_id": anchor_id,
            "profile": profile.__dict__,
            "prompts": {
                "description": description_prompt_ref,
                "pairing": pairing_prompt_ref,
                "task_extract": task_prompt_ref,
                "event_extract": event_prompt_ref,
                "maintenance": maintenance_prompt_ref,
            },
            "maintenance_retrieval": {
                "threshold": 0.0,
                "limit": 4,
                "status": "unfrozen_m2_pilot_only_not_formal_profile",
            },
        },
    )
    write_json(run_dir / "run-status.json", {"status": "running", "started_at_utc": utc_now()})
    try:
        traces: dict[str, dict[str, Any]] = {}
        descriptions: dict[str, dict[str, Any]] = {}
        for source in sources:
            canonical_id = source["canonical_instance_id"]
            trace_path = Path(source["normalized_path"])
            trace = read_json(trace_path)
            if trace["source"]["canonical_instance_id"] != canonical_id:
                raise ValueError(f"normalized identity mismatch: {canonical_id}")
            traces[canonical_id] = trace
            call = manager.call_json(
                purpose=f"m2_description:{canonical_id}",
                messages=description_messages(trace, custom_prompt=description_prompt),
                call_metadata={"prompt": description_prompt_ref, "source": source, "phase": "D01_description"},
            )
            description = validate_description(call["json"], trace)
            descriptions[canonical_id] = description
            write_json(
                run_dir / "descriptions" / f"{canonical_id}.json",
                {
                    "kind": "live_manager_description",
                    "historical_source": True,
                    "source": source,
                    "model_call_id": call["call_id"],
                    "description": description,
                    "source_step_ids_available": [step["source_entry_id"] for step in trace["steps"]],
                },
            )

        encoder = MiniLMEncoder()
        encoder_fingerprint = encoder.load()
        vectors: dict[str, list[float]] = {}
        index_records: dict[str, dict[str, Any]] = {}
        for canonical_id, description in descriptions.items():
            vectors[canonical_id], index_records[canonical_id] = encoder.index_description(description)
        ranked = []
        for candidate_id in sorted(descriptions):
            if candidate_id == anchor_id:
                continue
            ranked.append(
                {
                    "canonical_instance_id": candidate_id,
                    "score": cosine(vectors[anchor_id], vectors[candidate_id]),
                    "description": descriptions[candidate_id],
                    "source": source_by_id[candidate_id],
                }
            )
        ranked.sort(key=lambda item: (-item["score"], item["canonical_instance_id"]))
        ranked = ranked[:12]
        retrieval_record = {
            "kind": "live_minilm_description_candidate_ranking",
            "encoder": encoder_fingerprint,
            "anchor_id": anchor_id,
            "anchor_index_record": index_records[anchor_id],
            "ranked_candidates": [
                {
                    "canonical_instance_id": item["canonical_instance_id"],
                    "score": item["score"],
                    "index_record": index_records[item["canonical_instance_id"]],
                }
                for item in ranked
            ],
            "max_candidates": 12,
        }
        write_json(run_dir / "group_selection" / "minilm-ranking.json", retrieval_record)
        pairing_call = manager.call_json(
            purpose=f"m2_pairing:{anchor_id}",
            messages=pairing_messages(
                {"canonical_instance_id": anchor_id, "description": descriptions[anchor_id]},
                [
                    {"canonical_instance_id": item["canonical_instance_id"], "description": item["description"]}
                    for item in ranked
                ],
                custom_prompt=pairing_prompt,
            ),
            call_metadata={"prompt": pairing_prompt_ref, "ranking": file_ref(run_dir / "group_selection" / "minilm-ranking.json"), "phase": "D02_pairing"},
        )
        pairing = validate_pairing(
            pairing_call["json"],
            anchor_id=anchor_id,
            candidate_ids={item["canonical_instance_id"] for item in ranked},
        )
        write_json(
            run_dir / "group_selection" / "pairing.json",
            {"kind": "live_manager_pairing", "model_call_id": pairing_call["call_id"], "pairing": pairing, "ranking_path": str(run_dir / "group_selection" / "minilm-ranking.json")},
        )

        candidates: list[dict[str, Any]] = []
        if pairing["action"] == "select":
            group_ids = sorted(pairing["selected_instance_ids"])
            task_call = manager.call_json(
                purpose="m2_task_extract:" + "+".join(group_ids),
                messages=task_extraction_messages([traces[item] for item in group_ids], paper_prompt=task_prompt),
                call_metadata={"prompt": task_prompt_ref, "group": pairing, "phase": "P03_task_extract"},
            )
            task_result = validate_extraction(task_call["json"], benchmark="terminal-bench", expected_granularity="task")
            task_record = {"model_call_id": task_call["call_id"], "source_instance_ids": group_ids, "result": task_result}
            write_json(run_dir / "extraction" / "task.json", task_record)
            if task_result["action"] == "generate":
                candidates.append({"kind": "task", "record_path": str(run_dir / "extraction" / "task.json"), "source_instance_ids": group_ids, "skill": task_result["skill"], "model_call_id": task_call["call_id"]})
        else:
            write_json(run_dir / "extraction" / "task.json", {"kind": "task_extraction_not_called", "reason": "no_related_group", "pairing": pairing})

        event_call = manager.call_json(
            purpose=f"m2_event_extract:{anchor_id}",
            messages=event_extraction_messages(traces[anchor_id], paper_prompt=event_prompt, prior_event_ids=[]),
            call_metadata={"prompt": event_prompt_ref, "source": source_by_id[anchor_id], "phase": "P04_event_extract"},
        )
        event_result = validate_extraction(event_call["json"], benchmark="terminal-bench", expected_granularity="event")
        write_json(run_dir / "extraction" / "event.json", {"model_call_id": event_call["call_id"], "source_instance_ids": [anchor_id], "result": event_result})
        if event_result["action"] == "generate":
            candidates.append({"kind": "event", "record_path": str(run_dir / "extraction" / "event.json"), "source_instance_ids": [anchor_id], "skill": event_result["skill"], "model_call_id": event_call["call_id"]})

        bank = SkillBank.empty("terminal-bench")
        bank_path = run_dir / "bank_snapshots" / "skill-bank.json"
        bank.save(bank_path)
        maintenance_records = []
        for ordinal, candidate in enumerate(candidates, start=1):
            retrieved, retrieval = maintenance_candidates(bank, candidate["skill"], candidate["source_instance_ids"], encoder, threshold=0.0, limit=4)
            retrieval_path = run_dir / "retrieval" / f"maintenance-{ordinal:02d}.json"
            write_json(retrieval_path, retrieval)
            maintenance_call = manager.call_json(
                purpose=f"m2_maintenance:{candidate['kind']}:{ordinal}",
                messages=maintenance_messages(candidate["skill"], retrieved, paper_prompt=maintenance_prompt),
                call_metadata={"prompt": maintenance_prompt_ref, "candidate_record": candidate["record_path"], "retrieval": file_ref(retrieval_path), "phase": "P07_maintenance"},
            )
            model_decision = maintenance_call["json"]
            decision = model_decision.get("action") if isinstance(model_decision, dict) else None
            if decision not in {"add", "merge", "drop"}:
                raise ValueError("maintenance must return add, merge, or drop")
            merged_skill = candidate["skill"]
            merge_target_id = None
            if decision == "merge":
                merge_target_id = model_decision.get("merge_target_skill_id")
                if not isinstance(merge_target_id, str) or merge_target_id not in {skill["skill_id"] for skill in retrieved}:
                    raise ValueError("maintenance merge target is not one retrieved skill")
                merged_skill = validate_extraction({"action": "generate", "skill": model_decision.get("skill")}, benchmark="terminal-bench", expected_granularity=candidate["skill"]["granularity"])["skill"]
            operation = bank.apply_and_save(
                bank_path,
                operation_id=f"m2-maintenance-{ordinal:02d}-{maintenance_call['call_id']}",
                decision=decision,
                candidate=merged_skill,
                source_instance_ids=candidate["source_instance_ids"],
                evidence={
                    "candidate_record": candidate["record_path"],
                    "candidate_model_call_id": candidate["model_call_id"],
                    "maintenance_model_call_id": maintenance_call["call_id"],
                    "retrieval_path": str(retrieval_path),
                    "source_instance_ids": candidate["source_instance_ids"],
                },
                merge_target_id=merge_target_id,
            )
            record = {"candidate": candidate, "model_call_id": maintenance_call["call_id"], "model_decision": model_decision, "operation": operation, "bank_snapshot": bank.snapshot()}
            maintenance_records.append(record)
            write_json(run_dir / "operations" / f"maintenance-{ordinal:02d}.json", record)
        write_json(run_dir / "bank_snapshots" / "final.json", bank.to_dict())
        write_json(
            run_dir / "run-status.json",
            {
                "status": "completed",
                "finished_at_utc": utc_now(),
                "candidate_count": len(candidates),
                "maintenance_operation_count": len(maintenance_records),
                "note": "Normal-context M2 pilot only. Evolution requires a later live skill-conditioned trajectory and is not claimed here.",
            },
        )
    except BaseException as error:
        write_json(run_dir / "run-status.json", {"status": "failed", "failed_at_utc": utc_now(), "error_type": type(error).__name__, "error": str(error)})
        raise


if __name__ == "__main__":
    main()
