"""Execute the one approved R011 pairing calibration and provenance repairs.

R011 consumes the fixed ten historical text traces only.  It preserves R009
verbatim, makes one revised D02 decision per anchor, makes one D04 retry for
the length result and one evidence-only repair for each of five invalid sidecars,
then derives independent B/C banks from the resulting ordered candidate list.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

from codeskill_rebuild.arm_banks import direct_candidate_bank, group_exact_candidates, same_granularity_top5
from codeskill_rebuild.bank import SkillBank
from codeskill_rebuild.context import ContextBlocked
from codeskill_rebuild.manager import ManagerCallError, ManagerClient, ManagerProfile, ServerMessageTokenCounter
from codeskill_rebuild.pipeline import (
    event_evidence_repair_messages,
    event_extraction_with_evidence_messages,
    maintenance_messages,
    pairing_messages,
    task_extraction_messages,
    validate_event_evidence_repair,
    validate_event_extraction_with_evidence,
    validate_extraction,
    validate_maintenance,
    validate_pairing,
)
from codeskill_rebuild.retrieval import MiniLMEncoder, cosine
from codeskill_rebuild.types import canonical_instance_id, contract_from_files, read_json, sha256_file, sha256_text, utc_now, write_contract_snapshot, write_json


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
R009_REPAIR_SOURCE_IDS = {
    "cobol-modernization",
    "fix-ocaml-gc",
    "headless-terminal",
    "pypi-server",
    "schemelike-metacircular-eval",
}
R011_CONTRACT = {
    "version": "v0.10",
    "reproduction_spec_sha256": "28efc754a5523b43b73b51a97cd4f1702cf0a635525aff01505c2dbbee3baca5",
    "research_decisions_sha256": "06e7a9e28cf4ed955e628394116f730f5ec861b370b7bfe117c466d1fb08d857",
}


def file_ref(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": sha256_file(path)}


def call_failure(error: BaseException) -> dict[str, str]:
    if isinstance(error, ContextBlocked):
        status = "context_blocked"
    elif isinstance(error, ManagerCallError):
        status = "manager_call_error"
    else:
        status = "model_output_invalid_or_pipeline_error"
    return {"status": status, "error_type": type(error).__name__, "error": str(error)}


def _response_json(path: Path) -> dict[str, Any]:
    response = read_json(path)
    try:
        content = response["parsed_response"]["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError(f"cannot read preserved manager JSON from {path}") from error
    try:
        value = json.loads(content)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"preserved manager content is not JSON in {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"preserved manager JSON is not an object in {path}")
    return value


def _event_runtime_prompt(prompts_root: Path) -> tuple[str, dict[str, Any]]:
    paper = prompts_root / "paper" / "fig07_event_extraction.md"
    delta = prompts_root / "custom" / "m2_event_evidence_sidecar.md"
    value = paper.read_text(encoding="utf-8") + "\n\n--- D04 runtime sidecar delta ---\n\n" + delta.read_text(encoding="utf-8")
    return value, {"paper_prompt": file_ref(paper), "custom_delta": file_ref(delta), "composed_runtime_prompt_sha256": sha256_text(value)}


def _capture_source_snapshot(*, project_root: Path, run_dir: Path, relative_paths: list[str]) -> dict[str, Any]:
    """Copy every token-affecting R011 source file before the first call."""
    code = run_dir / "code"
    code.mkdir(parents=True, exist_ok=True)
    status = subprocess.run(["git", "status", "--porcelain=v1"], cwd=project_root, check=True, text=True, capture_output=True).stdout
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project_root, check=True, text=True, capture_output=True).stdout.strip()
    patch = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=project_root, check=True, text=True, capture_output=True).stdout
    (code / "working-tree-status.txt").write_text(status, encoding="utf-8")
    (code / "working-tree.patch").write_text(patch, encoding="utf-8")
    snapshots: list[dict[str, Any]] = []
    for relative in relative_paths:
        source = project_root / relative
        if not source.is_file():
            raise FileNotFoundError(source)
        destination = code / "source-snapshot" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        snapshots.append({"source_path": relative, "source_sha256": sha256_file(source), "snapshot": file_ref(destination)})
    return {
        "head": head,
        "dirty": bool(status),
        "working_tree_status": file_ref(code / "working-tree-status.txt"),
        "working_tree_patch": file_ref(code / "working-tree.patch"),
        "complete_relevant_source_snapshot": snapshots,
        "untracked_source_paths": [line[3:] for line in status.splitlines() if line.startswith("?? ")],
    }


def _descriptions(description_run: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, str]]]:
    values: dict[str, dict[str, Any]] = {}
    refs: dict[str, dict[str, str]] = {}
    for source_id in FULL_TEXT_SOURCE_IDS:
        path = description_run / "descriptions" / f"{source_id}.json"
        item = read_json(path)
        if item.get("source", {}).get("canonical_instance_id") != source_id or not isinstance(item.get("description"), dict):
            raise ValueError(f"R011 requires complete verified description for {source_id}")
        values[source_id] = item["description"]
        refs[source_id] = file_ref(path)
    return values, refs


def _r009_event_record(r009_run: Path, source_id: str) -> tuple[Path, dict[str, Any]]:
    path = r009_run / "extraction" / "event" / f"{FULL_TEXT_SOURCE_IDS.index(source_id) + 1:03d}-{source_id}.json"
    return path, read_json(path)


def _candidate(*, skill: dict[str, Any], source_ids: list[str], record: Path, kind: str, model_call_id: str | None) -> dict[str, Any]:
    return {
        "skill": deepcopy(skill),
        "source_instance_ids": list(source_ids),
        "source_instance_ids_raw": list(source_ids),
        "candidate_record": file_ref(record),
        "kind": kind,
        "model_call_id": model_call_id,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--description-run", type=Path, required=True)
    parser.add_argument("--r009-run", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--prompts-root", type=Path, required=True)
    args = parser.parse_args()
    if args.run_dir.exists():
        raise FileExistsError(args.run_dir)
    project_root = Path(__file__).resolve().parents[1]
    contract = contract_from_files(args.spec, args.decisions)
    if contract != R011_CONTRACT:
        raise ValueError(f"R011 requires the exact approved v0.10 contract, found {contract}")

    source_manifest = read_json(args.source_manifest)
    sources = source_manifest.get("sources")
    if not isinstance(sources, list) or source_manifest.get("source_pool_profile") != "r005_full_text_pool":
        raise ValueError("R011 needs the fixed R005 full text source manifest")
    source_by_id = {canonical_instance_id(str(item.get("canonical_instance_id", ""))): item for item in sources if isinstance(item, dict)}
    if sorted(source_by_id) != FULL_TEXT_SOURCE_IDS or len(source_by_id) != 10:
        raise ValueError("R011 source manifest must contain exactly the fixed ten sources")
    if not all(item.get("text_manager_eligible") and item.get("raw_trace_kind") == "historical_baseline" for item in source_by_id.values()):
        raise ValueError("R011 permits only approved historical text sources")
    descriptions, description_refs = _descriptions(args.description_run)
    args.run_dir.mkdir(parents=True)
    git = _capture_source_snapshot(
        project_root=project_root,
        run_dir=args.run_dir,
        relative_paths=[
            "scripts/run_m2_r011_calibration.py",
            "src/codeskill_rebuild/pipeline.py",
            "src/codeskill_rebuild/arm_banks.py",
            "src/codeskill_rebuild/bank.py",
            "src/codeskill_rebuild/manager.py",
            "src/codeskill_rebuild/retrieval.py",
            "src/codeskill_rebuild/types.py",
            "prompts/custom/m2_task_pairing.md",
            "prompts/custom/m2_event_evidence_repair.md",
            "prompts/custom/m2_event_evidence_sidecar.md",
            "prompts/paper/fig06_task_extraction.md",
            "prompts/paper/fig07_event_extraction.md",
            "prompts/paper/fig09_maintenance.md",
        ],
    )
    contract_snapshot = write_contract_snapshot(args.run_dir, args.spec, args.decisions, contract)
    raw_traces: dict[str, dict[str, Any]] = {}
    source_trace_refs: dict[str, dict[str, str]] = {}
    for source_id in FULL_TEXT_SOURCE_IDS:
        path = Path(source_by_id[source_id]["normalized_path"])
        trace = read_json(path)
        if canonical_instance_id(trace["source"]["canonical_instance_id"]) != source_id:
            raise ValueError(f"normalized trace identity mismatch for {source_id}")
        raw_traces[source_id] = trace
        source_trace_refs[source_id] = file_ref(path)

    service = read_json(args.config)["services"]["deepseek_flash"]
    profile = ManagerProfile(base_url=service["base_url"], model=service["model_id"], max_total_calls=100)
    task_prompt_path = args.prompts_root / "paper" / "fig06_task_extraction.md"
    pairing_prompt_path = args.prompts_root / "custom" / "m2_task_pairing.md"
    repair_prompt_path = args.prompts_root / "custom" / "m2_event_evidence_repair.md"
    maintenance_prompt_path = args.prompts_root / "paper" / "fig09_maintenance.md"
    task_prompt = task_prompt_path.read_text(encoding="utf-8")
    pairing_prompt = pairing_prompt_path.read_text(encoding="utf-8")
    repair_prompt = repair_prompt_path.read_text(encoding="utf-8")
    maintenance_prompt = maintenance_prompt_path.read_text(encoding="utf-8")
    event_prompt, event_prompt_ref = _event_runtime_prompt(args.prompts_root)
    manifest = {
        "schema_version": 1,
        "kind": "m2_r011_fixed_pairing_calibration_and_event_repair",
        "created_at_utc": utc_now(),
        "historical_source": True,
        "fixture": False,
        "live_manager": True,
        "contract": contract,
        "contract_snapshot": contract_snapshot,
        "git": git,
        "source_manifest": file_ref(args.source_manifest),
        "description_run": file_ref(args.description_run / "manifest.json"),
        "archived_r009_run": file_ref(args.r009_run / "manifest.json"),
        "source_ids_fixed_lexical_order": FULL_TEXT_SOURCE_IDS,
        "source_trace_refs": source_trace_refs,
        "description_refs": description_refs,
        "manager_profile": profile.__dict__,
        "prompts": {
            "r011_pairing": file_ref(pairing_prompt_path),
            "task_extract": file_ref(task_prompt_path),
            "event_extract": event_prompt_ref,
            "r011_event_evidence_repair": file_ref(repair_prompt_path),
            "maintenance": file_ref(maintenance_prompt_path),
        },
        "fixed_schedule": {
            "pairing": "all ten anchors in lexical canonical-ID order; all other nine descriptions ranked by MiniLM",
            "task": "one full raw-trace extraction per deduplicated selected group in lexical source-ID order",
            "event": "existing valid R009 results referenced; one build-pmars retry plus five evidence-only repairs in lexical source-ID order",
            "candidate_manifest": "task groups then sources in fixed lexical event order",
            "banks": "B deterministic schema-valid ingestion; C independent Fig.9 maintenance in candidate-manifest order",
        },
    }
    write_json(args.run_dir / "manifest.json", manifest)
    write_json(args.run_dir / "run-status.json", {"status": "running", "started_at_utc": utc_now()})
    manager = ManagerClient(profile, args.run_dir, contract, args.ledger, ServerMessageTokenCounter(profile.base_url))
    encoder = MiniLMEncoder()
    encoder_fingerprint = encoder.load()
    vectors: dict[str, list[float]] = {}
    index_records: dict[str, dict[str, Any]] = {}
    for source_id in FULL_TEXT_SOURCE_IDS:
        vectors[source_id], index_records[source_id] = encoder.index_description(descriptions[source_id])
    failures: list[dict[str, Any]] = []
    selected_groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    candidates: list[dict[str, Any]] = []

    try:
        # One revised, evidence-bound decision for every anchor.
        for anchor_id in FULL_TEXT_SOURCE_IDS:
            ranked = [
                {"canonical_instance_id": other, "score": cosine(vectors[anchor_id], vectors[other]), "description": descriptions[other], "index_record": index_records[other]}
                for other in FULL_TEXT_SOURCE_IDS if other != anchor_id
            ]
            ranked.sort(key=lambda item: (-item["score"], item["canonical_instance_id"]))
            ranking_path = args.run_dir / "retrieval" / "pairing" / f"{anchor_id}.json"
            write_json(ranking_path, {"kind": "r011_minilm_all_other_description_ranking", "anchor_id": anchor_id, "encoder": encoder_fingerprint, "anchor_index_record": index_records[anchor_id], "ranked_candidates": [{key: item[key] for key in ("canonical_instance_id", "score", "index_record")} for item in ranked]})
            output_path = args.run_dir / "group_selection" / f"{anchor_id}.json"
            try:
                call = manager.call_json(
                    purpose=f"r011_pairing:{anchor_id}",
                    messages=pairing_messages({"canonical_instance_id": anchor_id, "description": descriptions[anchor_id]}, [{"canonical_instance_id": item["canonical_instance_id"], "description": item["description"]} for item in ranked], custom_prompt=pairing_prompt),
                    call_metadata={"phase": "R011_D02_pairing_calibration", "prompt": file_ref(pairing_prompt_path), "ranking": file_ref(ranking_path), "anchor_description": description_refs[anchor_id], "prior_r009_pairing": file_ref(args.r009_run / "group_selection" / f"{anchor_id}.json")},
                )
                pairing = validate_pairing(call["json"], anchor_id=anchor_id, candidate_ids={item["canonical_instance_id"] for item in ranked}, require_shared_evidence=True)
                record = {"kind": "live_manager_r011_pairing", "anchor_id": anchor_id, "model_call_id": call["call_id"], "pairing": pairing, "ranking": file_ref(ranking_path)}
                write_json(output_path, record)
                if pairing["action"] == "select":
                    selected_groups.setdefault(tuple(sorted(pairing["selected_instance_ids"])), []).append({"anchor_id": anchor_id, "pairing": file_ref(output_path)})
            except BaseException as error:
                record = {"kind": "r011_pairing_failure", "anchor_id": anchor_id, "ranking": file_ref(ranking_path), **call_failure(error)}
                failures.append({"phase": "pairing", **record})
                write_json(output_path, record)

        groups_path = args.run_dir / "group_selection" / "deduplicated-groups.json"
        write_json(groups_path, {"kind": "r011_task_group_deduplication", "groups": [{"source_instance_ids": list(group), "selected_by": selected_groups[group], "dedup_reason": "identical sorted source-ID sets receive one task extraction"} for group in sorted(selected_groups)]})

        # Full raw trajectories are intentionally passed to Fig.6.  Context
        # preflight can block a group, but the runner never silently removes a
        # trajectory or substitutes a smaller group.
        for ordinal, group in enumerate(sorted(selected_groups), start=1):
            output_path = args.run_dir / "extraction" / "task" / f"{ordinal:03d}-{'--'.join(group)}.json"
            try:
                call = manager.call_json(
                    purpose="r011_task_extract:" + "+".join(group),
                    messages=task_extraction_messages([raw_traces[item] for item in group], paper_prompt=task_prompt),
                    call_metadata={"phase": "R011_P03_full_task_extract", "prompt": file_ref(task_prompt_path), "source_instance_ids": list(group), "raw_source_traces": {item: source_trace_refs[item] for item in group}, "selected_by_pairings": selected_groups[group]},
                )
                result = validate_extraction(call["json"], benchmark="terminal-bench", expected_granularity="task")
                record = {"kind": "live_manager_r011_task_extraction", "model_call_id": call["call_id"], "source_instance_ids": list(group), "selected_by": selected_groups[group], "raw_source_traces": {item: source_trace_refs[item] for item in group}, "result": result}
                write_json(output_path, record)
                if result["action"] == "generate":
                    candidates.append(_candidate(skill=result["skill"], source_ids=list(group), record=output_path, kind="task", model_call_id=call["call_id"]))
            except BaseException as error:
                record = {"kind": "r011_task_extraction_failure", "source_instance_ids": list(group), "selected_by": selected_groups[group], **call_failure(error)}
                failures.append({"phase": "task_extract", **record})
                write_json(output_path, record)

        # The event portion is in fixed source order.  Valid old R009 results
        # remain references; the six invalid records receive exactly the R011
        # retry/repair allowed by the contract.
        for ordinal, source_id in enumerate(FULL_TEXT_SOURCE_IDS, start=1):
            r009_path, r009_record = _r009_event_record(args.r009_run, source_id)
            output_path = args.run_dir / "extraction" / "event" / f"{ordinal:03d}-{source_id}.json"
            if r009_record.get("kind") == "live_manager_r009_event_extraction_d04":
                result = r009_record.get("result")
                record = {"kind": "r011_referenced_valid_r009_event_result", "source_instance_ids": [source_id], "r009_record": file_ref(r009_path), "result": result}
                write_json(output_path, record)
                if isinstance(result, dict) and result.get("action") == "generate" and isinstance(result.get("skill"), dict):
                    candidates.append(_candidate(skill=result["skill"], source_ids=[source_id], record=output_path, kind="event_r009_reference", model_call_id=r009_record.get("model_call_id")))
                continue
            try:
                if source_id == "build-pmars":
                    call = manager.call_json(
                        purpose="r011_event_length_retry:build-pmars",
                        messages=event_extraction_with_evidence_messages(raw_traces[source_id], runtime_prompt=event_prompt, prior_event_ids=[]),
                        retry_of="r009_event_extract:build-pmars",
                        call_metadata={"phase": "R011_D04_length_retry", "runtime_prompt": event_prompt_ref, "raw_source_trace": source_trace_refs[source_id], "prior_r009_record": file_ref(r009_path)},
                    )
                    result = validate_event_extraction_with_evidence(call["json"], raw_traces[source_id], benchmark="terminal-bench")
                    record = {"kind": "live_manager_r011_event_length_retry", "model_call_id": call["call_id"], "source_instance_ids": [source_id], "prior_r009_record": file_ref(r009_path), "result": result}
                elif source_id in R009_REPAIR_SOURCE_IDS:
                    ledger_calls = read_json(args.ledger).get("calls", [])
                    match = next((item for item in ledger_calls if item.get("run_dir") == str(args.r009_run) and item.get("purpose") == f"r009_event_extract:{source_id}"), None)
                    if not isinstance(match, dict) or not isinstance(match.get("response_path"), str):
                        raise ValueError(f"cannot locate original R009 model output for {source_id}")
                    original_response = Path(match["response_path"])
                    original_output = _response_json(original_response)
                    validator_error = str(r009_record.get("error", ""))
                    if not validator_error:
                        raise ValueError(f"R009 failure has no exact validator error for {source_id}")
                    call = manager.call_json(
                        purpose=f"r011_event_evidence_repair:{source_id}",
                        messages=event_evidence_repair_messages(raw_traces[source_id], repair_prompt=repair_prompt, original_model_output=original_output, validator_error=validator_error),
                        retry_of=f"r009_event_extract:{source_id}",
                        call_metadata={"phase": "R011_D04_evidence_only_repair", "repair_prompt": file_ref(repair_prompt_path), "raw_source_trace": source_trace_refs[source_id], "prior_r009_record": file_ref(r009_path), "original_response": file_ref(original_response), "exact_validator_error": validator_error},
                    )
                    result = validate_event_evidence_repair(call["json"], raw_traces[source_id], original_model_output=original_output, benchmark="terminal-bench")
                    record = {"kind": "live_manager_r011_event_evidence_repair", "model_call_id": call["call_id"], "source_instance_ids": [source_id], "prior_r009_record": file_ref(r009_path), "original_response": file_ref(original_response), "original_model_output": original_output, "exact_validator_error": validator_error, "result": result}
                else:
                    raise ValueError(f"unexpected R009 invalid event source {source_id}")
                write_json(output_path, record)
                if result.get("action") == "generate":
                    candidates.append(_candidate(skill=result["skill"], source_ids=[source_id], record=output_path, kind="event_r011_repair_or_retry", model_call_id=call["call_id"]))
            except BaseException as error:
                record = {"kind": "r011_event_retry_or_repair_failure", "source_instance_ids": [source_id], "prior_r009_record": file_ref(r009_path), **call_failure(error)}
                failures.append({"phase": "event_repair_or_retry", **record})
                write_json(output_path, record)

        candidate_manifest = {"kind": "r011_common_candidate_manifest", "order": "task-group lexical then event source lexical", "candidates": candidates, "candidate_count": len(candidates), "preserved_r009_event_failures": [file_ref(_r009_event_record(args.r009_run, item)[0]) for item in ["build-pmars", *sorted(R009_REPAIR_SOURCE_IDS)]]}
        candidate_manifest_path = args.run_dir / "candidates" / "common-candidates.json"
        write_json(candidate_manifest_path, candidate_manifest)
        grouped = group_exact_candidates(candidates)
        arm_b = direct_candidate_bank(benchmark="terminal-bench", grouped_candidates=grouped, operation_prefix="r011-b-direct")
        arm_b.save(args.run_dir / "bank_snapshots" / "arm-b-direct-candidates.json")

        # C is an independent fresh Fig.9 sequence.  We intentionally do not
        # map old decisions onto different skill IDs or an altered retrieval
        # set; every decision below is either newly live or explicitly failed.
        arm_c = SkillBank.empty("terminal-bench")
        arm_c_path = args.run_dir / "bank_snapshots" / "arm-c-maintained.json"
        arm_c.save(arm_c_path)
        maintenance_operations = 0
        for ordinal, candidate in enumerate(candidates, start=1):
            retrieved, retrieval = same_granularity_top5(arm_c, candidate["skill"], encoder)
            retrieval_path = args.run_dir / "retrieval" / "maintenance" / f"{ordinal:03d}-{candidate['kind']}.json"
            write_json(retrieval_path, {**retrieval, "encoder": encoder_fingerprint, "candidate_record": candidate["candidate_record"]})
            operation_path = args.run_dir / "operations" / f"maintenance-{ordinal:03d}.json"
            try:
                call = manager.call_json(
                    purpose=f"r011_maintenance:{ordinal:03d}:{candidate['kind']}",
                    messages=maintenance_messages(candidate["skill"], retrieved, paper_prompt=maintenance_prompt),
                    call_metadata={"phase": "R011_P07_arm_c_maintenance", "prompt": file_ref(maintenance_prompt_path), "candidate": candidate, "retrieval": file_ref(retrieval_path), "bank_snapshot_before": arm_c.snapshot(), "old_r009_maintenance_reuse": "not claimed: R011 constructs an independent ordered C bank"},
                )
                decision = validate_maintenance(call["json"], candidate=candidate["skill"], retrieved_skill_ids={item["skill_id"] for item in retrieved})
                operation = arm_c.apply_and_save(arm_c_path, operation_id=f"r011-maintenance-{ordinal:03d}-{call['call_id']}", decision=decision["action"], candidate=decision.get("skill", candidate["skill"]), source_instance_ids=candidate["source_instance_ids"], evidence={"candidate": candidate, "maintenance_model_call_id": call["call_id"], "retrieval": file_ref(retrieval_path)}, merge_target_id=decision.get("merge_target_skill_id"))
                maintenance_operations += 1
                write_json(operation_path, {"kind": "live_manager_r011_maintenance", "candidate": candidate, "model_call_id": call["call_id"], "model_decision": decision, "operation": operation, "bank_snapshot_after": arm_c.snapshot()})
            except BaseException as error:
                record = {"kind": "r011_maintenance_failure", "candidate": candidate, **call_failure(error)}
                failures.append({"phase": "maintenance", **record})
                write_json(operation_path, record)
        arm_c.save(args.run_dir / "bank_snapshots" / "final.json")
        write_json(args.run_dir / "run-status.json", {"status": "completed", "finished_at_utc": utc_now(), "manager_calls_created_in_run": manager.call_count, "selected_task_group_count": len(selected_groups), "candidate_count": len(candidates), "arm_b_exact_deduplicated_candidate_count": len(grouped), "arm_c_maintenance_operation_count": maintenance_operations, "recorded_failure_count": len(failures), "recorded_failures": failures, "note": "Completion means the R011 fixed calibration schedule reached every allowed stage. It does not claim candidate quality, live solver injection, or official task success."})
    except BaseException as error:
        write_json(args.run_dir / "run-status.json", {"status": "aborted", "failed_at_utc": utc_now(), "error_type": type(error).__name__, "error": str(error)})
        raise


if __name__ == "__main__":
    main()
