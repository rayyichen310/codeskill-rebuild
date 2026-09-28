"""R009 fixed-order, shared-bank extraction from all ten approved text traces.

This is intentionally a single serial program: ten D02 anchor decisions,
deduplicated P03 task extractions, then ten D04 event extractions.  Every
generated candidate immediately receives one genuine Fig.9 maintenance call
against the same durable bank.  Errors are recorded by phase and never turned
into model-selected skips or hand-written skills.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from codeskill_rebuild.arm_banks import same_granularity_top5
from codeskill_rebuild.bank import SkillBank
from codeskill_rebuild.context import ContextBlocked
from codeskill_rebuild.manager import (
    ManagerCallError,
    ManagerClient,
    ManagerProfile,
    ServerMessageTokenCounter,
    update_development_ledger_limit,
)
from codeskill_rebuild.manager_projection import PROJECTION_VERSION, project_trace_for_manager
from codeskill_rebuild.pipeline import (
    event_extraction_with_evidence_messages,
    maintenance_messages,
    pairing_messages,
    task_extraction_messages,
    validate_event_extraction_with_evidence,
    validate_extraction,
    validate_maintenance,
    validate_pairing,
)
from codeskill_rebuild.retrieval import MiniLMEncoder, cosine
from codeskill_rebuild.types import (
    canonical_instance_id,
    contract_from_files,
    read_json,
    sha256_file,
    sha256_text,
    utc_now,
    write_contract_snapshot,
    write_json,
)


FULL_TEXT_SOURCE_IDS = [
    "build-pmars",
    "cancel-async-tasks",
    "cobol-modernization",
    "fix-git",
    "fix-ocaml-gc",
    "git-leak-recovery",
    "headless-terminal",
    "kv-store-grpc",
    "pypi-server",
    "schemelike-metacircular-eval",
]


def file_ref(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": sha256_file(path)}


def composed_event_prompt(prompts_root: Path) -> tuple[str, dict[str, Any]]:
    paper_path = prompts_root / "paper" / "fig07_event_extraction.md"
    delta_path = prompts_root / "custom" / "m2_event_evidence_sidecar.md"
    value = paper_path.read_text(encoding="utf-8") + "\n\n--- D04 runtime sidecar delta ---\n\n" + delta_path.read_text(encoding="utf-8")
    return value, {
        "paper_prompt": file_ref(paper_path),
        "custom_delta": file_ref(delta_path),
        "composed_runtime_prompt_sha256": sha256_text(value),
    }


def call_failure(error: BaseException) -> dict[str, str]:
    if isinstance(error, ContextBlocked):
        classification = "context_blocked"
    elif isinstance(error, ManagerCallError):
        classification = "manager_call_error"
    else:
        classification = "model_output_invalid_or_pipeline_error"
    return {"status": classification, "error_type": type(error).__name__, "error": str(error)}


def maintenance_retrieval(
    bank: SkillBank,
    candidate: dict[str, Any],
    source_ids: list[str],
    encoder: MiniLMEncoder,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Use all active same-granularity entries for Fig.9 maintenance.

    ``source_ids`` is retained in this R009-compatible call shape so archived
    scripts remain runnable.  It is deliberately not used for maintenance
    retrieval: only solver evaluation excludes same-source skills.
    """
    del source_ids
    return same_granularity_top5(bank, candidate, encoder)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--description-run", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--prompts-root", type=Path, required=True)
    args = parser.parse_args()
    if args.run_dir.exists():
        raise FileExistsError(args.run_dir)

    source_manifest = read_json(args.source_manifest)
    sources = source_manifest.get("sources")
    if not isinstance(sources, list) or source_manifest.get("source_pool_profile") != "r005_full_text_pool":
        raise ValueError("R009 needs the fixed R005 full-text source manifest")
    source_by_id = {
        canonical_instance_id(str(item.get("canonical_instance_id", ""))): item
        for item in sources
        if isinstance(item, dict)
    }
    if sorted(source_by_id) != FULL_TEXT_SOURCE_IDS or len(source_by_id) != 10:
        raise ValueError("R009 source manifest must contain exactly the fixed ten textual baseline IDs")
    if not all(item.get("text_manager_eligible") and item.get("raw_trace_kind") == "historical_baseline" for item in source_by_id.values()):
        raise ValueError("R009 only permits approved text-manager historical baselines")

    descriptions: dict[str, dict[str, Any]] = {}
    description_refs: dict[str, dict[str, str]] = {}
    for source_id in FULL_TEXT_SOURCE_IDS:
        path = args.description_run / "descriptions" / f"{source_id}.json"
        value = read_json(path)
        if value.get("source", {}).get("canonical_instance_id") != source_id:
            raise ValueError(f"description source identity mismatch: {source_id}")
        description = value.get("description")
        if not isinstance(description, dict):
            raise ValueError(f"description is missing: {source_id}")
        descriptions[source_id] = description
        description_refs[source_id] = file_ref(path)

    service = read_json(args.config)["services"]["deepseek_flash"]
    profile = ManagerProfile(base_url=service["base_url"], model=service["model_id"], max_total_calls=100)
    contract = contract_from_files(args.spec, args.decisions)
    if contract["version"] != "v0.8":
        raise ValueError(f"R009 requires v0.8 contract, found {contract['version']}")
    ledger = update_development_ledger_limit(
        args.ledger,
        new_limit=100,
        reason="R009 approved full-source common candidate pipeline",
        contract=contract,
    )
    args.run_dir.mkdir(parents=True)
    contract_snapshot = write_contract_snapshot(args.run_dir, args.spec, args.decisions, contract)
    task_prompt_path = args.prompts_root / "paper" / "fig06_task_extraction.md"
    pairing_prompt_path = args.prompts_root / "custom" / "m2_task_pairing.md"
    maintenance_prompt_path = args.prompts_root / "paper" / "fig09_maintenance.md"
    task_prompt = task_prompt_path.read_text(encoding="utf-8")
    pairing_prompt = pairing_prompt_path.read_text(encoding="utf-8")
    maintenance_prompt = maintenance_prompt_path.read_text(encoding="utf-8")
    event_prompt, event_prompt_ref = composed_event_prompt(args.prompts_root)

    raw_traces: dict[str, dict[str, Any]] = {}
    projected_traces: dict[str, dict[str, Any]] = {}
    projection_refs: dict[str, dict[str, str]] = {}
    source_trace_refs: dict[str, dict[str, str]] = {}
    for source_id in FULL_TEXT_SOURCE_IDS:
        trace_path = Path(source_by_id[source_id]["normalized_path"])
        raw_trace = read_json(trace_path)
        if canonical_instance_id(raw_trace["source"]["canonical_instance_id"]) != source_id:
            raise ValueError(f"normalized trace identity mismatch: {source_id}")
        projection = project_trace_for_manager(raw_trace)
        mapping_path = args.run_dir / "projections" / f"{source_id}.json"
        write_json(mapping_path, projection["mapping"])
        raw_traces[source_id] = raw_trace
        projected_traces[source_id] = projection["manager_trace"]
        projection_refs[source_id] = file_ref(mapping_path)
        source_trace_refs[source_id] = file_ref(trace_path)

    manifest = {
        "schema_version": 1,
        "kind": "m2_r009_full_source_common_bank",
        "created_at_utc": utc_now(),
        "historical_source": True,
        "fixture": False,
        "live_manager": True,
        "contract": contract,
        "contract_snapshot": contract_snapshot,
        "source_manifest": file_ref(args.source_manifest),
        "description_run": file_ref(args.description_run / "manifest.json"),
        "source_ids_fixed_lexical_order": FULL_TEXT_SOURCE_IDS,
        "description_refs": description_refs,
        "source_trace_refs": source_trace_refs,
        "projection": {
            "version": PROJECTION_VERSION,
            "implementation": file_ref(Path(project_trace_for_manager.__code__.co_filename)),
            "per_source_mapping": projection_refs,
        },
        "manager_profile": profile.__dict__,
        "ledger_after_authorized_limit_change": {
            "path": str(args.ledger),
            "limit": ledger["limit"],
            "calls_preserved": len(ledger["calls"]),
            "limit_history": ledger.get("limit_history", []),
        },
        "prompts": {
            "pairing": file_ref(pairing_prompt_path),
            "task_extract": file_ref(task_prompt_path),
            "event_extract": event_prompt_ref,
            "maintenance": file_ref(maintenance_prompt_path),
        },
        "fixed_schedule": {
            "pairing": "all ten anchors in canonical-ID lexical order; each ranks the other nine descriptions",
            "task": "one extraction per unique sorted source-ID group, lexical group order; maintenance immediately after generation",
            "event": "one D04 extraction per source in the same lexical source order; maintenance immediately after generation",
            "maintenance_retrieval": "same benchmark/granularity, provenance-excluded MiniLM top 5, no score threshold",
        },
    }
    write_json(args.run_dir / "manifest.json", manifest)
    write_json(args.run_dir / "run-status.json", {"status": "running", "started_at_utc": utc_now()})
    manager = ManagerClient(profile, args.run_dir, contract, args.ledger, ServerMessageTokenCounter(profile.base_url))
    encoder = MiniLMEncoder()
    encoder_fingerprint = encoder.load()
    description_vectors: dict[str, list[float]] = {}
    description_indices: dict[str, dict[str, Any]] = {}
    for source_id in FULL_TEXT_SOURCE_IDS:
        description_vectors[source_id], description_indices[source_id] = encoder.index_description(descriptions[source_id])

    bank = SkillBank.empty("terminal-bench")
    bank_path = args.run_dir / "bank_snapshots" / "skill-bank.json"
    bank.save(bank_path)
    generated_candidates = 0
    maintenance_operations = 0
    recorded_failures: list[dict[str, Any]] = []

    def maintain(*, candidate: dict[str, Any], source_ids: list[str], candidate_record: Path, candidate_call_id: str, kind: str, ordinal: int) -> None:
        nonlocal maintenance_operations
        retrieved, retrieval = maintenance_retrieval(bank, candidate, source_ids, encoder)
        retrieval_path = args.run_dir / "retrieval" / "maintenance" / f"{ordinal:03d}-{kind}.json"
        write_json(retrieval_path, {**retrieval, "encoder": encoder_fingerprint, "candidate_record": file_ref(candidate_record)})
        try:
            call = manager.call_json(
                purpose=f"r009_maintenance:{ordinal:03d}:{kind}",
                messages=maintenance_messages(candidate, retrieved, paper_prompt=maintenance_prompt),
                call_metadata={
                    "phase": "R009_P07_maintenance",
                    "prompt": file_ref(maintenance_prompt_path),
                    "candidate_record": file_ref(candidate_record),
                    "candidate_model_call_id": candidate_call_id,
                    "retrieval": file_ref(retrieval_path),
                    "bank_snapshot_before": bank.snapshot(),
                },
            )
            decision = validate_maintenance(
                call["json"],
                candidate=candidate,
                retrieved_skill_ids={skill["skill_id"] for skill in retrieved},
                retrieved_skills=retrieved,
            )
        except BaseException as error:
            failure = {"phase": "maintenance", "ordinal": ordinal, "kind": kind, "candidate_record": str(candidate_record), **call_failure(error)}
            recorded_failures.append(failure)
            write_json(args.run_dir / "operations" / f"maintenance-{ordinal:03d}-{kind}.json", failure)
            return
        final_candidate = decision.get("skill", candidate)
        operation = bank.apply_and_save(
            bank_path,
            operation_id=f"r009-maintenance-{ordinal:03d}-{call['call_id']}",
            decision=decision["action"],
            candidate=final_candidate,
            source_instance_ids=source_ids,
            evidence={
                "candidate_record": file_ref(candidate_record),
                "candidate_model_call_id": candidate_call_id,
                "maintenance_model_call_id": call["call_id"],
                "retrieval": file_ref(retrieval_path),
                "source_instance_ids": source_ids,
                "candidate_kind": kind,
            },
            merge_target_id=decision.get("merge_target_skill_id"),
        )
        maintenance_operations += 1
        write_json(
            args.run_dir / "operations" / f"maintenance-{ordinal:03d}-{kind}.json",
            {
                "kind": "live_manager_r009_maintenance",
                "candidate_record": file_ref(candidate_record),
                "candidate_model_call_id": candidate_call_id,
                "model_call_id": call["call_id"],
                "model_decision": decision,
                "operation": operation,
                "bank_snapshot_after": bank.snapshot(),
            },
        )

    try:
        # D02: every anchor is processed even if other anchors had a failure.
        selected_groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for anchor_id in FULL_TEXT_SOURCE_IDS:
            ranked = [
                {
                    "canonical_instance_id": candidate_id,
                    "score": cosine(description_vectors[anchor_id], description_vectors[candidate_id]),
                    "description": descriptions[candidate_id],
                    "source": source_by_id[candidate_id],
                    "index_record": description_indices[candidate_id],
                }
                for candidate_id in FULL_TEXT_SOURCE_IDS
                if candidate_id != anchor_id
            ]
            ranked.sort(key=lambda item: (-item["score"], item["canonical_instance_id"]))
            if len(ranked) != 9:
                raise RuntimeError("each R009 anchor must rank exactly the other nine sources")
            ranking_path = args.run_dir / "retrieval" / "pairing" / f"{anchor_id}.json"
            write_json(
                ranking_path,
                {
                    "kind": "r009_minilm_all_other_description_ranking",
                    "anchor_id": anchor_id,
                    "encoder": encoder_fingerprint,
                    "anchor_index_record": description_indices[anchor_id],
                    "max_candidates": 12,
                    "ranked_candidates": [
                        {
                            "canonical_instance_id": item["canonical_instance_id"],
                            "score": item["score"],
                            "index_record": item["index_record"],
                        }
                        for item in ranked
                    ],
                },
            )
            pairing_path = args.run_dir / "group_selection" / f"{anchor_id}.json"
            try:
                call = manager.call_json(
                    purpose=f"r009_pairing:{anchor_id}",
                    messages=pairing_messages(
                        {"canonical_instance_id": anchor_id, "description": descriptions[anchor_id]},
                        [
                            {"canonical_instance_id": item["canonical_instance_id"], "description": item["description"]}
                            for item in ranked
                        ],
                        custom_prompt=pairing_prompt,
                    ),
                    call_metadata={
                        "phase": "R009_D02_pairing",
                        "prompt": file_ref(pairing_prompt_path),
                        "ranking": file_ref(ranking_path),
                        "anchor_description": description_refs[anchor_id],
                    },
                )
                pairing = validate_pairing(
                    call["json"], anchor_id=anchor_id, candidate_ids={item["canonical_instance_id"] for item in ranked}
                )
            except BaseException as error:
                record = {"kind": "r009_pairing_failure", "anchor_id": anchor_id, "ranking": file_ref(ranking_path), **call_failure(error)}
                recorded_failures.append({"phase": "pairing", **record})
                write_json(pairing_path, record)
                continue
            record = {
                "kind": "live_manager_r009_pairing",
                "anchor_id": anchor_id,
                "model_call_id": call["call_id"],
                "pairing": pairing,
                "ranking": file_ref(ranking_path),
            }
            write_json(pairing_path, record)
            if pairing["action"] == "select":
                group = tuple(sorted(pairing["selected_instance_ids"]))
                selected_groups.setdefault(group, []).append({"anchor_id": anchor_id, "pairing": file_ref(pairing_path)})

        write_json(
            args.run_dir / "group_selection" / "deduplicated-groups.json",
            {
                "kind": "r009_task_group_deduplication",
                "groups": [
                    {"source_instance_ids": list(group), "selected_by": selected_groups[group], "dedup_reason": "identical sorted source-ID set receives exactly one task extraction"}
                    for group in sorted(selected_groups)
                ],
            },
        )

        # P03 followed immediately by P07 for each generated task candidate.
        maintenance_ordinal = 0
        for group_ordinal, group in enumerate(sorted(selected_groups), start=1):
            extraction_path = args.run_dir / "extraction" / "task" / f"{group_ordinal:03d}-{'--'.join(group)}.json"
            try:
                call = manager.call_json(
                    purpose="r009_task_extract:" + "+".join(group),
                    messages=task_extraction_messages([projected_traces[source_id] for source_id in group], paper_prompt=task_prompt),
                    call_metadata={
                        "phase": "R009_P03_task_extract",
                        "prompt": file_ref(task_prompt_path),
                        "source_instance_ids": list(group),
                        "raw_source_traces": {source_id: source_trace_refs[source_id] for source_id in group},
                        "projection_mappings": {source_id: projection_refs[source_id] for source_id in group},
                        "selected_by_pairings": selected_groups[group],
                    },
                )
                result = validate_extraction(call["json"], benchmark="terminal-bench", expected_granularity="task")
            except BaseException as error:
                failure = {"kind": "r009_task_extraction_failure", "source_instance_ids": list(group), "selected_by": selected_groups[group], **call_failure(error)}
                recorded_failures.append({"phase": "task_extract", **failure})
                write_json(extraction_path, failure)
                continue
            write_json(
                extraction_path,
                {
                    "kind": "live_manager_r009_task_extraction",
                    "model_call_id": call["call_id"],
                    "source_instance_ids": list(group),
                    "selected_by": selected_groups[group],
                    "raw_source_traces": {source_id: source_trace_refs[source_id] for source_id in group},
                    "projection_mappings": {source_id: projection_refs[source_id] for source_id in group},
                    "result": result,
                },
            )
            if result["action"] == "generate":
                generated_candidates += 1
                maintenance_ordinal += 1
                maintain(
                    candidate=result["skill"],
                    source_ids=list(group),
                    candidate_record=extraction_path,
                    candidate_call_id=call["call_id"],
                    kind="task",
                    ordinal=maintenance_ordinal,
                )

        # P04/D04 followed immediately by P07, once per source in fixed order.
        for event_ordinal, source_id in enumerate(FULL_TEXT_SOURCE_IDS, start=1):
            extraction_path = args.run_dir / "extraction" / "event" / f"{event_ordinal:03d}-{source_id}.json"
            try:
                call = manager.call_json(
                    purpose=f"r009_event_extract:{source_id}",
                    messages=event_extraction_with_evidence_messages(
                        projected_traces[source_id], runtime_prompt=event_prompt, prior_event_ids=[]
                    ),
                    call_metadata={
                        "phase": "R009_P04_D04_event_extract",
                        "runtime_prompt": event_prompt_ref,
                        "source_instance_id": source_id,
                        "raw_source_trace": source_trace_refs[source_id],
                        "projection_mapping": projection_refs[source_id],
                        "prior_event_ids": [],
                    },
                )
                result = validate_event_extraction_with_evidence(
                    call["json"], projected_traces[source_id], benchmark="terminal-bench"
                )
            except BaseException as error:
                failure = {"kind": "r009_event_extraction_failure", "source_instance_ids": [source_id], **call_failure(error)}
                recorded_failures.append({"phase": "event_extract", **failure})
                write_json(extraction_path, failure)
                continue
            write_json(
                extraction_path,
                {
                    "kind": "live_manager_r009_event_extraction_d04",
                    "model_call_id": call["call_id"],
                    "source_instance_ids": [source_id],
                    "raw_source_trace": source_trace_refs[source_id],
                    "projection_mapping": projection_refs[source_id],
                    "result": result,
                },
            )
            if result["action"] == "generate":
                generated_candidates += 1
                maintenance_ordinal += 1
                maintain(
                    candidate=result["skill"],
                    source_ids=[source_id],
                    candidate_record=extraction_path,
                    candidate_call_id=call["call_id"],
                    kind="event",
                    ordinal=maintenance_ordinal,
                )

        bank.save(args.run_dir / "bank_snapshots" / "final.json")
        write_json(
            args.run_dir / "run-status.json",
            {
                "status": "completed",
                "finished_at_utc": utc_now(),
                "manager_calls_created_in_run": manager.call_count,
                "generated_candidate_count": generated_candidates,
                "maintenance_operation_count": maintenance_operations,
                "recorded_failure_count": len(recorded_failures),
                "recorded_failures": recorded_failures,
                "bank_sequence": bank.sequence,
                "bank_active_skill_count": len([skill for skill in bank.skills if skill.get("status") == "active"]),
                "note": "Completion means the fixed R009 pipeline reached every scheduled stage; it does not claim skill quality, M3 injection, or official task success.",
            },
        )
    except BaseException as error:
        write_json(
            args.run_dir / "run-status.json",
            {"status": "aborted", "failed_at_utc": utc_now(), "error_type": type(error).__name__, "error": str(error)},
        )
        raise


if __name__ == "__main__":
    main()
