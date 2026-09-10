"""Derive R010's independent B/C banks from preserved R009 candidates.

This is an offline derivation, never a replacement for the historical R009
run.  It reuses a Fig.9 response only when removing R009's erroneous
maintenance source filter leaves the complete prompt byte-for-byte unchanged.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from codeskill_rebuild.arm_banks import (
    direct_candidate_bank,
    exact_skill_schema,
    group_exact_candidates,
    overlapping_source_skill_ids,
)
from codeskill_rebuild.bank import BankError, SkillBank
from codeskill_rebuild.pipeline import maintenance_messages
from codeskill_rebuild.types import canonical_json, contract_from_files, read_json, sha256_file, sha256_text, utc_now, write_contract_snapshot, write_json


def file_ref(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": sha256_file(path)}


def _git(args: list[str]) -> str:
    completed = subprocess.run(["git", *args], check=True, capture_output=True, text=True)
    return completed.stdout


def git_provenance(run_dir: Path, *, relevant_code_paths: list[Path]) -> dict[str, Any]:
    head = _git(["rev-parse", "HEAD"]).strip()
    dirty_paths = _git(["status", "--porcelain=v1"])
    dirty = bool(dirty_paths.strip())
    result: dict[str, Any] = {"head": head, "dirty": dirty}
    if dirty:
        patch = _git(["diff", "--binary", "HEAD"])
        patch_path = run_dir / "code" / "working-tree.patch"
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path.write_text(patch, encoding="utf-8")
        status_path = run_dir / "code" / "working-tree-status.txt"
        status_path.write_text(dirty_paths, encoding="utf-8")
        result["working_tree_patch"] = file_ref(patch_path)
        result["working_tree_status"] = file_ref(status_path)
        project_root = Path.cwd().resolve()
        snapshot_paths = {path.resolve() for path in relevant_code_paths if path.exists() and path.is_file()}
        untracked_paths: list[Path] = []
        for line in dirty_paths.splitlines():
            if not line.startswith("?? "):
                continue
            path = (project_root / line[3:]).resolve()
            if path.is_file():
                untracked_paths.append(path)
                snapshot_paths.add(path)
        snapshots: list[dict[str, Any]] = []
        for source_path in sorted(snapshot_paths):
            try:
                relative = source_path.relative_to(project_root)
            except ValueError:
                relative = Path("external") / source_path.name
            copied = run_dir / "code" / "source-snapshot" / relative
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, copied)
            snapshots.append({"source_path": str(relative), "snapshot": file_ref(copied), "source_sha256": sha256_file(source_path)})
        result["complete_relevant_source_snapshot"] = snapshots
        result["untracked_source_paths"] = [str(path.relative_to(project_root)) for path in sorted(untracked_paths)]
    return result


def _state_bank(archived: SkillBank, state_sha256: str) -> SkillBank:
    state = next((item for item in archived.states or [] if item.get("state_sha256") == state_sha256), None)
    if not isinstance(state, dict):
        raise BankError(f"Archived bank has no state {state_sha256}")
    return SkillBank(
        benchmark=archived.benchmark,
        skills=state["skills"],
        operations=[],
        sequence=int(state["sequence"]),
        states=[state],
    )


def _candidate_records(r009_run: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((r009_run / "extraction").glob("*/*.json")):
        value = read_json(path)
        result = value.get("result")
        if not isinstance(result, dict) or result.get("action") != "generate":
            continue
        sources = value.get("source_instance_ids")
        if not isinstance(sources, list) or not sources:
            continue
        records.append(
            {
                "skill": result.get("skill"),
                "source_instance_ids": sources,
                "source_instance_ids_raw": sources,
                "candidate_record": file_ref(path),
            }
        )
    return records


def _archived_retrieved(pre_bank: SkillBank, retrieval: dict[str, Any]) -> list[dict[str, Any]]:
    ids = [item.get("skill_id") for item in retrieval.get("ranked", []) if isinstance(item, dict)]
    if not all(isinstance(value, str) for value in ids):
        raise BankError("archived maintenance retrieval has an invalid skill id")
    by_id = {skill.get("skill_id"): skill for skill in pre_bank.skills if skill.get("status") == "active"}
    missing = [value for value in ids if value not in by_id]
    if missing:
        raise BankError(f"archived maintenance retrieval refers to absent skills: {missing}")
    return [by_id[value] for value in ids]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--r009-run", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--prompts-root", type=Path, required=True)
    args = parser.parse_args()
    if args.run_dir.exists():
        raise FileExistsError(args.run_dir)
    r009_status = read_json(args.r009_run / "run-status.json")
    if r009_status.get("status") != "completed":
        raise ValueError("R010 derivation requires a completed archived R009 run")
    contract = contract_from_files(args.spec, args.decisions)
    if contract["version"] != "v0.10":
        raise ValueError(f"R010 derivation requires v0.10 contract, found {contract['version']}")

    args.run_dir.mkdir(parents=True)
    current_script = Path(__file__).resolve()
    arm_banks_path = Path(__import__("codeskill_rebuild.arm_banks", fromlist=["__file__"]).__file__).resolve()
    bank_path = Path(__import__("codeskill_rebuild.bank", fromlist=["__file__"]).__file__).resolve()
    pipeline_path = Path(__import__("codeskill_rebuild.pipeline", fromlist=["__file__"]).__file__).resolve()
    provenance = git_provenance(
        args.run_dir,
        relevant_code_paths=[
            current_script,
            arm_banks_path,
            bank_path,
            pipeline_path,
            Path("scripts/run_m2_common_bank_r009.py"),
            Path("tests/test_arm_banks.py"),
        ],
    )
    snapshot = write_contract_snapshot(args.run_dir, args.spec, args.decisions, contract)
    prompt_path = args.prompts_root / "paper" / "fig09_maintenance.md"
    maintenance_prompt = prompt_path.read_text(encoding="utf-8")
    records = _candidate_records(args.r009_run)
    grouped = group_exact_candidates(records)
    b_bank = direct_candidate_bank(benchmark="terminal-bench", grouped_candidates=grouped)
    b_path = args.run_dir / "bank_snapshots" / "arm-b-direct-candidates.json"
    b_bank.save(b_path)
    write_json(args.run_dir / "candidates" / "exact-deduplicated.json", {"kind": "r010_shared_candidate_list", "records": records, "exact_groups": grouped})

    archived = SkillBank.load(args.r009_run / "bank_snapshots" / "final.json")
    group_by_schema = {canonical_json(exact_skill_schema(group["skill"])): group for group in grouped}
    equivalence: list[dict[str, Any]] = []
    for ordinal, operation_path in enumerate(sorted((args.r009_run / "operations").glob("maintenance-*.json")), start=1):
        operation_document = read_json(operation_path)
        archived_operation = operation_document.get("operation")
        if not isinstance(archived_operation, dict):
            continue
        candidate = exact_skill_schema(archived_operation.get("candidate"))
        group = group_by_schema.get(canonical_json(candidate))
        if group is None:
            raise BankError(f"maintenance candidate is absent from the shared candidate list: {operation_path}")
        pre_bank = _state_bank(archived, str(archived_operation.get("before_snapshot_sha256")))
        legacy_removed = overlapping_source_skill_ids(
            pre_bank,
            candidate=candidate,
            source_instance_ids=group["source_instance_ids"],
        )
        retrieval_ref = archived_operation.get("evidence", {}).get("retrieval")
        if not isinstance(retrieval_ref, dict) or not isinstance(retrieval_ref.get("path"), str):
            raise BankError("archived maintenance operation has no retrieval reference")
        retrieval = read_json(Path(retrieval_ref["path"]))
        retrieved = _archived_retrieved(pre_bank, retrieval)
        request_path = args.r009_run / "model_calls" / str(archived_operation["evidence"]["maintenance_model_call_id"]) / "request.json"
        archived_request = read_json(request_path)
        rebuilt_messages = maintenance_messages(candidate, retrieved, paper_prompt=maintenance_prompt)
        archived_messages = archived_request.get("request", {}).get("messages")
        messages_match = isinstance(archived_messages, list) and canonical_json(rebuilt_messages) == canonical_json(archived_messages)
        reusable = not legacy_removed and messages_match
        evidence = {
            "ordinal": ordinal,
            "archived_operation": file_ref(operation_path),
            "archived_request": file_ref(request_path),
            "archived_retrieval": file_ref(Path(retrieval_ref["path"])),
            "legacy_source_filter_would_remove_skill_ids": legacy_removed,
            "current_fig09_messages_sha256": sha256_text(canonical_json(rebuilt_messages)),
            "archived_fig09_messages_sha256": sha256_text(canonical_json(archived_messages)) if isinstance(archived_messages, list) else None,
            "messages_match": messages_match,
            "reuse_eligible_without_new_model_call": reusable,
        }
        equivalence.append(evidence)
    c_path = args.run_dir / "bank_snapshots" / "arm-c-maintained.json"
    all_reused = bool(equivalence) and all(item["reuse_eligible_without_new_model_call"] for item in equivalence)
    if all_reused:
        # C remains a separate file, but preserves every archived skill ID,
        # version, journal entry, and state hash that produced the verified
        # requests. R010 adds only an external derivation/equivalence layer.
        c_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.r009_run / "bank_snapshots" / "final.json", c_path)
    write_json(args.run_dir / "equivalence" / "maintenance-inputs.json", {"kind": "r010_maintenance_source_filter_equivalence", "checks": equivalence})
    write_json(
        args.run_dir / "manifest.json",
        {
            "schema_version": 1,
            "kind": "r010_offline_shared_candidates_independent_arm_banks",
            "created_at_utc": utc_now(),
            "historical_source": True,
            "fixture": False,
            "live_manager": False,
            "contract": contract,
            "contract_snapshot": snapshot,
            "git": provenance,
            "archived_r009_run": file_ref(args.r009_run / "manifest.json"),
            "archived_r009_status": file_ref(args.r009_run / "run-status.json"),
            "source_code": {
                "script": file_ref(current_script),
                "arm_banks": file_ref(arm_banks_path),
                "bank": file_ref(bank_path),
                "pipeline": file_ref(pipeline_path),
                "maintenance_prompt": file_ref(prompt_path),
            },
            "candidate_count": len(records),
            "exact_deduplicated_candidate_count": len(grouped),
            "arm_b_direct_candidate_bank": file_ref(b_path),
            "arm_c_maintained_bank": file_ref(c_path) if all_reused else None,
            "arm_c_provenance": {
                "mode": "independent_deepcopy_of_verified_archived_r009_bank" if all_reused else "not_created_pending_changed_maintenance_inputs",
                "archived_source": file_ref(args.r009_run / "bank_snapshots" / "final.json"),
                "preserves_archived_skill_ids_versions_operations_and_state_hashes": all_reused,
            },
            "maintenance_reuse_check": file_ref(args.run_dir / "equivalence" / "maintenance-inputs.json"),
            "new_manager_calls": 0,
            "r009_recorded_failure_count": r009_status.get("recorded_failure_count"),
            "note": "This derivation separates common candidates from B/C bank state. It preserves R009's task-group absence and event failures and does not claim complete M2, M3 injection, or official success.",
        },
    )
    write_json(
        args.run_dir / "run-status.json",
        {
            "status": "completed" if all_reused else "requires_new_maintenance_calls",
            "finished_at_utc": utc_now(),
            "candidate_count": len(records),
            "exact_deduplicated_candidate_count": len(grouped),
            "reused_maintenance_operation_count": len(equivalence) if all_reused else 0,
            "maintenance_equivalence_check_count": len(equivalence),
            "new_manager_calls": 0,
            "preserved_r009_failure_count": r009_status.get("recorded_failure_count"),
        },
    )


if __name__ == "__main__":
    main()
