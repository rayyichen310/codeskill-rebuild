"""Import a bounded, hash-pinned textual baseline subset for the M2 pilot."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from codeskill_rebuild.traces import TraceImportError, normalize_openclaw_trial, write_normalized_trace
from codeskill_rebuild.types import canonical_instance_id, contract_from_files, sha256_file, utc_now, write_contract_snapshot, write_json


DEFAULT_SOURCE_IDS = [
    "build-pmars",
    "cancel-async-tasks",
    "fix-git",
    "git-leak-recovery",
]

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


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--source-id", action="append", dest="source_ids")
    parser.add_argument("--profile", choices=("r001_pilot", "r005_full_text_pool"), default="r001_pilot")
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--decisions", required=True, type=Path)
    args = parser.parse_args()
    if args.profile == "r005_full_text_pool" and args.source_ids:
        raise ValueError("R005 imports the fixed complete text-source pool; do not hand-select a subset")
    source_ids = args.source_ids or (FULL_TEXT_SOURCE_IDS if args.profile == "r005_full_text_pool" else DEFAULT_SOURCE_IDS)
    if args.profile == "r001_pilot" and not 1 <= len(source_ids) <= 4:
        raise ValueError("R001 M2 source pilot permits 1–4 sources")
    if args.profile == "r005_full_text_pool" and source_ids != FULL_TEXT_SOURCE_IDS:
        raise ValueError("R005 source pool must contain each fixed text source in canonical order")
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("source IDs must be unique")
    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    baseline_rows = {}
    for row in audit.get("rows", []):
        if row.get("arm") != "baseline":
            continue
        raw_id = row.get("instance_id")
        if isinstance(raw_id, str):
            baseline_rows[canonical_instance_id(raw_id)] = row

    selected_rows = []
    for source_id in source_ids:
        row = baseline_rows.get(canonical_instance_id(source_id))
        if row is None:
            raise ValueError(f"Requested source is not an audited baseline: {source_id}")
        if row.get("eligibility") != "structurally_usable_raw_session":
            raise ValueError(f"Requested source is not structurally usable: {source_id}")
        selected_rows.append(row)

    normalized_dir = args.run_dir / "trajectories" / "normalized"
    manifest_sources = []
    for row in selected_rows:
        artifacts = row.get("artifacts", {})
        session = artifacts.get("agent/openclaw.session.jsonl", {})
        session_hash = session.get("sha256")
        if not isinstance(session_hash, str):
            raise ValueError(f"Audit row has no session hash: {row.get('instance_id')}")
        trace = normalize_openclaw_trial(Path(row["trial_path"]), expected_session_sha256=session_hash)
        if not trace["text_manager_eligible"]:
            raise TraceImportError(f"Text M2 refuses multimodal-pending source: {row['instance_id']}")
        raw_id = trace["source"]["official_task_name"]
        canonical_id = trace["source"]["canonical_instance_id"]
        if raw_id != row["instance_id"] or canonical_id != canonical_instance_id(row["instance_id"]):
            raise TraceImportError(f"Audit/config identity mismatch for {row['instance_id']}")
        target = normalized_dir / f"{canonical_id}.json"
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite normalized source: {target}")
        write_normalized_trace(trace, target)
        manifest_sources.append(
            {
                "canonical_instance_id": canonical_id,
                "official_task_name": raw_id,
                "trial_path": row["trial_path"],
                "session_path": trace["source"]["session_path"],
                "session_sha256": session_hash,
                "normalized_path": str(target),
                "normalized_sha256": sha256_file(target),
                "official_reward": trace["outcome"].get("official_reward"),
                "text_manager_eligible": trace["text_manager_eligible"],
                "raw_trace_kind": "historical_baseline",
            }
        )
    contract = contract_from_files(args.spec, args.decisions)
    contract_snapshot = write_contract_snapshot(args.run_dir, args.spec, args.decisions, contract)
    write_json(
        args.run_dir / "source-manifest.json",
        {
            "schema_version": 1,
            "kind": "m2_source_import_manifest",
            "created_at_utc": utc_now(),
            "historical": True,
            "fixture": False,
            "live": False,
            "source_pool_profile": args.profile,
            "source_pool_size": len(source_ids),
            "audit": {"path": str(args.audit), "sha256": digest(args.audit)},
            "contract": contract,
            "contract_snapshot": contract_snapshot,
            "sources": manifest_sources,
            "excluded_from_this_text_pilot": {
                "code-from-image": "multimodal_pending",
                "polyglot-c-py": "no_agent_trace_agent_setup_timeout",
            },
        },
    )


if __name__ == "__main__":
    main()
