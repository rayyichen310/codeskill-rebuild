#!/usr/bin/env python3
"""Run the bounded R015 historical-thinking manager A/B.

This is intentionally a manager-only diagnostic.  It never invokes a solver,
publishes a bank, performs retrieval, or changes lifecycle/maintenance state.
The immutable manifest is written by ``prepare`` before either arm may call a
model.  ``run-arm`` verifies every frozen input and uses the production R015
manager boundary.  ``compare`` checks source/policy invariants and retained
request/response artifacts after both arms finish.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from codeskill_rebuild.manager_projection import (  # noqa: E402
    HISTORICAL_THINKING_POLICY_VERSION,
    project_historical_thinking,
    validate_historical_thinking_policy,
)
from codeskill_rebuild.pipeline import (  # noqa: E402
    task_candidate_merge_messages,
    validate_task_extraction_with_evidence,
)
from codeskill_rebuild.types import canonical_json, sha256_file, sha256_text  # noqa: E402
from scripts import run_r015_c_only_harbor_driver as driver  # noqa: E402
from scripts.legacy import r015_task_extraction as legacy_task  # noqa: E402


SCHEMA_VERSION = 1
ACCEPTED_BASE_COMMIT = "45228a0867c40f7a772a7fdaa9fe361f11b9ffed"
TRACE_NAMES = (
    "build-pmars",
    "fix-git",
    "git-leak-recovery",
    "schemelike-metacircular-eval",
)
PROMPTS = (
    "custom/r015_task_sop_candidate.md",
    "custom/r015_fig07_event_extraction_with_code_examples.md",
    "custom/r015_fig06_task_extraction_with_code_examples.md",
    "custom/m2_evidence_compaction.md",
)
IMPLEMENTATION_FILES = (
    "src/codeskill_rebuild/manager_projection.py",
    "scripts/run_r015_c_only_harbor_driver.py",
    "scripts/legacy/run_r015_thinking_ab.py",
    "scripts/legacy/r015_task_extraction.py",
    "configs/r015-c-only-coding.json",
)
R015_16K_PROFILE = "r015_thinking_ab_16k"


class ThinkingABError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ThinkingABError(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _git_output(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=True,
    )
    return result.stdout.strip()


def _trace_path(source_root: Path, task: str) -> Path:
    return (source_root / "round-1" / task / "official-harbor" / "trajectory-live.json").resolve()


def _trace_record(path: Path, task: str) -> dict[str, Any]:
    if not path.is_file():
        raise ThinkingABError(f"missing frozen trace: {path}")
    trace = _read_json(path)
    source = trace.get("source")
    if not isinstance(source, dict) or source.get("canonical_instance_id") != task:
        raise ThinkingABError(f"trace identity mismatch for {task}: {path}")
    steps = trace.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ThinkingABError(f"trace has no normalized steps: {path}")
    thinking_blocks = 0
    thinking_chars = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        for block in step.get("content", []):
            if isinstance(block, dict) and block.get("type") in {"thinking", "reasoning"} and isinstance(block.get("text"), str):
                thinking_blocks += 1
                thinking_chars += len(block["text"])
    return {
        "task_id": task,
        "path": str(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "source": deepcopy(source),
        "outcome": deepcopy(trace.get("outcome")),
        "step_count": len(steps),
        "thinking_block_count": thinking_blocks,
        "thinking_character_count": thinking_chars,
    }


def prepare(args: argparse.Namespace) -> int:
    output = Path(args.output).resolve()
    if output.exists():
        raise ThinkingABError(f"manifest already exists; refusing overwrite: {output}")
    source_root = Path(args.source_root).resolve()
    traces = {task: _trace_record(_trace_path(source_root, task), task) for task in TRACE_NAMES}
    prompts = {}
    for relative in PROMPTS:
        path = ROOT / "prompts" / relative
        prompts[relative] = {"path": str(path.resolve()), "sha256": sha256_file(path)}
    implementation = {}
    for relative in IMPLEMENTATION_FILES:
        path = ROOT / relative
        implementation[relative] = {"path": str(path.resolve()), "sha256": sha256_file(path)}
    baseline_manifest_path = getattr(args, "baseline_manifest", None)
    if baseline_manifest_path is not None:
        baseline = _read_json(Path(baseline_manifest_path))
        if set(baseline.get("traces", {})) != set(traces):
            raise ThinkingABError("baseline trace set differs")
        for task, trace in traces.items():
            if trace["sha256"] != baseline["traces"][task].get("sha256"):
                raise ThinkingABError(f"baseline source hash differs: {task}")
        for relative, prompt in prompts.items():
            if prompt["sha256"] != baseline.get("prompts", {}).get(relative, {}).get("sha256"):
                raise ThinkingABError(f"baseline prompt hash differs: {relative}")
    if args.profile is None:
        manager_profile = {
            "base_url": args.base_url.rstrip("/"),
            "model": args.model,
            "manager_context_tokens": 270000,
            "max_output_tokens": 8192,
            "safety_tokens": 4096,
            "temperature": 0.0,
            "reasoning_effort": "max",
            "timeout_seconds": 300,
            "input_allowance_tokens": 257712,
        }
    else:
        profile_path = Path(args.profile).resolve()
        manager_profile = _read_json(profile_path)
        expected = {
            "kind": "r015_thinking_ab_manager_profile",
            "output_budget_profile": R015_16K_PROFILE,
            "model": "deepseek-ai/DeepSeek-V4-Flash",
            "manager_context_tokens": 270000,
            "max_output_tokens": 16384,
            "safety_tokens": 4096,
            "temperature": 0.0,
            "reasoning_effort": "max",
            "timeout_seconds": 300,
            "summary_segment_max_tokens": 65536,
        }
        if manager_profile != expected or args.model != expected["model"]:
            raise ThinkingABError("experiment profile differs from the approved 16K settings")
        manager_profile = {
            **manager_profile,
            "base_url": args.base_url.rstrip("/"),
            "input_allowance_tokens": 249520,
            "profile_path": str(profile_path),
            "profile_sha256": sha256_file(profile_path),
        }
        implementation["configs/r015-thinking-ab-16k.json"] = {
            "path": str(profile_path), "sha256": sha256_file(profile_path),
        }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": "r015_historical_thinking_manager_ab_manifest",
        "created_at_utc": _utc_now(),
        "scope": {
            "round": 1,
            "manager_only": True,
            "solver_rerun": False,
            "formal_campaign": False,
            "bank_publication": False,
            "retrieval_or_maintenance_change": False,
        },
        "source_root": str(source_root),
        "traces": traces,
        "cases": [
            {"case_id": "s01-build-pmars-task", "kind": "task_candidate", "sources": ["build-pmars"]},
            {"case_id": "s01-build-pmars-event", "kind": "event", "sources": ["build-pmars"], "max_attempts": 3},
            {"case_id": "s07-fix-git-task", "kind": "task_candidate", "sources": ["fix-git"]},
            {"case_id": "s07-git-leak-recovery-task", "kind": "task_candidate", "sources": ["git-leak-recovery"]},
            {"case_id": "s07-fixed-source-group", "kind": "fixed_source_group_diagnostic", "sources": ["fix-git", "git-leak-recovery"], "natural_pairing_claim": False},
            {"case_id": "long-failure-repair-task", "kind": "task_candidate", "sources": ["schemelike-metacircular-eval"]},
            {"case_id": "long-failure-repair-event", "kind": "event", "sources": ["schemelike-metacircular-eval"], "max_attempts": 3},
        ],
        "long_case_selection": {
            "task_id": "schemelike-metacircular-eval",
            "reason": "same-round long trajectory containing an observed failed edit followed by a read-and-successful-repair sequence",
            "failed_edit_step_id": "ba88f22d-01ba-4c56-9d55-255939c0ada3",
            "successful_repair_result_step_id": "13bb81f0-40dc-41ae-9511-8f62b87d5e54",
        },
        "arms": {
            "keep": {"historical_thinking_policy": "keep"},
            "exclude": {"historical_thinking_policy": "exclude"},
        },
        "policy_version": HISTORICAL_THINKING_POLICY_VERSION,
        "manager_profile": manager_profile,
        "prompts": prompts,
        "implementation": implementation,
        "git": {
            "accepted_base_commit": ACCEPTED_BASE_COMMIT,
            "head": getattr(args, "code_head", None) or _git_output("rev-parse", "HEAD"),
            "branch": "isolated_export" if getattr(args, "code_head", None) else _git_output("branch", "--show-current"),
            "code_snapshot_sha256": getattr(args, "code_snapshot_sha256", None),
        },
    }
    if baseline_manifest_path is not None:
        baseline = _read_json(Path(baseline_manifest_path))
        if manifest["cases"] != baseline.get("cases"):
            raise ThinkingABError("baseline case selection differs")
        manifest["prior_manifest"] = {
            "path": str(Path(baseline_manifest_path).resolve()),
            "sha256": sha256_file(Path(baseline_manifest_path)),
        }
    _write_json(output, manifest)
    print(json.dumps({"manifest": str(output), "sha256": sha256_file(output)}, indent=2))
    return 0


def _verify_manifest(manifest_path: Path, manifest: dict[str, Any]) -> None:
    if manifest.get("kind") != "r015_historical_thinking_manager_ab_manifest":
        raise ThinkingABError("wrong manifest kind")
    if manifest.get("policy_version") != HISTORICAL_THINKING_POLICY_VERSION:
        raise ThinkingABError("historical thinking policy version drift")
    profile = manifest.get("manager_profile")
    if not isinstance(profile, dict):
        raise ThinkingABError("manager profile is missing")
    if profile.get("input_allowance_tokens") != (
        profile.get("manager_context_tokens", 0)
        - profile.get("max_output_tokens", 0)
        - profile.get("safety_tokens", 0)
    ):
        raise ThinkingABError("manager input allowance does not match the profile")
    if profile.get("output_budget_profile") == R015_16K_PROFILE:
        if (
            profile.get("max_output_tokens") != 16384
            or profile.get("manager_context_tokens") != 270000
            or profile.get("safety_tokens") != 4096
            or profile.get("summary_segment_max_tokens") != 65536
            or profile.get("reasoning_effort") != "max"
            or profile.get("temperature") != 0.0
            or profile.get("timeout_seconds") != 300
        ):
            raise ThinkingABError("16K experiment profile drift")
        profile_path = Path(profile["profile_path"])
        if sha256_file(profile_path) != profile.get("profile_sha256"):
            raise ThinkingABError("16K experiment profile file changed")
    for task, record in manifest["traces"].items():
        path = Path(record["path"])
        if sha256_file(path) != record["sha256"]:
            raise ThinkingABError(f"frozen trace changed: {task}")
    for group in ("prompts", "implementation"):
        for name, record in manifest[group].items():
            if sha256_file(Path(record["path"])) != record["sha256"]:
                raise ThinkingABError(f"frozen {group} input changed: {name}")
    if not manifest_path.is_file():
        raise ThinkingABError("manifest disappeared during verification")


def _trajectory_ref(record: dict[str, Any], trace: dict[str, Any]) -> dict[str, Any]:
    source = trace["source"]
    return {
        "path": record["path"],
        "sha256": record["sha256"],
        "round_id": 1,
        "trial_id": source.get("live_trial_id") or f"r1:C:{record['task_id']}",
        "session_id": source["session_id"],
        "complete": True,
    }


def _context(
    *, arm_root: Path, manifest: dict[str, Any], policy: str, task_id: str,
) -> tuple[dict[str, Any], Any]:
    task_root = arm_root / "tasks" / task_id
    task_root.mkdir(parents=True, exist_ok=False)
    profile = manifest["manager_profile"]
    config = _read_json(ROOT / "configs" / "r015-c-only-coding.json")
    context = {
        "artifact_root": str(task_root),
        "trial_id": f"r1:thinking-ab:{policy}:{task_id}",
        "task_id": task_id,
        "target": {
            "endpoint": profile["base_url"],
            "model_id": profile["model"],
            "context_tokens": profile["manager_context_tokens"],
            "reasoning_effort": profile["reasoning_effort"],
        },
        "upstream": {"endpoint": profile["base_url"]},
        "profile": config["retrieval_profile"],
        "trusted_manager_root": str((arm_root / "manager").resolve()),
        "driver_config": {
            "historical_thinking_policy": policy,
            "manager_ledger": str((arm_root / "manager-ledger.json").resolve()),
            "manager_output_budget_profile": profile.get("output_budget_profile", "default"),
        },
    }
    _manager, executor, context = driver._manager_context(context, output_path=task_root / "result.json")
    if (
        executor.manager.profile.max_output_tokens != profile["max_output_tokens"]
        or executor.manager.profile.manager_context_tokens != profile["manager_context_tokens"]
        or executor.manager.profile.safety_tokens != profile["safety_tokens"]
    ):
        raise ThinkingABError("actual ManagerClient profile differs from the manifest")
    return context, executor


def _run_single(
    *, arm_root: Path, manifest: dict[str, Any], policy: str, task_id: str,
    run_task: bool, run_event: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    record = manifest["traces"][task_id]
    trace = _read_json(Path(record["path"]))
    ref = _trajectory_ref(record, trace)
    context, executor = _context(arm_root=arm_root, manifest=manifest, policy=policy, task_id=task_id)
    result: dict[str, Any] = {"task_id": task_id, "trajectory_ref": ref}
    candidates: list[dict[str, Any]] = []
    if run_task:
        candidates, evidence = legacy_task._single_task_candidate_extraction(
            context=context, trace=trace, current_ref=ref, executor=executor,
        )
        result["task_candidate"] = {"evidence": evidence, "candidates": candidates}
    if run_event:
        from scripts.legacy.run_r015_c_only_harbor_driver import _event_extraction
        attempts, event_candidates, evidence = _event_extraction(
            context=context, input_value={}, trace=trace, executor=executor,
        )
        result["event"] = {
            "attempts": attempts,
            "candidates": event_candidates,
            "evidence": evidence,
        }
    return result, candidates


def _run_fixed_merge(
    *, arm_root: Path, manifest: dict[str, Any], policy: str,
    candidate_by_task: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    task_ids = ["fix-git", "git-leak-recovery"]
    if any(len(candidate_by_task.get(task_id, [])) != 1 for task_id in task_ids):
        return {
            "status": "not_run",
            "reason": "fixed-source merge requires one validated isolated candidate from each source",
            "source_task_ids": task_ids,
        }
    traces = [_read_json(Path(manifest["traces"][task_id]["path"])) for task_id in task_ids]
    records = [
        legacy_task._validate_task_candidate_record(
            candidate_by_task[task_id][0],
            field=f"paired_merge.{policy}.{task_id}",
            expected_round=1,
            expected_task_id=task_id,
            expected_trace=trace,
            expected_trajectory_ref=_trajectory_ref(manifest["traces"][task_id], trace),
            expected_historical_thinking_policy=driver._historical_thinking_policy_identity(
                {"driver_config": {"historical_thinking_policy": policy}}
            ),
        )
        for task_id, trace in zip(task_ids, traces, strict=True)
    ]
    merge_candidates = [
        {
            "canonical_instance_id": task_id,
            "candidate_id": record["candidate_id"],
            "skill": deepcopy(record["skill"]),
            "candidate_context": deepcopy(record["candidate_context"]),
            "official_task_outcome": deepcopy(record["official_task_outcome"]),
            "evidence": deepcopy(record["evidence"]),
        }
        for task_id, record in zip(task_ids, records, strict=True)
    ]
    context, executor = _context(
        arm_root=arm_root, manifest=manifest, policy=policy, task_id="s07-fixed-source-group",
    )
    prompt = driver._prompt_path("custom/r015_fig06_task_extraction_with_code_examples.md").read_text(encoding="utf-8")
    messages = task_candidate_merge_messages(merge_candidates, traces, paper_prompt=prompt)
    call, journal, invalid = driver._manager_call(
        executor,
        trial_id=str(context["trial_id"]),
        phase="fixed-source-group-merge",
        purpose=f"r015_thinking_ab_fixed_source_group:{policy}",
        messages=messages,
        trajectory_context=context,
        source_traces=traces,
        messages_builder=lambda values: task_candidate_merge_messages(merge_candidates, values, paper_prompt=prompt),
        metadata={
            "condition": "thinking-ab",
            "diagnostic_kind": "fixed_source_group_diagnostic",
            "natural_pairing_claim": False,
            "source_task_ids": task_ids,
        },
    )
    if call is None:
        journal = driver._finish_manager_journal(executor, journal, status="fixed_merge_output_rejected", value={"error": invalid})
        return {"status": "model_output_rejected", "error": invalid, "journal": journal}
    try:
        checked = validate_task_extraction_with_evidence(
            call.get("json"), traces, benchmark="terminal-bench",
            visible_step_ids_by_source=call.get("visible_step_ids_by_source"),
        )
    except (TypeError, ValueError) as error:
        journal = driver._finish_manager_journal(
            executor, journal, status="fixed_merge_output_rejected",
            value={"call_id": call.get("call_id"), "error_type": type(error).__name__, "error": str(error)},
        )
        return {
            "status": "validation_failed", "call_id": call.get("call_id"),
            "error_type": type(error).__name__, "error": str(error), "model_output": call.get("json"),
            "journal": journal,
        }
    journal = driver._finish_manager_journal(
        executor, journal, status="fixed_merge_validated",
        value={"call_id": call.get("call_id"), "validated": checked},
    )
    return {
        "status": "validated", "call_id": call.get("call_id"), "value": checked,
        "journal": journal, "natural_pairing_claim": False,
    }


def _call_inventory(manager_root: Path) -> list[dict[str, Any]]:
    inventory = []
    for call_dir in sorted((manager_root / "model_calls").glob("call-*")):
        request_path = call_dir / "request.json"
        response_path = call_dir / "response.json"
        preflight_path = call_dir / "preflight.json"
        item: dict[str, Any] = {"call_id": call_dir.name}
        if request_path.is_file():
            request = _read_json(request_path)
            item.update({
                "request_path": str(request_path.resolve()),
                "request_sha256": sha256_file(request_path),
                "purpose": request.get("purpose"),
                "call_metadata": request.get("call_metadata"),
                "preflight": request.get("preflight"),
                "request_options": {key: value for key, value in request.get("request", {}).items() if key != "messages"},
                "messages_sha256": sha256_text(canonical_json(request.get("request", {}).get("messages"))),
            })
        if response_path.is_file():
            response = _read_json(response_path)
            item.update({
                "response_path": str(response_path.resolve()),
                "response_sha256": sha256_file(response_path),
                "elapsed_seconds": response.get("elapsed_seconds"),
                "http_status": response.get("http_status"),
                "finish_reason": response.get("finish_reason"),
                "classification": response.get("classification"),
                "usage": response.get("usage"),
                "tokenizer_prompt_token_comparison": response.get("tokenizer_prompt_token_comparison"),
            })
        elif preflight_path.is_file():
            item.update({"preflight_path": str(preflight_path.resolve()), "preflight_sha256": sha256_file(preflight_path)})
        inventory.append(item)
    return inventory


def run_arm(args: argparse.Namespace) -> int:
    policy = validate_historical_thinking_policy(args.policy)
    manifest_path = Path(args.manifest).resolve()
    manifest = _read_json(manifest_path)
    _verify_manifest(manifest_path, manifest)
    arm_root = Path(args.run_dir).resolve()
    if arm_root.exists():
        raise ThinkingABError(f"arm run directory already exists; refusing replay: {arm_root}")
    arm_root.mkdir(parents=True)
    start_record = {
        "kind": "r015_historical_thinking_ab_arm_start",
        "started_at_utc": _utc_now(),
        "policy": policy,
        "policy_version": HISTORICAL_THINKING_POLICY_VERSION,
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
    }
    _write_json(arm_root / "arm-start.json", start_record)
    results: dict[str, Any] = {}
    candidates: dict[str, list[dict[str, Any]]] = {}
    try:
        for task_id, run_task, run_event in (
            ("build-pmars", True, True),
            ("fix-git", True, False),
            ("git-leak-recovery", True, False),
            ("schemelike-metacircular-eval", True, True),
        ):
            task_result, task_candidates = _run_single(
                arm_root=arm_root, manifest=manifest, policy=policy, task_id=task_id,
                run_task=run_task, run_event=run_event,
            )
            results[task_id] = task_result
            candidates[task_id] = task_candidates
            _write_json(arm_root / "partial-results.json", {"policy": policy, "results": results})
        results["s07-fixed-source-group"] = {
            "status": "pending_pair_gate",
            "reason": "both arms must have validated isolated candidates before either merge call",
        }
    except Exception as error:
        inventory = _call_inventory(arm_root / "manager")
        failure = {
            **start_record,
            "kind": "r015_historical_thinking_ab_arm_result",
            "finished_at_utc": _utc_now(),
            "status": "failed",
            "error_type": type(error).__name__,
            "error": str(error),
            "results": results,
            "manager_calls": inventory,
            "manager_call_count": len(inventory),
        }
        _write_json(arm_root / "arm-result.json", failure)
        raise ThinkingABError(
            f"arm {policy} stopped after preserving {len(inventory)} model-call artifacts: "
            f"{type(error).__name__}: {error}"
        ) from error
    inventory = _call_inventory(arm_root / "manager")
    final = {
        **start_record,
        "kind": "r015_historical_thinking_ab_arm_result",
        "finished_at_utc": _utc_now(),
        "status": "complete",
        "results": results,
        "manager_calls": inventory,
        "manager_call_count": len(inventory),
    }
    _write_json(arm_root / "arm-result.json", final)
    print(json.dumps({"result": str(arm_root / "arm-result.json"), "calls": len(inventory)}, indent=2))
    return 0


def run_pair_merge(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    manifest = _read_json(manifest_path)
    _verify_manifest(manifest_path, manifest)
    output = Path(args.output).resolve()
    if output.exists():
        raise ThinkingABError(f"paired merge result already exists; refusing replay: {output}")
    arm_results = {
        policy: _read_json(Path(path).resolve())
        for policy, path in (("keep", args.keep_run), ("exclude", args.exclude_run))
    }
    expected_manifest_sha = sha256_file(manifest_path)
    candidate_maps: dict[str, dict[str, list[dict[str, Any]]]] = {}
    missing: dict[str, list[str]] = {}
    for policy, result in arm_results.items():
        if (
            result.get("status") != "complete"
            or result.get("policy") != policy
            or result.get("manifest_sha256") != expected_manifest_sha
        ):
            raise ThinkingABError(f"{policy} arm is not a completed run of this manifest")
        candidates: dict[str, list[dict[str, Any]]] = {}
        for task_id in ("fix-git", "git-leak-recovery"):
            value = result["results"][task_id]["task_candidate"]["candidates"]
            candidates[task_id] = value
        candidate_maps[policy] = candidates
        missing[policy] = [task_id for task_id, value in candidates.items() if len(value) != 1]
    if any(missing.values()):
        merged = {
            policy: {
                "status": "not_run",
                "reason": "paired merge requires one validated isolated candidate per fixed source in both arms",
                "missing_predecessors_by_arm": missing,
            }
            for policy in ("keep", "exclude")
        }
    else:
        merged = {}
        for policy, path in (("keep", args.keep_run), ("exclude", args.exclude_run)):
            merged[policy] = _run_fixed_merge(
                arm_root=Path(path).resolve().parent,
                manifest=manifest,
                policy=policy,
                candidate_by_task=candidate_maps[policy],
            )
    value = {
        "kind": "r015_historical_thinking_ab_paired_fixed_source_merge",
        "created_at_utc": _utc_now(),
        "manifest_sha256": expected_manifest_sha,
        "fixed_sources": ["fix-git", "git-leak-recovery"],
        "missing_predecessors_by_arm": missing,
        "arms": merged,
    }
    _write_json(output, value)
    print(json.dumps({"paired_merge": str(output), "arms": {
        policy: result["status"] for policy, result in merged.items()
    }}, indent=2))
    return 0


def _strip_recognized_historical_reasoning(value: Any) -> Any:
    if isinstance(value, list):
        result = []
        for item in value:
            if (
                isinstance(item, dict)
                and item.get("type") in {"thinking", "reasoning"}
                and isinstance(item.get("text"), str)
                and set(item).issubset({"type", "text"})
            ):
                continue
            result.append(_strip_recognized_historical_reasoning(item))
        return result
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in {"thinking", "reasoning"} and (
                isinstance(item, str) or (isinstance(item, list) and all(isinstance(part, str) for part in item))
            ):
                continue
            result[key] = _strip_recognized_historical_reasoning(item)
        return result
    return value


def _aggregate(calls: list[dict[str, Any]]) -> dict[str, Any]:
    usage_keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    totals = {key: 0 for key in usage_keys}
    known = {key: 0 for key in usage_keys}
    elapsed = 0.0
    classifications: dict[str, int] = {}
    for call in calls:
        usage = call.get("usage")
        for key in usage_keys:
            value = usage.get(key) if isinstance(usage, dict) else None
            if isinstance(value, int):
                totals[key] += value
                known[key] += 1
        if isinstance(call.get("elapsed_seconds"), (int, float)):
            elapsed += float(call["elapsed_seconds"])
        label = str(call.get("classification") or call.get("finish_reason") or "unknown")
        classifications[label] = classifications.get(label, 0) + 1
    return {
        "call_count": len(calls), "usage_totals": totals, "usage_known_call_counts": known,
        "elapsed_seconds_sum": elapsed, "outcomes": classifications,
    }


def compare(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    manifest = _read_json(manifest_path)
    _verify_manifest(manifest_path, manifest)
    keep = _read_json(Path(args.keep_run).resolve())
    exclude = _read_json(Path(args.exclude_run).resolve())
    expected_manifest_sha = sha256_file(manifest_path)
    if keep.get("manifest_sha256") != expected_manifest_sha or exclude.get("manifest_sha256") != expected_manifest_sha:
        raise ThinkingABError("arm result is not bound to the supplied manifest")
    if keep.get("policy") != "keep" or exclude.get("policy") != "exclude":
        raise ThinkingABError("arm labels do not match keep/exclude")
    source_checks = {}
    for task_id, record in manifest["traces"].items():
        trace = _read_json(Path(record["path"]))
        kept = project_historical_thinking(trace, policy="keep")
        removed = project_historical_thinking(trace, policy="exclude")
        projected_again = project_historical_thinking(kept["manager_trace"], policy="exclude")
        exact = canonical_json(projected_again["manager_trace"]) == canonical_json(removed["manager_trace"])
        source_checks[task_id] = {
            "raw_trace_sha256": record["sha256"],
            "recognized_removal_count": len(removed["mapping"]["recognized_fields_removed"]),
            "unknown_reasoning_shape_count": len(removed["mapping"]["unrecognized_reasoning_like_fields"]),
            "keep_to_exclude_projection_exact": exact,
        }
        if not exact:
            raise ThinkingABError(f"policy projection invariant failed: {task_id}")
    keep_options = [item.get("request_options") for item in keep["manager_calls"] if "request_options" in item]
    exclude_options = [item.get("request_options") for item in exclude["manager_calls"] if "request_options" in item]
    option_domain = {canonical_json(item) for item in keep_options + exclude_options}
    if len(option_domain) != 1:
        raise ThinkingABError("manager request options differ within or across arms")
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": "r015_historical_thinking_manager_ab_comparison",
        "created_at_utc": _utc_now(),
        "manifest_path": str(manifest_path),
        "manifest_sha256": expected_manifest_sha,
        "source_policy_invariants": source_checks,
        "request_options_identical": True,
        "request_options": json.loads(next(iter(option_domain))),
        "keep": _aggregate(keep["manager_calls"]),
        "exclude": _aggregate(exclude["manager_calls"]),
        "call_count_equal": len(keep["manager_calls"]) == len(exclude["manager_calls"]),
        "interpretation_boundary": {
            "fixed_s07_group_is_natural_pairing_evidence": False,
            "solver_or_official_reward_evidence": False,
            "model_derived_summaries_may_cause_downstream_prompt_divergence": True,
        },
    }
    output = Path(args.output).resolve()
    if output.exists():
        raise ThinkingABError(f"comparison already exists; refusing overwrite: {output}")
    _write_json(output, result)
    print(json.dumps({"comparison": str(output), "sha256": sha256_file(output)}, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(description=__doc__)
    commands = top.add_subparsers(dest="command", required=True)
    p_prepare = commands.add_parser("prepare")
    p_prepare.add_argument("--source-root", required=True)
    p_prepare.add_argument("--output", required=True)
    p_prepare.add_argument("--base-url", required=True)
    p_prepare.add_argument("--model", default="deepseek-ai/DeepSeek-V4-Flash")
    p_prepare.add_argument("--profile", help="explicit experiment manager profile JSON")
    p_prepare.add_argument("--baseline-manifest", help="freeze the original source, prompt and case contract")
    p_prepare.add_argument("--code-head", help="accepted local Git HEAD for an isolated source export")
    p_prepare.add_argument("--code-snapshot-sha256", help="hash of the isolated source archive")
    p_prepare.set_defaults(func=prepare)
    p_arm = commands.add_parser("run-arm")
    p_arm.add_argument("--manifest", required=True)
    p_arm.add_argument("--run-dir", required=True)
    p_arm.add_argument("--policy", choices=("keep", "exclude"), required=True)
    p_arm.set_defaults(func=run_arm)
    p_compare = commands.add_parser("compare")
    p_compare.add_argument("--manifest", required=True)
    p_compare.add_argument("--keep-run", required=True)
    p_compare.add_argument("--exclude-run", required=True)
    p_compare.add_argument("--output", required=True)
    p_compare.set_defaults(func=compare)
    p_merge = commands.add_parser("pair-merge")
    p_merge.add_argument("--manifest", required=True)
    p_merge.add_argument("--keep-run", required=True)
    p_merge.add_argument("--exclude-run", required=True)
    p_merge.add_argument("--output", required=True)
    p_merge.set_defaults(func=run_pair_merge)
    return top


def main() -> int:
    args = parser().parse_args()
    try:
        return int(args.func(args))
    except (ThinkingABError, OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
