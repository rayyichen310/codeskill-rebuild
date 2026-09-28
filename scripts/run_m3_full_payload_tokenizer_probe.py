#!/usr/bin/env python3
"""Run one bounded, synthetic full-payload M3 tokenizer/usage probe."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from codeskill_rebuild.solver_probe import FullPayloadUsageProbe, PayloadProbeError, PayloadProbeProfile, ServerPayloadTokenCounter
from codeskill_rebuild.types import contract_from_files, sha256_file, utc_now, write_contract_snapshot, write_json


def file_ref(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": sha256_file(path)}


def capture_source_snapshot(*, project_root: Path, run_dir: Path, relative_paths: list[str]) -> dict[str, Any]:
    code = run_dir / "code"
    code.mkdir(parents=True, exist_ok=True)
    try:
        status = subprocess.run(["git", "status", "--porcelain=v1"], cwd=project_root, check=True, text=True, capture_output=True).stdout
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project_root, check=True, text=True, capture_output=True).stdout.strip()
        patch = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=project_root, check=True, text=True, capture_output=True).stdout
        provenance = {"kind": "git_checkout", "head": head, "dirty": bool(status)}
    except (OSError, subprocess.CalledProcessError) as error:
        # A deployment may intentionally be a source-only Git archive, so it
        # cannot manufacture a Git checkout merely to run a tokenizer probe.
        # Preserve that limitation and bind the probe to a caller-supplied
        # immutable source revision plus the copied files below.
        source_revision = os.environ.get("CODESKILL_SOURCE_REVISION", "").strip()
        if not source_revision:
            raise RuntimeError(
                "source-only deployment needs CODESKILL_SOURCE_REVISION; refusing to label an archive as a Git checkout"
            ) from error
        status = ""
        patch = ""
        head = f"archive:{source_revision}"
        provenance = {
            "kind": "source_only_archive",
            "source_revision": source_revision,
            "git_metadata_unavailable": True,
            "git_error_type": type(error).__name__,
            "git_error": str(error),
        }
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
        "provenance": provenance,
        "working_tree_status": file_ref(code / "working-tree-status.txt"),
        "working_tree_patch": file_ref(code / "working-tree.patch"),
        "complete_relevant_source_snapshot": snapshots,
        "untracked_source_paths": [line[3:] for line in status.splitlines() if line.startswith("?? ")],
    }


def get_json(url: str) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=15) as response:
        raw = response.read().decode("utf-8")
        return {"http_status": response.status, "body": json.loads(raw) if raw.strip() else ""}


def synthetic_payload(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": "Synthetic M3 tokenization probe only. Do not access tools or solve a benchmark task."},
            {"role": "user", "content": "Return a short acknowledgement of this synthetic input."},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "probe_echo",
                    "description": "Synthetic schema used only to verify full solver-payload template accounting.",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string", "description": "Synthetic probe text."}},
                        "required": ["value"],
                    },
                },
            }
        ],
        "tool_choice": "auto",
        "temperature": 1.0,
        "top_p": 0.95,
        "reasoning_effort": "high",
        "max_tokens": 256,
        "stream": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    args = parser.parse_args()
    if args.run_dir.exists():
        raise FileExistsError(args.run_dir)
    project_root = Path(__file__).resolve().parents[1]
    contract = contract_from_files(args.spec, args.decisions)
    if contract.get("version") != "v0.12":
        raise ValueError(f"M3 profile 001 requires the current v0.12 contract; found {contract}")
    service = json.loads(args.config.read_text(encoding="utf-8"))["services"]["deepseek_flash"]
    payload = synthetic_payload(service["model_id"])
    args.run_dir.mkdir(parents=True)
    snapshot = capture_source_snapshot(
        project_root=project_root,
        run_dir=args.run_dir,
        relative_paths=[
            "scripts/run_m3_full_payload_tokenizer_probe.py",
            "src/codeskill_rebuild/solver_probe.py",
            "src/codeskill_rebuild/types.py",
            "configs/model-endpoints.json",
            "docs/archive/M3_DEVELOPMENT_PROFILE.md",
        ],
    )
    contract_snapshot = write_contract_snapshot(args.run_dir, args.spec, args.decisions, contract)
    server_metadata = {
        "models": get_json(service["base_url"].removesuffix("/v1") + "/v1/models"),
        "server_info": get_json(service["base_url"].removesuffix("/v1") + "/get_server_info"),
    }
    profile = PayloadProbeProfile(base_url=service["base_url"], model=service["model_id"], timeout_seconds=60, max_output_tokens=256, max_total_calls=100)
    manifest = {
        "schema_version": 1,
        "kind": "m3_full_payload_tools_tokenizer_probe",
        "created_at_utc": utc_now(),
        "historical": False,
        "fixture": False,
        "synthetic": True,
        "trace_content": "none",
        "contract": contract,
        "contract_snapshot": contract_snapshot,
        "git": snapshot,
        "profile": profile.__dict__,
        "m3_profile": file_ref(args.profile),
        "service_metadata": server_metadata,
        "payload_scope": "complete OpenAI chat payload including messages, tools, tool_choice, sampling, reasoning, output cap, and stream flag; /tokenize adds only add_generation_prompt=true",
        "limits": {"max_new_completion_calls": 1, "max_output_tokens": 256, "timeout_seconds": 60, "global_ledger_limit": 100},
        "success_condition": "same payload's server /tokenize count equals completion usage.prompt_tokens",
        "limitation": "This synthetic schema probe validates complete-payload template accounting only; it does not establish actual OpenClaw adapter wiring or native compaction.",
    }
    write_json(args.run_dir / "manifest.json", manifest)
    write_json(args.run_dir / "run-status.json", {"status": "running", "started_at_utc": utc_now()})
    probe = FullPayloadUsageProbe(
        profile=profile,
        run_dir=args.run_dir,
        ledger_path=args.ledger,
        contract=contract,
        exact_token_counter=ServerPayloadTokenCounter(profile.base_url, timeout_seconds=profile.timeout_seconds),
    )
    try:
        result = probe.run(purpose="m3_full_payload_tools_tokenizer_probe", payload=deepcopy(payload))
    except BaseException as error:
        write_json(
            args.run_dir / "run-status.json",
            {
                "status": "failed",
                "failed_at_utc": utc_now(),
                "error_type": type(error).__name__,
                "error": str(error),
                "model_completions_reserved": 1 if (args.run_dir / "model_calls" / "call-0001" / "request.json").exists() else 0,
                "note": "Preserved M3 prerequisite failure; no solver trial may start from this result.",
            },
        )
        raise
    write_json(
        args.run_dir / "run-status.json",
        {
            "status": "completed",
            "finished_at_utc": utc_now(),
            "model_completions_reserved": 1,
            "model_completions_http_completed": 1,
            "call_id": result["call_id"],
            "tokenizer_prompt_token_match": result["response"]["tokenizer_prompt_token_comparison"],
            "note": "Synthetic full-payload tokenizer prerequisite result only; no benchmark trace, tool execution, solver trial, or native compaction claim.",
        },
    )


if __name__ == "__main__":
    main()
