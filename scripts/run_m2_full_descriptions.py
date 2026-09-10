"""R005 common, projected D01 descriptions for every fixed textual source."""

from __future__ import annotations

import argparse
from pathlib import Path

from codeskill_rebuild.manager import ManagerClient, ManagerProfile, ServerMessageTokenCounter
from codeskill_rebuild.manager_projection import PROJECTION_VERSION, project_trace_for_manager
from codeskill_rebuild.pipeline import description_messages, validate_description
from codeskill_rebuild.retrieval import MiniLMEncoder, cosine
from codeskill_rebuild.types import canonical_instance_id, contract_from_files, read_json, sha256_file, utc_now, write_contract_snapshot, write_json


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--source-manifest", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--decisions", required=True, type=Path)
    parser.add_argument("--prompts-root", required=True, type=Path)
    args = parser.parse_args()
    if args.run_dir.exists():
        raise FileExistsError(args.run_dir)
    source_manifest = read_json(args.source_manifest)
    if source_manifest.get("source_pool_profile") != "r005_full_text_pool":
        raise ValueError("full descriptions require the R005 fixed full text-source manifest")
    sources = source_manifest.get("sources")
    if not isinstance(sources, list):
        raise ValueError("source manifest has no sources list")
    source_by_id = {canonical_instance_id(str(item.get("canonical_instance_id", ""))): item for item in sources if isinstance(item, dict)}
    if list(source_by_id) != FULL_TEXT_SOURCE_IDS or len(source_by_id) != len(FULL_TEXT_SOURCE_IDS):
        raise ValueError("R005 source manifest must contain every fixed text source in canonical order")
    if not all(item.get("text_manager_eligible") and item.get("raw_trace_kind") == "historical_baseline" for item in source_by_id.values()):
        raise ValueError("R005 descriptions only accept eligible historical baseline text sources")
    service = read_json(args.config)["services"]["deepseek_flash"]
    profile = ManagerProfile(base_url=service["base_url"], model=service["model_id"], max_total_calls=60)
    contract = contract_from_files(args.spec, args.decisions)
    prompt_path = args.prompts_root / "custom" / "m2_description.md"
    prompt = prompt_path.read_text(encoding="utf-8")
    args.run_dir.mkdir(parents=True)
    contract_snapshot = write_contract_snapshot(args.run_dir, args.spec, args.decisions, contract)
    write_json(
        args.run_dir / "manifest.json",
        {
            "schema_version": 1,
            "kind": "m2_r005_full_text_source_descriptions",
            "created_at_utc": utc_now(),
            "historical_source": True,
            "fixture": False,
            "live_manager": True,
            "contract": contract,
            "contract_snapshot": contract_snapshot,
            "source_manifest": file_ref(args.source_manifest),
            "source_ids": FULL_TEXT_SOURCE_IDS,
            "profile": profile.__dict__,
            "prompt": file_ref(prompt_path),
            "projection_version": PROJECTION_VERSION,
            "projection_implementation": file_ref(Path(project_trace_for_manager.__code__.co_filename)),
            "generation_budget": {"global_limit": 60, "planned_new_completions": 10, "repair_calls": 0, "note": "each description uses the same prompt; invalid calls are preserved, not silently substituted"},
        },
    )
    manager = ManagerClient(profile, args.run_dir, contract, args.ledger, ServerMessageTokenCounter(profile.base_url))
    write_json(args.run_dir / "run-status.json", {"status": "running", "started_at_utc": utc_now()})
    try:
        descriptions: dict[str, dict] = {}
        vectors: dict[str, list[float]] = {}
        projection_refs: dict[str, dict[str, str]] = {}
        encoder = MiniLMEncoder()
        encoder_fingerprint = encoder.load()
        for source_id in FULL_TEXT_SOURCE_IDS:
            source = source_by_id[source_id]
            raw_trace_path = Path(source["normalized_path"])
            raw_trace = read_json(raw_trace_path)
            if canonical_instance_id(raw_trace["source"]["canonical_instance_id"]) != source_id:
                raise ValueError(f"source identity mismatch for {source_id}")
            projection = project_trace_for_manager(raw_trace)
            mapping_path = args.run_dir / "projections" / f"{source_id}.json"
            write_json(mapping_path, projection["mapping"])
            projection_ref = file_ref(mapping_path)
            projection_refs[source_id] = projection_ref
            call = manager.call_json(
                purpose=f"m2_r005_description:{source_id}",
                messages=description_messages(projection["manager_trace"], custom_prompt=prompt),
                call_metadata={"phase": "R005_D01_full_pool_description", "source": source, "source_trace": file_ref(raw_trace_path), "projection_mapping": projection_ref, "prompt": file_ref(prompt_path)},
            )
            description = validate_description(call["json"], raw_trace)
            descriptions[source_id] = description
            write_json(
                args.run_dir / "descriptions" / f"{source_id}.json",
                {
                    "kind": "live_manager_r005_description",
                    "historical_source": True,
                    "source": source,
                    "source_trace": file_ref(raw_trace_path),
                    "projection_mapping": projection_ref,
                    "model_call_id": call["call_id"],
                    "description": description,
                    "source_step_ids_available": [step["source_entry_id"] for step in raw_trace["steps"]],
                },
            )
            vectors[source_id], index_record = encoder.index_description(description)
            write_json(args.run_dir / "retrieval" / "description-index" / f"{source_id}.json", {"encoder": encoder_fingerprint, "source_id": source_id, "index_record": index_record})
        ranked_pairs = []
        for left_index, left in enumerate(FULL_TEXT_SOURCE_IDS):
            for right in FULL_TEXT_SOURCE_IDS[left_index + 1 :]:
                ranked_pairs.append({"left": left, "right": right, "score": cosine(vectors[left], vectors[right])})
        ranked_pairs.sort(key=lambda item: (-item["score"], item["left"], item["right"]))
        write_json(
            args.run_dir / "retrieval" / "all-description-pairs.json",
            {"kind": "r005_full_pool_minilm_pair_inventory", "encoder": encoder_fingerprint, "source_ids": FULL_TEXT_SOURCE_IDS, "pair_count": len(ranked_pairs), "pairs": ranked_pairs, "description_projection_mappings": projection_refs},
        )
        write_json(args.run_dir / "run-status.json", {"status": "completed", "finished_at_utc": utc_now(), "description_count": len(descriptions), "model_calls": len(descriptions), "next_stage": "pair/task/event/maintenance selection requires an explicit common candidate plan; no task group was silently chosen"})
    except BaseException as error:
        write_json(args.run_dir / "run-status.json", {"status": "failed", "failed_at_utc": utc_now(), "error_type": type(error).__name__, "error": str(error)})
        raise


if __name__ == "__main__":
    main()
