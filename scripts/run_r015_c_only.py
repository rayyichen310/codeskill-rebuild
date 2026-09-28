"""Prepare or (after explicit approval) run the isolated R015 C-only protocol.

``prepare`` and ``check`` are safe setup commands.  ``start`` requires the
literal ``--confirm-user-start`` gate and runs the repository's built-in public
Harbor driver by default.  The durable state machine still accepts an
explicitly selected driver for controlled integration tests.  One ordered process owns
the campaign; a crash or uncertain child process leaves a reconciliation
record instead of replaying a paid trial or advancing the task cursor.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import signal
import subprocess
import sys
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from codeskill_rebuild.c_only_protocol import (  # noqa: E402
    COnlyProtocol,
    COnlyProtocolError,
    validate_c_only_config,
)
from codeskill_rebuild.harbor_recovery import (  # noqa: E402
    HarborRecoveryError,
    load_harbor_recovery_manifest,
)
from codeskill_rebuild.manager_reconciliation import load_reconciliation_manifest  # noqa: E402
from codeskill_rebuild.types import canonical_json, read_json, sha256_file, sha256_text, utc_now, write_json  # noqa: E402


def _object(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise COnlyProtocolError(f"{field} must be an object")
    return value


def _nonempty(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise COnlyProtocolError(f"{field} must be a nonempty string")
    return value.strip()


def _repo_input_path(value: Path) -> Path:
    """Resolve a preparation input relative to this source checkout."""
    path = Path(value)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def _portable_path(value: Path) -> str:
    """Store repository-local evidence references with POSIX separators.

    The prepared config is copied to Linux T2. Windows ``str(Path)`` would
    leave backslashes in a relative JSON reference, which Linux treats as
    literal filename characters during the production driver check.
    """
    path = _repo_input_path(value)
    try:
        return path.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def _expected_session_id(trial_id: str) -> str:
    """Return the deterministic session identity used by the built-in driver."""
    return "r015-" + sha256_text(trial_id)[:28]


def _baseline_tasks(value: dict[str, Any]) -> list[dict[str, Any]]:
    if value.get("kind") != "r015_legacy_coding_baseline_manifest":
        raise COnlyProtocolError("baseline manifest is not the audited historical coding manifest")
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise COnlyProtocolError("baseline manifest has no tasks")
    return tasks


def _load_public_task_audit(path: Path, *, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """Load the public task/image audit used to pin the prepared tasks.

    The audit contains only task.toml and Docker inspect metadata.  It is
    deliberately a separate immutable input so the production driver can
    reject a changed task checkout or image instead of silently substituting
    another public artifact at formal start.
    """
    value = read_json(path)
    if value.get("kind") != "r015_public_tb21_task_artifact_audit":
        raise COnlyProtocolError(f"public task audit has an unexpected kind: {path}")
    records = value.get("tasks")
    if not isinstance(records, list) or len(records) != len(tasks):
        raise COnlyProtocolError("public task audit does not cover the complete configured task order")
    expected_ids = [str(item.get("canonical_instance_id")) for item in tasks]
    actual_ids = [str(item.get("task_id")) for item in records if isinstance(item, dict)]
    if actual_ids != expected_ids:
        raise COnlyProtocolError("public task audit order differs from the audited baseline order")
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise COnlyProtocolError(f"public task audit record {index} is not an object")
        task_toml = record.get("task_toml")
        if not isinstance(task_toml, dict) or not isinstance(task_toml.get("sha256"), str):
            raise COnlyProtocolError(f"public task audit record {index} has no task.toml hash")
        historical = record.get("historical_public_task_toml")
        if isinstance(historical, dict) and historical.get("matches_prepared_public_toml") is not True:
            raise COnlyProtocolError(
                f"public task audit record {index} differs from its historical public task.toml"
            )
        image = record.get("docker_image")
        if not isinstance(image, dict) or image.get("status") != "observed" or not isinstance(image.get("image_id"), str) or not image.get("image_id"):
            raise COnlyProtocolError(
                f"public task audit record {index} has no observed immutable Docker image identity"
            )
    return value


def _build_config(
    baseline_path: Path,
    *,
    gpu_evidence_path: Path | None = None,
    task_audit_path: Path | None = None,
) -> dict[str, Any]:
    baseline_path = _repo_input_path(baseline_path)
    if gpu_evidence_path is not None:
        gpu_evidence_path = _repo_input_path(gpu_evidence_path)
    if task_audit_path is not None:
        task_audit_path = _repo_input_path(task_audit_path)
    baseline = read_json(baseline_path)
    tasks = _baseline_tasks(baseline)
    if task_audit_path is None:
        candidate = ROOT / "docs" / "evidence" / "r015-c-only-public-task-audit-20260913-24.json"
        task_audit_path = candidate if candidate.is_file() else None
    task_audit: dict[str, Any] | None = None
    audit_by_task: dict[str, dict[str, Any]] = {}
    if task_audit_path is not None:
        task_audit = _load_public_task_audit(task_audit_path, tasks=tasks)
        audit_by_task = {
            str(record["task_id"]): record
            for record in task_audit["tasks"]
            if isinstance(record, dict)
        }
    protocol_id = "r015-c-only-coding-two-rounds-v1"
    first = _object(tasks[0], field="baseline.tasks[0]")
    effective = _object(first.get("effective_runtime"), field="baseline.tasks[0].effective_runtime")
    wire = _object(first.get("wire_log"), field="baseline.tasks[0].wire_log")
    baseline_protocol = _object(baseline.get("baseline_protocol"), field="baseline.baseline_protocol")
    model_name = _nonempty(baseline_protocol.get("model_name"), field="baseline_protocol.model_name")
    model_id = model_name.split("/", 1)[-1]
    provider_id = model_name.split("/", 1)[0] if "/" in model_name else "customendpoint"
    params = effective.get("params") if isinstance(effective.get("params"), dict) else {}
    extra_body = params.get("extra_body") if isinstance(params.get("extra_body"), dict) else {}
    endpoint = (wire.get("endpoint_urls") or [effective.get("provider_base_url")])[0]
    if not isinstance(endpoint, str) or not endpoint:
        raise COnlyProtocolError("baseline has no observed upstream endpoint")
    upstream_endpoint = endpoint.removesuffix("/chat/completions")
    observed_openclaw = "2026.7.2"
    for item in tasks:
        info = item.get("agent_info")
        version = info.get("version") if isinstance(info, dict) else None
        if isinstance(version, str) and "OpenClaw " in version:
            observed_openclaw = version.split("OpenClaw ", 1)[1].split()[0]
            break
    target_gpu: dict[str, Any] = {"status": "pending"}
    if gpu_evidence_path is not None:
        gpu = read_json(gpu_evidence_path)
        slurm = _object(gpu.get("slurm"), field="gpu.slurm")
        target_gpu = {
            "status": "observed",
            "evidence_path": _portable_path(gpu_evidence_path),
            "evidence_sha256": sha256_file(gpu_evidence_path),
            "job_id": slurm.get("job_id"),
            "job_state": slurm.get("state"),
            "time_left_seconds_at_capture": slurm.get("time_left_seconds_at_capture"),
            "main_port": 31000,
            "summarizer_port": 30002,
        }
    retrieval_profile_path = ROOT / "configs" / "m3-r015-development.json"
    retrieval_profile = read_json(retrieval_profile_path)
    if not isinstance(retrieval_profile, dict) or retrieval_profile.get("kind") != "r012_execution_profile":
        raise COnlyProtocolError(f"frozen R012 retrieval profile is missing or invalid: {retrieval_profile_path}")
    estimate_path = ROOT / "docs" / "evidence" / "r015-c-only-runtime-estimate-20260913-final.json"
    runtime_estimate = (
        {
            "path": _portable_path(estimate_path),
            "sha256": sha256_file(estimate_path),
            "comparison_only": True,
            "formal_campaign_started": False,
        }
        if estimate_path.is_file()
        else None
    )
    return {
        "schema_version": 1,
        "kind": "r015_c_only_two_round_protocol",
        "protocol_id": protocol_id,
        "status": "prepared",
        "baseline_manifest": {
            "path": _portable_path(baseline_path),
            "sha256": sha256_file(baseline_path),
            "comparison_only": True,
            "solver_input_imported": False,
            "skills_imported": False,
            "trajectories_imported": False,
        },
        "tasks": [
            {
                "order": int(item["order"]),
                "task_name": item["task_name"],
                "canonical_instance_id": item["canonical_instance_id"],
                "task_digest": item.get("task_digest"),
                **(
                    {
                        "public_task_toml_sha256": audit_by_task[item["canonical_instance_id"]]["task_toml"]["sha256"],
                        "public_task_toml_size_bytes": audit_by_task[item["canonical_instance_id"]]["task_toml"].get("size_bytes"),
                        "public_docker_image": audit_by_task[item["canonical_instance_id"]]["environment"].get("docker_image"),
                        "public_docker_image_id": audit_by_task[item["canonical_instance_id"]]["docker_image"].get("image_id"),
                        "public_docker_repo_digests": deepcopy(audit_by_task[item["canonical_instance_id"]]["docker_image"].get("repo_digests", [])),
                    }
                    if item["canonical_instance_id"] in audit_by_task
                    else {}
                ),
                "baseline_outcome": {
                    "classification": item.get("outcome", {}).get("classification"),
                    "reward": item.get("outcome", {}).get("reward"),
                    "exception_type": item.get("outcome", {}).get("exception_type"),
                    "duration_seconds": item.get("timing", {}).get("duration_seconds"),
                    "observed_model_calls": item.get("execution", {}).get("model_calls", 0),
                },
            }
            for item in tasks
        ],
        "protocol": {
            "condition": "C-only",
            "round_count": 2,
            "rounds_sequential": True,
            "fresh_bank_each_round": True,
            "fresh_trajectory_pool_each_round": True,
            "fresh_description_pool_each_round": True,
            "one_trial_per_task_per_round": True,
            "publish_after_each_task": True,
            "baseline_reference_only": True,
            "historical_skills_allowed": False,
            "historical_trajectories_allowed": False,
            "cross_round_material_allowed": False,
            "formal_start_requires_user_confirmation": True,
            "task_skill_pairing": {
                "min_distinct_completed_tasks": 2,
                "max_distinct_completed_tasks": 3,
                "early_action": "skip",
                "source": "same_round_completed_C_only_trajectories",
            },
            "event_extraction": {
                "stop_on": [],
                "repair_or_transport_retry_does_not_consume_initial_slot": True,
            },
            "publication": {
                "extraction_source": "current_task_complete_C_only_trajectory",
                "maintenance_source": "skills_actually_supplied_to_current_C_trial",
                "same_source_merge_preserves_provenance": True,
                "same_evaluation_task_exclusion": True,
                "atomic_before_next_task": True,
            },
        },
        "retrieval_profile": retrieval_profile,
        "driver": {
            "kind": "official_harbor_c_only_driver",
            "harbor_executable": "env:CODESKILL_HARBOR_BIN (or harbor on PATH)",
            "python_executable": "current interpreter",
            "task_root": "env:CODESKILL_TB21_TASK_ROOT (or the configured TB2.1 checkout)",
            "plugin_path": "openclaw_plugin",
            "sidecar_script": "scripts/run_openclaw_r012_sidecar.py",
            "manager_ledger": "run-dir/manager-ledger.json",
            "manager_model_config": "env:CODESKILL_MANAGER_CONFIG (optional; defaults to audited upstream endpoint)",
            "source_policy": "official Harbor/OpenClaw public adapter; no source mount, fork, dist patch, or monkeypatch",
        },
        "runtime_alignment": {
            "baseline_observed": {
                "model_id": model_id,
                "provider_id": provider_id,
                "endpoint": upstream_endpoint,
                "thinking": effective.get("thinking_default"),
                "reasoning_effort": extra_body.get("reasoning_effort"),
                "temperature": params.get("temperature"),
                "top_p": params.get("top_p"),
                "context_tokens": effective.get("context_tokens"),
                "max_output_tokens": effective.get("provider_model", {}).get("maxTokens"),
                "max_forwarded_requests": "not_configured_in_historical_manifest; observed_calls_are_not_a_cap",
                "agent_timeout_multiplier": baseline_protocol.get("agent_timeout_multiplier"),
                "agent_setup_timeout_seconds_observed": 360,
                "agent_timeout_seconds": "not_persisted_in_historical_manifest",
                "verifier_timeout_seconds": "not_persisted_in_historical_manifest",
                "outer_timeout_seconds": "not_persisted_in_historical_manifest",
                "max_turns": "not_persisted_in_historical_manifest",
                "harbor_version": baseline_protocol.get("harbor_version"),
                "openclaw_version": observed_openclaw,
                "dataset_ref": baseline.get("source", {}).get("dataset_ref"),
                "wire_evidence": {
                    "thinking_values": wire.get("thinking_values_logged"),
                    "temperature_values": wire.get("temperature_values_logged"),
                    "reasoning_effort_log_count": wire.get("extra_body_reasoning_effort_log_count"),
                    "response_statuses": wire.get("response_statuses"),
                },
            },
            "prepared_target": {
                "provider_id": "codeskill-r012",
                "solver_provider_id": "codeskill-r012",
                "solver_endpoint": "per-trial public CODESKILL sidecar",
                "endpoint": "per-trial public CODESKILL sidecar",
                "upstream_provider_id": provider_id,
                "model_id": model_id,
                "thinking": "high",
                "reasoning_effort": "max",
                "temperature": 1.0,
                "top_p": 0.95,
                "context_tokens": 270000,
                "max_output_tokens": 81920,
                # The frozen R012 profile contains historical stress-run
                # accounting fields.  The prepared protocol makes the
                # C-only transport budget explicit so those fields cannot
                # silently become a 24-request or 2700-second cap.
                "proxy_max_input_tokens": 250000,
                "proxy_max_output_tokens": 81920,
                "max_forwarded_requests": None,
                "proxy_max_forwarded_requests": None,
                "selection_condition": "C-only",
                "selection_arm": "C",
                "selection_profile_runtime_limits_ignored": ["proxy_max_forwarded_requests", "outer_timeout_seconds"],
                # Keep the historical multiplier and Harbor's documented
                # setup default. The baseline did not persist absolute
                # agent/verifier/outer limits, so null means the official
                # task and Harbor defaults must be captured before a start.
                "agent_timeout_multiplier": baseline_protocol.get("agent_timeout_multiplier"),
                "agent_setup_timeout_multiplier": 1.0,
                "agent_timeout_seconds": None,
                "setup_timeout_seconds": 360,
                "verifier_timeout_seconds": None,
                "outer_timeout_seconds": None,
                "provider_timeout_seconds": 900,
                "compaction": {
                    "midTurnPrecheck": {"enabled": True},
                    "native_summary": "official OpenClaw public configuration; capture effective runtime before formal start",
                },
                "tool_environment": {
                    "launcher": "official Harbor Docker task environment",
                    "task_image": "per Terminal-Bench 2.1 task; exact image digest captured per trial",
                    "workspace": "/app",
                    "shell": "official task image default shell",
                },
                "max_turns": None,
                "max_model_calls": None,
                "retry_count": 0,
                "harbor_concurrency": 1,
                # Harbor 0.17.1 is the historical version and is available in
                # the isolated T2 terminal-bench2 environment.  The public
                # adapter was imported and rendered against both Harbor
                # versions; selecting 0.17.1 removes an avoidable runtime
                # deviation while the OpenClaw package/image gate remains
                # explicit below.
                "harbor_version": baseline_protocol.get("harbor_version", "0.17.1"),
                "openclaw_version": "2026.9.3",
                "tb21_commit": "7131e4375048a0e408a8fb404b5f499d726b695b",
                "agent_uid": 1000,
                "agent_home": "/home/codeskill",
                "agent_workspace": "/app",
                "minilm_repo_id": "sentence-transformers/all-MiniLM-L6-v2",
                "minilm_revision": "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
                "wire_verification": "required before formal start; public adapter must emit the fields above",
            },
            "comparison_limitations": [
                "The prepared target now uses the historical Harbor 0.17.1, while the historical OpenClaw 2026.7.2 source checkout is not published as an npm version and the public adapter target is OpenClaw 2026.9.3.",
                "The historical task environment mounted the host OpenClaw tree read-only; the prepared adapter installs the public OpenClaw package and loads only the public CODESKILL plugin.",
                "The historical wire exposed customendpoint directly; the prepared solver wire exposes a per-trial public sidecar and keeps the same upstream model request settings.",
                "No historical fixed max-call or max-turn cap was found; the prepared sidecar and official task driver intentionally leave both unset so the observed 50-call task is representable.",
                "The baseline persisted agent_timeout_multiplier=4.0 and observed a 360-second setup timeout, but it did not persist absolute agent, verifier, or outer limits. The prepared target keeps the multiplier, setup default, and provider timeout while requiring per-trial effective timeout capture before formal start.",
                "Baseline compaction and task-image shell details are observed only where their effective artifacts record them; exact task image digests and task-derived verifier limits remain per-trial evidence fields.",
                "A clean historical OpenClaw 2026.7.2 source checkout loads and validates the public CODESKILL plugin under Node 22.23.1, but the unchanged Harbor installer cannot install that source-only release; the parity gate records the minimum decision instead of silently substituting 2026.9.3.",
            ],
            "parity_gate": {
                "status": "blocked_requires_user_decision",
                "evidence_path": "docs/evidence/r015-c-only-runtime-parity-20260913-03.json",
                "decision_options": [
                    "authorize an immutable public-interface installation transport for the clean historical OpenClaw 2026.7.2 source checkout and provide or recover the historical public task image identities; source mounts, forks, dist patches, and monkeypatches remain forbidden",
                    "explicitly approve the prepared Harbor 0.17.1/OpenClaw 2026.9.3 public-package target and the unproven historical task-image identity, recording that deviation before formal start",
                ],
                "rule": "the formal runner must refuse a blocked parity gate until one option is explicitly recorded",
            },
        },
        "task_artifact_audit": (
            {
                "path": _portable_path(task_audit_path),
                "sha256": sha256_file(task_audit_path),
                "historical_public_toml_comparison": "all configured task.toml hashes match the isolated historical public metadata tree",
                "docker_image_identity": "all configured public image IDs were observed by Docker image inspect at audit time",
                "hidden_material_read": False,
            }
            if task_audit is not None and task_audit_path is not None
            else None
        ),
        "runtime_estimate": runtime_estimate,
        "<MODEL_SERVICE_HOST>": target_gpu,
        "formal": {
            "status": "not_started",
            "planned_trials": len(tasks) * 2,
            "task_count_per_round": len(tasks),
            "start_requires": ["--confirm-user-start", "--accept-runtime-deviation"],
            "start_command": "python scripts/run_r015_c_only.py start --config configs/r015-c-only-coding.json --baseline-manifest docs/baselines/r015-legacy-coding-baseline-20260913.json --state runs/r015-c-only-coding/state.json --run-dir runs/r015-c-only-coding --trial-driver scripts/run_r015_c_only_harbor_driver.py --confirm-user-start --accept-runtime-deviation",
            "resume_command": "python scripts/run_r015_c_only.py resume --config configs/r015-c-only-coding.json --baseline-manifest docs/baselines/r015-legacy-coding-baseline-20260913.json --state runs/r015-c-only-coding/state.json --run-dir runs/r015-c-only-coding --trial-driver scripts/run_r015_c_only_harbor_driver.py --confirm-user-start --accept-runtime-deviation",
        },
    }


def _write_if_new(path: Path, value: dict[str, Any]) -> None:
    if path.exists():
        existing = read_json(path)
        if canonical_json(existing) != canonical_json(value):
            raise COnlyProtocolError(f"refusing to overwrite an existing different file: {path}")
        return
    write_json(path, value)


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    value = _build_config(
        args.baseline_manifest,
        gpu_evidence_path=args.gpu_evidence,
        task_audit_path=args.task_audit,
    )
    validate_c_only_config(value)
    _write_if_new(args.output_config, value)
    if args.state is not None:
        if args.state.exists():
            raise COnlyProtocolError(f"state already exists; use check/resume: {args.state}")
        protocol = COnlyProtocol.initialize(args.output_config, args.baseline_manifest, args.state)
        protocol.save(args.state)
    return {"status": "prepared", "config": str(args.output_config), "config_sha256": sha256_file(args.output_config), "state": str(args.state) if args.state else None, "formal_campaign": "not_started", "planned_trials": len(value["tasks"]) * 2}


def check(args: argparse.Namespace) -> dict[str, Any]:
    config = validate_c_only_config(read_json(args.config))
    baseline = read_json(args.baseline_manifest)
    if config["baseline_manifest"]["sha256"] != sha256_file(args.baseline_manifest):
        raise COnlyProtocolError("config baseline manifest hash does not match supplied baseline")
    if args.state is not None:
        protocol = COnlyProtocol.load(args.state, args.config, args.baseline_manifest)
        return {
            "status": "valid",
            "config_sha256": sha256_file(args.config),
            "baseline_sha256": sha256_file(args.baseline_manifest),
            "current_round": protocol.current_round_id,
            "current_task": protocol.current_task_id,
            "formal_campaign": protocol.state["formal_campaign"],
            "rounds": {key: {"status": value["status"], "next_task_index": value["next_task_index"], "bank_skills": len(value["bank"].get("skills", [])), "trajectory_pool": len(value["trajectory_pool"]), "description_pool": len(value["description_pool"]), "task_candidate_pool": len(value.get("task_candidate_pool", []))} for key, value in protocol.state["rounds"].items()},
        }
    return {"status": "valid", "config_sha256": sha256_file(args.config), "baseline_sha256": sha256_file(args.baseline_manifest), "task_count": len(config["tasks"]), "planned_trials": len(config["tasks"]) * 2, "formal_campaign": "not_started"}


def resume(args: argparse.Namespace) -> dict[str, Any]:
    protocol = COnlyProtocol.load(args.state, args.config, args.baseline_manifest)
    # A plain ``resume`` remains a read-only status query for a prepared
    # campaign. Once the user gate is supplied (or a campaign is already
    # active), select the repository's built-in driver automatically. This
    # keeps the production recovery command executable without treating a
    # placeholder/external driver as a prerequisite, while still allowing an
    # explicitly supplied driver in controlled integration tests.
    trial_driver = args.trial_driver
    if trial_driver is None and (args.confirm_user_start or protocol.state["formal_campaign"] == "active"):
        trial_driver = ROOT / "scripts" / "run_r015_c_only_harbor_driver.py"
    if trial_driver is not None:
        if not trial_driver.is_file():
            raise COnlyProtocolError(f"official C-only Harbor driver is not a file: {trial_driver}")
        _ensure_runtime_parity_gate(
            protocol,
            accept_runtime_deviation=bool(getattr(args, "accept_runtime_deviation", False)),
            state_path=args.state,
        )
        if protocol.state["formal_campaign"] == "not_started":
            if not args.confirm_user_start:
                raise COnlyProtocolError("formal start is gated; pass --confirm-user-start only after the user explicitly approves")
            protocol.authorize_start()
            protocol.save(args.state)
        if protocol.state["formal_campaign"] == "complete":
            raise COnlyProtocolError("formal campaign is already complete")
        run_dir = (args.run_dir or args.state.parent).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        with _execution_lock(run_dir, state_path=args.state):
            _run_driver(
                protocol,
                trial_driver,
                run_dir,
                args.state,
                reconciliation_manifest_path=getattr(args, "reconciliation_manifest", None),
                harbor_recovery_manifest_path=getattr(args, "harbor_recovery_manifest", None),
            )
        return {
            "status": protocol.state["formal_campaign"],
            "state": str(args.state),
            "current_round": protocol.current_round_id,
            "current_task": protocol.current_task_id,
            "resumed": True,
        }
    return {
        "status": "resume_ready" if protocol.state["formal_campaign"] in {"not_started", "active"} else protocol.state["formal_campaign"],
        "current_round": protocol.current_round_id,
        "current_task": protocol.current_task_id,
        "formal_campaign": protocol.state["formal_campaign"],
        "next_action": "freeze current task, then invoke the built-in official driver" if protocol.current_task_id else "start the next round or inspect completion",
    }


def _driver_result(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise COnlyProtocolError(f"trial driver did not create its output: {path}")
    value = read_json(path)
    if not isinstance(value, dict):
        raise COnlyProtocolError("trial driver output must be an object")
    return value


def _driver_command(
    driver: Path,
    input_path: Path,
    output_path: Path,
    *,
    continue_from_trial: bool = False,
    continue_from_extraction: bool = False,
    reconciliation_manifest_path: Path | None = None,
    harbor_recovery_manifest_path: Path | None = None,
) -> list[str]:
    """Build one explicit command for either a Python or native driver."""
    if reconciliation_manifest_path is not None and not continue_from_trial:
        raise COnlyProtocolError("a manager reconciliation manifest requires --continue-from-trial")
    if continue_from_extraction and (continue_from_trial or reconciliation_manifest_path is not None or harbor_recovery_manifest_path is not None):
        raise COnlyProtocolError("extraction continuation cannot be combined with another recovery mode")
    if harbor_recovery_manifest_path is not None and (continue_from_trial or reconciliation_manifest_path is not None):
        raise COnlyProtocolError("a Harbor artifact recovery cannot be combined with trial continuation or manager reconciliation")
    # An audited pre-Graph call has an explicit historical entrypoint. Fresh
    # trials and ordinary completed-trial recovery remain on the Graph driver.
    if (reconciliation_manifest_path is not None and
            driver.resolve() == (ROOT / "scripts" / "run_r015_c_only_harbor_driver.py").resolve()):
        driver = ROOT / "scripts" / "legacy" / "run_r015_c_only_harbor_driver.py"
    prefix = [sys.executable, str(driver)] if driver.suffix.lower() in {".py", ".pyw"} else [str(driver)]
    command = [*prefix, "--input", str(input_path), "--output", str(output_path)]
    if continue_from_trial:
        command.append("--continue-from-trial")
    if continue_from_extraction:
        command.append("--continue-from-extraction")
    if reconciliation_manifest_path is not None:
        command.extend(["--reconciliation-manifest", str(Path(reconciliation_manifest_path).resolve())])
    if harbor_recovery_manifest_path is not None:
        command.extend(["--recover-from-harbor-manifest", str(Path(harbor_recovery_manifest_path).resolve())])
    return command


def _driver_process_path(input_path: Path) -> Path:
    return input_path.with_name("driver-process.json")


def _driver_continuation_process_path(input_path: Path) -> Path:
    return input_path.with_name("driver-continuation-process.json")


def _reconcile_path(input_path: Path) -> Path:
    return input_path.with_name("reconcile-required.json")


_DRIVER_PHASES = ("trial", "extraction", "publication")


def _driver_stage_path(input_path: Path, phase: str) -> Path:
    if phase not in _DRIVER_PHASES:
        raise COnlyProtocolError(f"unknown durable C-only driver phase: {phase}")
    return input_path.with_name(f"driver-stage-{phase}.json")


def _trusted_manager_root(run_dir: Path) -> Path:
    """Return the manager artifact root for one trusted execution namespace.

    The logical campaign state may live above an isolated attempt directory:
    ``<logical-run>/state.json`` versus
    ``<logical-run>/attempts/<attempt>/manager``.  Manager response binding
    must follow the coordinator's execution ``run_dir`` rather than infer a
    sibling from the state path or from driver output.
    """
    resolved = Path(run_dir).resolve()
    # The coordinator owns this path and may be starting a fresh attempt
    # namespace.  Create that trusted namespace before deriving its manager
    # root; never derive it from the mutable state path.
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved / "manager"


def _load_driver_stage(
    input_path: Path,
    *,
    assignment: dict[str, Any],
    phase: str,
    strict_manager: bool = False,
    manager_root: Path | None = None,
    frozen_state_ref: tuple[Path, str] | None = None,
    stage_path: Path | None = None,
) -> dict[str, Any] | None:
    """Read one immutable, completed phase written by the built-in driver.

    A phase file is useful only when its process and input binding are also
    durable.  This prevents a half-written Harbor/model request from being
    mistaken for a safe resume point.
    """
    path = stage_path or _driver_stage_path(input_path, phase)
    if not path.is_file():
        return None
    value = _object(read_json(path), field=f"driver-stage-{phase}")
    if value.get("kind") != "r015_c_only_driver_stage" or value.get("status") != "complete" or value.get("phase") != phase:
        raise COnlyProtocolError(f"durable driver stage is not a completed {phase} record: {path}")
    for field in ("condition", "round_id", "task_id", "trial_id"):
        expected = "C-only" if field == "condition" else assignment[field]
        if value.get(field) != expected:
            raise COnlyProtocolError(f"driver stage {phase} is bound to a different assignment: {path}")
    if value.get("input_path") != str(input_path) or value.get("input_sha256") != sha256_file(input_path):
        raise COnlyProtocolError(f"driver stage {phase} input binding changed: {path}")
    payload = _object(value.get("payload"), field=f"driver-stage-{phase}.payload")
    if value.get("payload_sha256") != sha256_text(canonical_json(payload)):
        raise COnlyProtocolError(f"driver stage {phase} payload hash changed: {path}")
    _validate_driver_payload_refs(
        payload,
        field=f"driver-stage-{phase}.payload",
        strict_manager=strict_manager,
        manager_root=manager_root,
        frozen_state_ref=frozen_state_ref,
    )
    return payload


def _trial_stage_is_safe_continuation(
    *,
    input_path: Path,
    output_path: Path,
    assignment: dict[str, Any],
    manager_root: Path | None = None,
    allow_test_fixture: bool = False,
    allow_reconciled_manager_journal: bool = False,
) -> bool:
    """Check whether a failed child stopped immediately after Harbor trial.

    The check is deliberately conservative.  It returns true only for an
    immutable trial stage, a terminal original process record, no final
    output, and either no manager journal or the one event-001 journal named by
    an explicit reconciliation manifest.  Any other journal means a paid
    manager call may have started, so the caller must reconcile instead of
    replaying.
    """
    if not _driver_stage_path(input_path, "trial").is_file():
        return False
    if any(_driver_stage_path(input_path, phase).is_file() for phase in ("extraction", "publication")):
        return False
    process_path = _driver_process_path(input_path)
    if not process_path.is_file():
        return False
    process = _object(read_json(process_path), field="driver-process")
    status = process.get("status")
    if status not in {"failed", "succeeded"} or process.get("timed_out") is True:
        return False
    # The outer process record is the second boundary for this recovery path.
    # A contradictory status/return-code pair is not evidence that Harbor
    # completed; refuse it instead of treating a hand-edited record as a safe
    # manager-phase continuation.
    returncode = process.get("returncode")
    if status == "succeeded" and returncode != 0:
        return False
    if status == "failed" and returncode == 0:
        return False
    if process.get("input_path") != str(input_path) or process.get("input_sha256") != sha256_file(input_path):
        _mark_reconcile(
            input_path,
            assignment=assignment,
            phase="trial_continuation_input_mismatch",
            error="completed trial stage process input binding changed",
            process=process,
        )
        raise COnlyProtocolError("completed trial stage process input binding changed; reconcile before continuation")
    if output_path.is_file():
        return False
    payload = _load_driver_stage(
        input_path,
        assignment=assignment,
        phase="trial",
        strict_manager=not allow_test_fixture,
        manager_root=manager_root,
        frozen_state_ref=_input_frozen_state_ref(input_path),
    )
    if payload is None:
        return False
    trial = _object(payload.get("trial"), field="driver-stage-trial.trial")
    if trial.get("outcome") != "completed" or not isinstance(payload.get("trace"), dict) or not isinstance(payload.get("proxy_attempt_records"), list):
        return False
    process_payload = _object(payload.get("official_process"), field="driver-stage-trial.official_process")
    if process_payload.get("official_trial_boundary_started") is not True:
        return False
    journal_root = manager_root.parent if manager_root is not None else input_path.parents[2]
    journal_dir = journal_root / "manager-journals" / str(assignment["task_id"]) / sha256_text(str(assignment["trial_id"]))[:20]
    if journal_dir.is_dir() and any(path.is_file() for path in journal_dir.glob("*.json")):
        journal_files = sorted(path for path in journal_dir.glob("*.json") if path.is_file())
        expected_event = journal_dir / "event-001.json"
        if not allow_reconciled_manager_journal or journal_files != [expected_event]:
            _mark_reconcile(
                input_path,
                assignment=assignment,
                phase="trial_continuation_manager_journal_present",
                error="a manager journal exists after the completed trial stage; manager state may be paid or in-flight",
                process=process,
            )
            raise COnlyProtocolError("completed trial has existing manager journal state; reconcile before continuation")
    return True


def _validate_manager_response_binding(
    value: dict[str, Any],
    *,
    field: str,
    manager_root: Path | None,
) -> None:
    """Bind manager response refs to the call directory that produced them.

    A SHA-256 check proves that a referenced file was not changed after the
    driver wrote its JSON, but it does not by itself prove that a caller did
    not point a ``Fig.9`` or D01/D02 record at another run's response.  The
    built-in driver always stores responses as
    ``<run>/manager/model_calls/<call-id>/response.json`` and carries the
    call id next to the reference.  Validate that public, durable convention
    while keeping controlled fixture payloads available through the explicit
    test-only path in the caller.
    """
    call_id_value = value.get("manager_call_id")
    call_id = str(call_id_value).strip() if isinstance(call_id_value, str) and call_id_value.strip() else None
    nested_response = value.get("response")
    refs: list[tuple[str, dict[str, Any], str | None]] = []
    if isinstance(nested_response, dict) and ("path" in nested_response or "sha256" in nested_response):
        nested_call = nested_response.get("call_id")
        nested_call_id = str(nested_call).strip() if isinstance(nested_call, str) and nested_call.strip() else None
        refs.append((f"{field}.response", nested_response, nested_call_id))
        if call_id is not None and nested_call_id != call_id:
            raise COnlyProtocolError(f"{field}.response.call_id differs from manager_call_id")
        if call_id is None:
            call_id = nested_call_id
    for prefix in ("manager_response", "fig8_manager_response", "fig9_response"):
        path_value = value.get(prefix + "_path")
        hash_value = value.get(prefix + "_sha256")
        if path_value is None and hash_value is None:
            continue
        if not isinstance(path_value, str) or not path_value.strip() or not isinstance(hash_value, str) or not hash_value.strip():
            raise COnlyProtocolError(f"{field}.{prefix} must contain both path and sha256")
        refs.append(
            (
                f"{field}.{prefix}",
                {"path": path_value, "sha256": hash_value},
                call_id,
            )
        )
    if not refs:
        return
    if any(Path(str(ref.get("path"))).name == "wire-response.json" for _field, ref, _call in refs):
        if manager_root is None:
            raise COnlyProtocolError(f"{field} Task Graph response has no current manager root")
        graph_key = value.get("call_key", call_id)
        if not isinstance(graph_key, str) or len(graph_key) != 64 or any(char not in "0123456789abcdef" for char in graph_key):
            raise COnlyProtocolError(f"{field} Task Graph call key is invalid")
        if call_id is not None and call_id != graph_key:
            raise COnlyProtocolError(f"{field} Task Graph manager_call_id differs from call key")
        call_dir = manager_root.absolute() / "task-calls" / graph_key
        identity_path = call_dir / "identity.json"
        request_path = call_dir / "wire-request.json"
        if not identity_path.is_file() or not request_path.is_file():
            raise COnlyProtocolError(f"{field} Task Graph request/identity is missing")
        identity_record = read_json(identity_path)
        if sha256_text(canonical_json(identity_record)) != graph_key:
            raise COnlyProtocolError(f"{field} Task Graph call key differs from immutable identity")
        if value.get("request") is not None:
            request_ref = value["request"]
            if request_ref.get("path") != str(request_path) or request_ref.get("sha256") != sha256_file(request_path):
                raise COnlyProtocolError(f"{field} Task Graph request binding differs")
        for ref_field, ref, _ref_call_id in refs:
            response_path = Path(str(ref.get("path"))).absolute()
            if not response_path.is_file() or response_path != call_dir / "wire-response.json" or ref.get("sha256") != sha256_file(response_path):
                raise COnlyProtocolError(f"{ref_field} Task Graph response is outside its producing call")
            response_value = read_json(response_path)
            if not isinstance(response_value, dict) or not isinstance(response_value.get("choices"), list):
                raise COnlyProtocolError(f"{ref_field} Task Graph raw response is invalid")
        return
    if manager_root is None:
        raise COnlyProtocolError(f"{field} manager response cannot be validated without the current run manager root")
    if call_id is None:
        # A response reference without a call id is ambiguous at the
        # production boundary.  Fig.8/Fig.9 and D01/D02 records all carry the
        # id emitted by ManagerClient; accepting an unbound file would permit
        # cross-run provenance relabelling.
        raise COnlyProtocolError(f"{field} manager response has no call id binding")
    for ref_field, ref, ref_call_id in refs:
        if ref_call_id != call_id:
            raise COnlyProtocolError(f"{ref_field} call id differs from manager_call_id")
        path = Path(str(ref.get("path"))).absolute()
        stated_hash = ref.get("sha256")
        if not path.is_file() or not isinstance(stated_hash, str) or sha256_file(path) != stated_hash:
            # The recursive verifier below reports the same condition for all
            # ordinary refs; keep this local check so the semantic validator
            # remains safe when called independently.
            raise COnlyProtocolError(f"{ref_field} is not an immutable manager response file: {path}")
        if path.name != "response.json" or path.parent.name != call_id or path.parent.parent.name != "model_calls":
            raise COnlyProtocolError(f"{ref_field} is outside its ManagerClient call directory: {path}")
        try:
            path.relative_to(manager_root.absolute())
        except ValueError as error:
            raise COnlyProtocolError(f"{ref_field} points outside the current run manager root: {path}") from error
        try:
            response_value = read_json(path)
        except (OSError, ValueError) as error:
            raise COnlyProtocolError(f"{ref_field} response JSON is unreadable: {path}") from error
        if not isinstance(response_value, dict) or response_value.get("kind") != "live_manager_call":
            raise COnlyProtocolError(f"{ref_field} is not a ManagerClient live response record: {path}")
        if not isinstance(response_value.get("purpose"), str) or not response_value["purpose"].strip():
            raise COnlyProtocolError(f"{ref_field} live response has no durable manager purpose: {path}")


def _validate_driver_payload_refs(
    value: Any,
    *,
    field: str,
    strict_manager: bool = False,
    manager_root: Path | None = None,
    frozen_state_ref: tuple[Path, str] | None = None,
) -> None:
    """Verify every embedded immutable ``path``/``sha256`` pair.

    Stage hashes prove only that the JSON wrapper was not edited.  This
    recursive check also proves that manager responses, journals, sidecar
    attempts, Harbor packets, and normalized trajectories still point to the
    bytes that the completed process actually produced.  A completed trial
    may also carry the state file hash captured at its launch boundary.  The
    coordinator legitimately appends its own timestamp/state transition while
    recovering that trial, so callers may pass the exact input-bound frozen
    state reference for that one path.  This exception is exact and scoped to
    the input binding; arbitrary stale or relabelled references still fail.
    """
    if isinstance(value, dict):
        if strict_manager:
            _validate_manager_response_binding(value, field=field, manager_root=manager_root)
        if "path" in value and "sha256" in value and isinstance(value.get("path"), str):
            path = Path(value["path"])
            if value.get("exists") is not False:
                if not path.is_file():
                    raise COnlyProtocolError(f"{field}.path does not identify an immutable evidence file: {path}")
                stated = value.get("sha256")
                frozen_match = (
                    frozen_state_ref is not None
                    and path.resolve() == frozen_state_ref[0].resolve()
                    and stated == frozen_state_ref[1]
                )
                if not isinstance(stated, str) or (stated != sha256_file(path) and not frozen_match):
                    raise COnlyProtocolError(f"{field}.sha256 does not match its evidence file: {path}")
        for key, child in value.items():
            _validate_driver_payload_refs(
                child,
                field=f"{field}.{key}",
                strict_manager=strict_manager,
                manager_root=manager_root,
                frozen_state_ref=frozen_state_ref,
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_driver_payload_refs(
                child,
                field=f"{field}[{index}]",
                strict_manager=strict_manager,
                manager_root=manager_root,
                frozen_state_ref=frozen_state_ref,
            )


def _input_frozen_state_ref(input_path: Path) -> tuple[Path, str]:
    """Return the immutable state binding captured in one driver input.

    The returned hash is the launch-time state identity, not a claim that the
    mutable coordinator file has never advanced.  Recovery validators use it
    only for the exact state path and hash pair already embedded in the input.
    """
    value = _object(read_json(input_path), field="driver input")
    state = _object(value.get("state"), field="driver input.state")
    path_value = state.get("path")
    stated = state.get("sha256")
    if not isinstance(path_value, str) or not path_value.strip() or not isinstance(stated, str) or not stated.strip():
        raise COnlyProtocolError("driver input.state must carry a frozen path and sha256")
    return Path(path_value).resolve(), stated


def _stage_output(
    input_path: Path,
    *,
    assignment: dict[str, Any],
    phase: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Convert a built-in driver's completed phase into the outer output shape."""
    result = {
        "schema_version": 1,
        "kind": "r015_c_only_trial_driver_output",
        "condition": "C-only",
        "round_id": assignment["round_id"],
        "task_id": assignment["task_id"],
        "phase": phase,
        **deepcopy(payload),
    }
    return result


def _mark_reconcile(
    input_path: Path,
    *,
    assignment: dict[str, Any],
    phase: str,
    error: BaseException | str,
    process: dict[str, Any] | None = None,
) -> None:
    value = {
        "schema_version": 1,
        "kind": "r015_c_only_reconcile_required",
        "created_at_utc": utc_now(),
        "condition": "C-only",
        "round_id": assignment["round_id"],
        "task_id": assignment["task_id"],
        "trial_id": assignment["trial_id"],
        "phase": phase,
        "error_type": type(error).__name__ if isinstance(error, BaseException) else "ReconciliationRequired",
        "error": str(error),
        "driver_process": deepcopy(process),
        "action": "manual reconciliation required; no automatic retry or task advance",
    }
    path = _reconcile_path(input_path)
    if path.exists():
        # The first failure is the authoritative boundary.  A later cleanup
        # exception must never replace or mask it, because doing so can hide
        # the original Harbor/model failure and make a resume look safe.
        # Keep the original bytes immutable; callers can inspect subsequent
        # exceptions from the durable process record and stderr.
        return
    write_json(path, value)


def _configured_outer_timeout(protocol: COnlyProtocol) -> float | None:
    runtime = _object(protocol.config.get("runtime_alignment"), field="runtime_alignment")
    prepared = _object(runtime.get("prepared_target"), field="runtime_alignment.prepared_target")
    value = prepared.get("outer_timeout_seconds")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise COnlyProtocolError("runtime_alignment.prepared_target.outer_timeout_seconds must be null or positive")
    return float(value)


def _ensure_runtime_parity_gate(
    protocol: COnlyProtocol,
    *,
    accept_runtime_deviation: bool,
    state_path: Path,
) -> None:
    """Fail closed when preparation has not established historical parity.

    The prepared target intentionally carries a concrete public runtime, but
    the parity audit currently proves that it differs from the historical
    Harbor/OpenClaw versions and does not prove the old image digest.  A
    caller must therefore opt into the documented deviation.  The decision
    is persisted only after this explicit flag is supplied, so later resume
    calls can be audited without silently inferring consent from an active
    process.
    """
    runtime = _object(protocol.config.get("runtime_alignment"), field="runtime_alignment")
    gate = runtime.get("parity_gate")
    if gate is None:
        return
    gate = _object(gate, field="runtime_alignment.parity_gate")
    status = gate.get("status")
    if status == "aligned":
        return
    if status == "approved_deviation":
        return
    if status != "blocked_requires_user_decision":
        raise COnlyProtocolError("runtime parity gate has an unknown status")
    previous = protocol.state.get("runtime_parity_decision")
    if isinstance(previous, dict) and previous.get("status") == "approved_deviation":
        return
    if not accept_runtime_deviation:
        raise COnlyProtocolError(
            "runtime parity gate is blocked; record an explicit runtime decision before starting "
            "(--accept-runtime-deviation is required for the prepared deviation)"
        )
    decision = {
        "status": "approved_deviation",
        "accepted_at_utc": utc_now(),
        "gate_evidence_path": gate["evidence_path"],
        "gate_evidence_sha256": None,
        "decision": "explicit CLI acceptance of the prepared runtime deviation",
    }
    evidence_path = Path(str(gate["evidence_path"]))
    if not evidence_path.is_absolute():
        evidence_path = ROOT / evidence_path
    if evidence_path.is_file():
        decision["gate_evidence_sha256"] = sha256_file(evidence_path)
    protocol.state["runtime_parity_decision"] = decision
    protocol.save(state_path)


def _process_group_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    """Stop Harbor and any sidecar descendants after an explicit timeout."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                return


@contextmanager
def _execution_lock(run_dir: Path, *, state_path: Path) -> Any:
    """Serialize the campaign and leave a crash marker for manual recovery."""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / ".r015-c-only.lock"
    payload = {
        "kind": "r015_c_only_execution_lock",
        "pid": os.getpid(),
        "created_at_utc": utc_now(),
        "state_path": str(state_path.resolve()),
        "note": "A surviving lock after process loss requires manual reconciliation; it is never auto-cleared.",
    }
    try:
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        try:
            existing = read_json(path)
        except Exception:
            existing = {"path": str(path), "contents": path.read_text(encoding="utf-8", errors="replace")}
        raise COnlyProtocolError(f"C-only runner lock exists; inspect and reconcile before resuming: {existing}") from error
    try:
        os.close(descriptor)
        write_json(path, payload)
        yield
    finally:
        try:
            current = read_json(path)
            if current.get("pid") == payload["pid"] and current.get("created_at_utc") == payload["created_at_utc"]:
                path.unlink()
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            # If cleanup itself cannot prove ownership, retain the marker.
            pass


def _write_driver_input(protocol: COnlyProtocol, assignment: dict[str, Any], run_dir: Path, state_path: Path) -> Path:
    task_id = assignment["task_id"]
    path = run_dir / f"round-{assignment['round_id']}" / task_id / "driver-input.json"
    value = {
            "schema_version": 1,
            "kind": "r015_c_only_trial_driver_input",
            "protocol_id": protocol.config["protocol_id"],
            "round_id": assignment["round_id"],
            "condition": "C-only",
            "task": deepcopy(protocol._task(task_id)),
            "assignment": deepcopy(assignment),
            "config": {
                "path": str(protocol.config_path.resolve()),
                "sha256": sha256_file(protocol.config_path),
                "task_artifact_audit": deepcopy(protocol.config.get("task_artifact_audit")),
                "runtime_alignment": deepcopy(protocol.config.get("runtime_alignment")),
                "retrieval_profile": deepcopy(protocol.config.get("retrieval_profile")),
                "driver": deepcopy(protocol.config.get("driver", {})),
            },
            "state": {"path": str(state_path.resolve()), "sha256": sha256_file(state_path)},
            "round_material": {
                "trajectory_pool": deepcopy(protocol._round().get("trajectory_pool", [])),
                "description_pool": deepcopy(protocol._round().get("description_pool", [])),
                "task_candidate_pool": deepcopy(protocol._round().get("task_candidate_pool", [])),
                "bank": deepcopy(assignment.get("frozen_bank")),
            },
            "historical_baseline_used": False,
            "output_contract": {
                "path": str(path.with_name("driver-output.json")),
                "required_keys": ["trial", "extraction", "publication"],
                "trial_required_keys": ["condition", "round_id", "task_id", "outcome", "trajectory", "supplied_skills", "raw_evidence"],
                "extraction_required_keys": ["condition", "round_id", "task_id", "decision", "candidates", "trajectory_ref", "evidence"],
                "publication_required_keys": ["condition", "round_id", "task_id", "operations", "manager_decisions"],
                "event_attempts": "optional ordered records; invalid model outputs are retained and do not stop unless skip/duplicate",
                "official_trial_required": True,
                "fixture_or_replay_evidence_forbidden": True,
            },
        }
    if path.exists():
        existing = read_json(path)
        # The initial state digest is part of the immutable launch intent, but
        # the coordinator legitimately changes the state after applying a
        # completed trial phase.  Keep the original input bytes (and therefore
        # its process/output hash) stable while allowing only this expected
        # state-file digest to move on a later phase resume.
        comparable_existing = deepcopy(existing)
        comparable_value = deepcopy(value)
        if isinstance(comparable_existing, dict) and isinstance(comparable_value, dict):
            existing_state = comparable_existing.get("state")
            value_state = comparable_value.get("state")
            if isinstance(existing_state, dict) and isinstance(value_state, dict) and existing_state.get("path") == value_state.get("path"):
                existing_state.pop("sha256", None)
                value_state.pop("sha256", None)
            # ``extract_after_task`` durably appends the current task's own
            # trajectory/description to the round pools before publication.
            # Those records are derived from this same frozen assignment and
            # are expected to appear on a phase-aware resume, whereas every
            # prior-task record is immutable launch material.  Compare the
            # latter while leaving the protocol's own trajectory/hash checks
            # responsible for the derived current-task records.
            for material in (comparable_existing.get("round_material"), comparable_value.get("round_material")):
                if isinstance(material, dict):
                    material.setdefault("task_candidate_pool", [])
                    for pool_name in ("trajectory_pool", "description_pool", "task_candidate_pool"):
                        pool = material.get(pool_name)
                        if isinstance(pool, list):
                            material[pool_name] = [
                                item
                                for item in pool
                                if not isinstance(item, dict) or item.get("task_id") != task_id
                            ]
        if canonical_json(comparable_existing) != canonical_json(comparable_value):
            raise COnlyProtocolError(f"driver input already exists with different binding: {path}")
    else:
        write_json(path, value)
    return path


def _load_clean_driver_output(
    input_path: Path,
    output_path: Path,
    assignment: dict[str, Any],
    *,
    process_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    process_path = process_path or _driver_process_path(input_path)
    if not process_path.is_file():
        if output_path.exists():
            _mark_reconcile(input_path, assignment=assignment, phase="driver_output_without_process", error="driver output exists without a durable completed process record")
            raise COnlyProtocolError(f"driver output has no durable process record; reconcile {input_path.parent}")
        raise COnlyProtocolError(f"no durable driver process record exists: {process_path}")
    process = _object(read_json(process_path), field="driver-process")
    if process.get("condition") != "C-only" or process.get("round_id") != assignment["round_id"] or process.get("task_id") != assignment["task_id"] or process.get("trial_id") != assignment["trial_id"]:
        raise COnlyProtocolError("driver process record is bound to a different C-only assignment")
    if process.get("input_path") != str(input_path) or process.get("output_path") != str(output_path):
        _mark_reconcile(
            input_path,
            assignment=assignment,
            phase="driver_process_path_mismatch",
            error="driver process record paths differ from the requested durable input/output",
            process=process,
        )
        raise COnlyProtocolError("driver process input/output paths differ from the durable assignment")
    if process.get("status") != "succeeded" or process.get("returncode") != 0 or process.get("timed_out") is True:
        _mark_reconcile(input_path, assignment=assignment, phase="driver_process_not_successful", error=process.get("error", "driver process did not finish successfully"), process=process)
        raise COnlyProtocolError("driver process failed or is uncertain; automatic retry is disabled")
    input_hash = sha256_file(input_path)
    if process.get("input_sha256") != input_hash:
        raise COnlyProtocolError("driver process input hash differs from the durable driver input")
    if not output_path.is_file():
        _mark_reconcile(input_path, assignment=assignment, phase="driver_output_missing_after_success", error="successful driver process has no output file", process=process)
        raise COnlyProtocolError(f"successful driver process has no output; reconcile before continuing: {output_path}")
    output_hash = sha256_file(output_path)
    if process.get("output_sha256") != output_hash:
        _mark_reconcile(input_path, assignment=assignment, phase="driver_output_hash_mismatch", error="driver output bytes differ from the durable process record", process=process)
        raise COnlyProtocolError("driver output hash mismatch; automatic retry is disabled")
    return _driver_result(output_path), process


def _reconciliation_manifest_ref(path: Path) -> dict[str, str]:
    path = Path(path).resolve()
    if not path.is_file():
        raise COnlyProtocolError(f"manager reconciliation manifest is not a file: {path}")
    return {"path": str(path), "sha256": sha256_file(path)}


def _harbor_recovery_launch(path: Path) -> dict[str, Any]:
    """Load the fixed paths for one original-bound Harbor artifact recovery.

    The manifest is the only authority for the recovery output namespace.  In
    particular, the coordinator must not derive a new output beside the
    original driver input: doing so would make a failed original process look
    like an ordinary retry.  This helper performs the cheap structural check
    before writing a new process intent; assignment and original artifact
    binding are checked by the Harbor driver itself.
    """
    path = Path(path).resolve()
    try:
        manifest = load_harbor_recovery_manifest(path)
    except (HarborRecoveryError, OSError, ValueError, KeyError) as error:
        raise COnlyProtocolError(f"Harbor recovery manifest is not valid: {error}") from error
    recovery = _object(manifest.get("recovery"), field="recovery.recovery")
    root = Path(_nonempty(recovery.get("root"), field="recovery.recovery.root")).resolve()
    output = _object(recovery.get("output"), field="recovery.recovery.output")
    process = _object(recovery.get("process"), field="recovery.recovery.process")
    output_path = Path(_nonempty(output.get("path"), field="recovery.recovery.output.path")).resolve()
    process_path = Path(_nonempty(process.get("path"), field="recovery.recovery.process.path")).resolve()
    if output_path != root / "driver-output.json" or process_path != root / "driver-recovery-process.json":
        raise COnlyProtocolError("Harbor recovery manifest uses noncanonical output/process paths")
    return {
        "path": path,
        "sha256": sha256_file(path),
        "manifest": manifest,
        "root": root,
        "output_path": output_path,
        "process_path": process_path,
    }


def _check_existing_reconciliation_process(
    process_path: Path,
    *,
    reconciliation_manifest_path: Path | None,
    output_path: Path,
) -> None:
    """Refuse to reuse a continuation artifact bound to another audit."""
    if reconciliation_manifest_path is None:
        return
    expected = _reconciliation_manifest_ref(reconciliation_manifest_path)
    if not process_path.is_file():
        if output_path.is_file():
            raise COnlyProtocolError(
                "a reconciled driver output exists without its durable reconciliation process record"
            )
        return
    process = _object(read_json(process_path), field="driver-process")
    actual = process.get("reconciliation_manifest")
    if not isinstance(actual, dict) or actual.get("path") != expected["path"] or actual.get("sha256") != expected["sha256"]:
        raise COnlyProtocolError(
            "existing continuation process is bound to a different reconciliation manifest; manual reconciliation required"
        )


def _check_existing_harbor_recovery_process(
    process_path: Path,
    *,
    recovery: dict[str, Any],
    input_path: Path,
    output_path: Path,
    assignment: dict[str, Any],
) -> None:
    """Validate an existing no-rerun Harbor recovery process boundary.

    A recovery process is deliberately separate from the original failed
    ``driver-process.json``.  Reusing it is safe only after its manifest,
    assignment, input/output paths, and terminal status all match the exact
    manifest selected by the coordinator.  A failed/running process remains
    an operator-visible stop; this helper never turns it into an automatic
    replay.
    """
    if not process_path.is_file():
        if output_path.is_file():
            raise COnlyProtocolError(
                "a Harbor recovery output exists without its durable recovery process record"
            )
        return
    process = _object(read_json(process_path), field="Harbor recovery process")
    expected_ref = {"path": str(recovery["path"]), "sha256": recovery["sha256"]}
    actual = process.get("harbor_recovery_manifest")
    if not isinstance(actual, dict) or actual.get("path") != expected_ref["path"] or actual.get("sha256") != expected_ref["sha256"]:
        raise COnlyProtocolError(
            "existing Harbor recovery process is bound to a different recovery manifest; manual reconciliation required"
        )
    for field, expected in (
        ("condition", "C-only"),
        ("round_id", assignment["round_id"]),
        ("task_id", assignment["task_id"]),
        ("trial_id", assignment["trial_id"]),
        ("input_path", str(input_path)),
        ("output_path", str(output_path)),
    ):
        if process.get(field) != expected:
            raise COnlyProtocolError(f"existing Harbor recovery process differs at {field}; manual reconciliation required")
    status = process.get("status")
    if status != "succeeded" or process.get("returncode") != 0 or process.get("timed_out") is True:
        raise COnlyProtocolError("Harbor recovery process failed or is uncertain; automatic retry is disabled")


def _apply_pending_task_extraction_handoff(
    *, protocol: COnlyProtocol, assignment: dict[str, Any],
    input_path: Path, output_path: Path, manager_root: Path | None,
    allow_test_fixture: bool,
) -> bool:
    handoff_path = output_path.with_name("extraction-awaiting-ack.json")
    ack_path = output_path.with_name("extraction-ack.json")
    if not handoff_path.is_file():
        return False
    if ack_path.exists():
        return True
    handoff = _object(read_json(handoff_path), field="Task extraction handoff")
    stage_path = output_path.with_name("driver-stage-extraction.json")
    if handoff.get("kind") != "r015_task_extraction_handoff_v1" or \
            any(handoff.get(key) != assignment[key] for key in ("round_id", "task_id", "trial_id")) or \
            handoff.get("input_sha256") != sha256_file(input_path) or \
            handoff.get("extraction_stage_path") != str(stage_path) or \
            not stage_path.is_file() or handoff.get("extraction_stage_sha256") != sha256_file(stage_path):
        raise COnlyProtocolError("Task extraction handoff differs from its immutable driver stage")
    frozen_state_ref = _input_frozen_state_ref(input_path)
    payload = _load_driver_stage(
        input_path, assignment=assignment, phase="extraction",
        strict_manager=not allow_test_fixture, manager_root=manager_root,
        frozen_state_ref=frozen_state_ref, stage_path=stage_path,
    )
    if payload is None:
        raise COnlyProtocolError("Task extraction handoff has no completed stage")
    state_path = frozen_state_ref[0]
    _apply_driver_output(
        protocol, _stage_output(input_path, assignment=assignment,
                                phase="extraction", payload=payload),
        assignment, state_path, phase="extraction",
        manager_root=manager_root, allow_test_fixture=allow_test_fixture,
        frozen_state_ref=frozen_state_ref,
    )
    if "extraction" not in protocol._assignment(assignment["task_id"]):
        raise COnlyProtocolError("coordinator did not durably save Task extraction")
    write_json(ack_path, {"kind": "r015_task_extraction_ack_v1",
                          "handoff_sha256": sha256_file(handoff_path),
                          "durable_state_sha256": sha256_file(state_path)})
    return True


def _invoke_driver(
    *,
    driver: Path,
    input_path: Path,
    output_path: Path,
    assignment: dict[str, Any],
    protocol: COnlyProtocol,
    continue_from_trial: bool = False,
    continue_from_extraction: bool = False,
    reconciliation_manifest_path: Path | None = None,
    harbor_recovery_manifest_path: Path | None = None,
    manager_root: Path | None = None,
    allow_test_fixture: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if reconciliation_manifest_path is not None and not continue_from_trial:
        raise COnlyProtocolError("a manager reconciliation manifest requires a trial continuation")
    if continue_from_extraction and (continue_from_trial or reconciliation_manifest_path is not None or harbor_recovery_manifest_path is not None):
        raise COnlyProtocolError("extraction continuation cannot be combined with another recovery mode")
    if harbor_recovery_manifest_path is not None and (continue_from_trial or reconciliation_manifest_path is not None):
        raise COnlyProtocolError("a Harbor artifact recovery cannot be combined with trial continuation or manager reconciliation")
    recovery = (
        _harbor_recovery_launch(harbor_recovery_manifest_path)
        if harbor_recovery_manifest_path is not None
        else None
    )
    reconciliation_manifest_ref = (
        _reconciliation_manifest_ref(reconciliation_manifest_path)
        if reconciliation_manifest_path is not None
        else None
    )
    recovery_manifest_ref = (
        {"path": str(recovery["path"]), "sha256": recovery["sha256"]}
        if recovery is not None
        else None
    )
    if recovery is not None:
        manifest_input = _object(recovery["manifest"].get("input"), field="recovery.input")
        if Path(_nonempty(manifest_input.get("path"), field="recovery.input.path")).resolve() != input_path.resolve():
            raise COnlyProtocolError("Harbor recovery manifest input path differs from the requested driver input")
        if recovery["output_path"] != output_path.resolve():
            raise COnlyProtocolError("Harbor recovery manifest output path differs from the requested driver output")
    command = _driver_command(
        driver,
        input_path,
        output_path,
        continue_from_trial=continue_from_trial,
        continue_from_extraction=continue_from_extraction,
        reconciliation_manifest_path=reconciliation_manifest_path,
        harbor_recovery_manifest_path=harbor_recovery_manifest_path,
    )
    process_path = (
        recovery["process_path"]
        if recovery is not None
        else _driver_continuation_process_path(input_path)
        if continue_from_trial or continue_from_extraction
        else _driver_process_path(input_path)
    )
    if process_path.exists() or output_path.exists():
        if recovery is not None:
            _check_existing_harbor_recovery_process(
                process_path,
                recovery=recovery,
                input_path=input_path,
                output_path=output_path,
                assignment=assignment,
            )
        else:
            _check_existing_reconciliation_process(
                process_path,
                reconciliation_manifest_path=reconciliation_manifest_path,
                output_path=output_path,
            )
        return _load_clean_driver_output(input_path, output_path, assignment, process_path=process_path)
    started = utc_now()
    intent = {
        "schema_version": 1,
        "kind": "r015_c_only_driver_process",
        "status": "running",
        "condition": "C-only",
        "round_id": assignment["round_id"],
        "task_id": assignment["task_id"],
        "trial_id": assignment["trial_id"],
        "command": command,
        "input_path": str(input_path),
        "input_sha256": sha256_file(input_path),
        "output_path": str(output_path),
        "continuation_from": (
            "original_failed_harbor_trial_artifact_recovery"
            if recovery is not None
            else "completed_trial_stage"
            if continue_from_trial
            else "completed_extraction_stage"
            if continue_from_extraction
            else None
        ),
        "reconciliation_manifest": deepcopy(reconciliation_manifest_ref),
        "harbor_recovery_manifest": deepcopy(recovery_manifest_ref),
        "harbor_recovery_process_path": str(process_path) if recovery is not None else None,
        "started_at_utc": started,
        "timeout_seconds": _configured_outer_timeout(protocol),
    }
    process_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(process_path, intent)
    process: subprocess.Popen[str] | None = None
    stdout = ""
    stderr = ""
    timed_out = False
    returncode: int | None = None
    error: BaseException | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""),
                 "CODESKILL_EXTRACT_HANDOFF": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_process_group_kwargs(),
        )
        timeout = _configured_outer_timeout(protocol)
        try:
            deadline = time.monotonic() + timeout if timeout is not None else None
            while True:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = process.communicate(
                        timeout=min(0.25, remaining) if remaining is not None else 0.25)
                    break
                except subprocess.TimeoutExpired:
                    _apply_pending_task_extraction_handoff(
                        protocol=protocol, assignment=assignment,
                        input_path=input_path, output_path=output_path,
                        manager_root=manager_root,
                        allow_test_fixture=allow_test_fixture,
                    )
        except subprocess.TimeoutExpired as timeout_error:
            timed_out = True
            error = timeout_error
            _terminate_process_group(process)
            stdout, stderr = process.communicate()
        returncode = process.returncode
    except BaseException as caught:
        error = caught
        if process is not None and process.poll() is None:
            _terminate_process_group(process)
            stdout, stderr = process.communicate()
        returncode = process.returncode if process is not None else None
    process_record = {
        **intent,
        "status": "timed_out" if timed_out else ("succeeded" if error is None and returncode == 0 else "failed"),
        "finished_at_utc": utc_now(),
        "returncode": returncode,
        "timed_out": timed_out,
        "stdout": stdout,
        "stderr": stderr,
        "error_type": type(error).__name__ if error is not None else None,
        "error": str(error) if error is not None else (None if returncode == 0 else f"driver returned {returncode}"),
        "output_sha256": sha256_file(output_path) if output_path.is_file() else None,
    }
    write_json(process_path, process_record)
    if process_record["status"] != "succeeded":
        _mark_reconcile(input_path, assignment=assignment, phase="driver_process_failed", error=process_record["error"] or "driver failed", process=process_record)
        raise COnlyProtocolError("official C-only driver failed; current task is stopped for reconciliation")
    try:
        output, clean_process = _load_clean_driver_output(
            input_path,
            output_path,
            assignment,
            process_path=process_path,
        )
    except BaseException as caught:
        _mark_reconcile(input_path, assignment=assignment, phase="driver_output_validation", error=caught, process=process_record)
        raise
    return output, clean_process


def _confirm_task_graph_publication(
    *, protocol: COnlyProtocol, assignment: dict[str, Any],
    output: dict[str, Any], state_path: Path,
) -> None:
    """Confirm the sole durable coordinator commit at Graph's second interrupt.

    This reads the saved protocol state; a staged driver operation cannot
    satisfy the receipt. Repeating after a crash is idempotent at the graph
    checkpoint and cannot call a model or replay a bank operation.
    """
    extraction = protocol._assignment(assignment["task_id"]).get("extraction", {})
    marker = extraction.get("evidence", {}).get("task", {}).get("graph")
    if marker is None:
        return
    if not isinstance(marker, dict) or marker.get("kind") != "r015_task_graph_v1":
        raise COnlyProtocolError("durable Task extraction has an invalid graph marker")
    if "publication" not in protocol._assignment(assignment["task_id"]):
        raise COnlyProtocolError("Task Graph cannot confirm before durable publication")
    from codeskill_rebuild.task_graph import TaskGraphRunner, task_thread_id
    from codeskill_rebuild.task_graph_stages import PublicationReceiptService

    publication = _object(output.get("publication"), field="Task Graph driver publication")
    operations = publication.get("operations")
    if not isinstance(operations, list):
        raise COnlyProtocolError("Task Graph publication has no ordered operations")
    published = protocol._assignment(assignment["task_id"])["publication"]
    ordered_ids = [item.get("operation_id") for item in operations]
    if ordered_ids != [item.get("operation_id") for item in published["operations"]]:
        raise COnlyProtocolError("Task Graph output operations differ from durable publication")
    runner = TaskGraphRunner(directory=Path(marker["directory"]),
                             service=PublicationReceiptService())
    identity = marker["identity"]
    if marker["thread_id"] != task_thread_id(identity):
        raise COnlyProtocolError("Task Graph marker identity changed")
    # The first three hashes are checked against the persisted Task receipt
    # inside the graph, then HM and the ordered operations are checked against
    # the saved protocol bank and journal.
    from codeskill_rebuild.task_graph import read_task_graph_state
    state = read_task_graph_state(directory=Path(marker["directory"]), identity=identity)
    if state.get("stage") == "publication_confirmed":
        return
    task_receipt = _object(state.get("task_receipt"), field="Task Graph staged receipt")
    receipt = {"thread_id": marker["thread_id"],
               "H0": task_receipt["H0"], "HE": task_receipt["HE"],
               "HT": task_receipt["HT"],
               "HM": published["after_bank_state_sha256"],
               "maintenance_operations": [item for item in operations
                                          if item.get("source_kind") == "maintenance"],
               "ordered_operation_ids": ordered_ids,
               "durable_state_path": str(state_path)}
    confirmed = runner.invoke(identity=identity, receipt=receipt)
    if confirmed.get("stage") != "publication_confirmed":
        raise COnlyProtocolError("Task Graph did not checkpoint durable publication")


def _apply_driver_output(
    protocol: COnlyProtocol,
    output: dict[str, Any],
    assignment: dict[str, Any],
    state_path: Path,
    *,
    phase: str = "complete",
    manager_root: Path | None = None,
    allow_test_fixture: bool = False,
    frozen_state_ref: tuple[Path, str] | None = None,
) -> None:
    task_id = assignment["task_id"]
    try:
        _validate_driver_payload_refs(
            output,
            field="driver-output",
            strict_manager=not allow_test_fixture,
            manager_root=manager_root,
            frozen_state_ref=frozen_state_ref,
        )
        trial = _object(output.get("trial"), field="driver-output.trial")
        extraction_value = output.get("extraction")
        publication_value = output.get("publication")
        extraction = _object(extraction_value, field="driver-output.extraction") if phase in {"extraction", "publication", "complete"} else None
        publication = _object(publication_value, field="driver-output.publication") if phase in {"publication", "complete"} else None
        records = [("trial", trial)]
        if extraction is not None:
            records.append(("extraction", extraction))
        if publication is not None:
            records.append(("publication", publication))
        for label, record in records:
            if record.get("condition") != "C-only" or record.get("round_id") != assignment["round_id"] or record.get("task_id") != task_id:
                raise COnlyProtocolError(f"driver-output.{label} must be explicitly bound to this C-only round/task")
        raw_evidence = _object(trial.get("raw_evidence"), field="driver-output.trial.raw_evidence")
        if raw_evidence.get("official_harbor_trial") is not True or raw_evidence.get("historical_baseline_used") is True:
            raise COnlyProtocolError("driver output must identify an official Harbor trial and reject fixture/replay evidence")
        if not allow_test_fixture and output.get("evidence_mode") != "official_live":
            raise COnlyProtocolError(
                "production C-only output must carry evidence_mode=official_live; "
                "controlled fixtures require the explicit test-only boundary"
            )
        if not allow_test_fixture and raw_evidence.get("session_id") != _expected_session_id(assignment["trial_id"]):
            raise COnlyProtocolError(
                "production C-only output session_id is not the deterministic frozen assignment session"
            )
        # Always replay the durable trial operation, even when the state says
        # that this phase exists.  The coordinator compares the complete
        # payload hash and therefore rejects changed driver output instead of
        # silently accepting a newly written result over the old one.
        protocol.record_trial(
            task_id,
            outcome=trial.get("outcome"),
            trajectory=trial.get("trajectory"),
            supplied_skills=trial.get("supplied_skills", []),
            raw_evidence=raw_evidence,
        )
        protocol.save(state_path)
        if phase == "trial":
            return
        event_attempts = output.get("event_attempts", [])
        if not isinstance(event_attempts, list):
            raise COnlyProtocolError("driver-output.event_attempts must be a list")
        for index, attempt in enumerate(event_attempts, start=1):
            item = _object(attempt, field=f"driver-output.event_attempts[{index - 1}]")
            if item.get("attempt_no") != index:
                raise COnlyProtocolError("driver event attempts must be numbered contiguously")
            protocol.record_event_attempt(
                task_id,
                attempt_no=index,
                outcome=item.get("outcome"),
                candidate=item.get("candidate"),
                raw_response=item.get("raw_response"),
            )
        if event_attempts:
            protocol.save(state_path)
        if "extraction" not in protocol._assignment(task_id):
            if extraction is None:
                raise COnlyProtocolError("driver output has no extraction phase for the current assignment")
            protocol.extract_after_task(
                task_id,
                candidates=extraction.get("candidates", []),
                trajectory_ref=extraction.get("trajectory_ref", trial.get("trajectory")),
                extraction_evidence=extraction.get("evidence", extraction),
                description_records=extraction.get("description_records", []),
                task_candidate_records=extraction.get("task_candidate_records", []),
                decision=extraction.get("decision", "extract"),
                reason=extraction.get("reason"),
            )
            protocol.save(state_path)
        if phase == "extraction":
            return
        if "publication" not in protocol._assignment(task_id):
            if publication is None:
                raise COnlyProtocolError("driver output has no publication phase for the current assignment")
            protocol.publish_after_task(
                task_id,
                operations=publication.get("operations", []),
                manager_decisions=publication.get("manager_decisions", []),
            )
            protocol.save(state_path)
        _confirm_task_graph_publication(
            protocol=protocol, assignment=assignment, output=output,
            state_path=state_path,
        )
        if task_id not in protocol._round().get("completed_tasks", {}):
            protocol.finish_task(task_id)
            protocol.save(state_path)
    except BaseException:
        # Any phase failure leaves the current assignment and all raw driver
        # output in place.  The caller records a reconciliation marker and
        # stops the ordered campaign; only a valid explicit trial
        # infrastructure result is allowed to advance.
        try:
            protocol.save(state_path)
        except BaseException:
            pass
        raise


def _recover_driver_stages(
    protocol: COnlyProtocol,
    *,
    assignment: dict[str, Any],
    input_path: Path,
    state_path: Path,
    manager_root: Path | None = None,
    allow_test_fixture: bool = False,
) -> str | None:
    """Apply an already completed built-in phase without launching a child.

    A running or missing process record is ambiguous even when a phase file is
    present.  The caller must reconcile that boundary manually.  Once a phase
    and its process are both durable, applying it is idempotent through the
    coordinator journals and never repeats the Harbor/model work.
    """
    process_path = _driver_process_path(input_path)
    present = [phase for phase in _DRIVER_PHASES if _driver_stage_path(input_path, phase).is_file()]
    if not present:
        return None
    if not process_path.is_file():
        _mark_reconcile(input_path, assignment=assignment, phase="driver_stage_without_process", error="a durable phase exists without a process record")
        raise COnlyProtocolError("driver stage exists without a durable process record; reconcile before resuming")
    process = _object(read_json(process_path), field="driver-process")
    status = process.get("status")
    if status == "running" or status in {"timed_out", "uncertain"}:
        _mark_reconcile(input_path, assignment=assignment, phase="driver_stage_inflight", error="driver process is running or uncertain; completed phase cannot be trusted", process=process)
        raise COnlyProtocolError("driver process is running or uncertain; reconcile before resuming")
    if status not in {"succeeded", "failed"}:
        _mark_reconcile(input_path, assignment=assignment, phase="driver_stage_unknown_process", error="driver process has an unknown durable status", process=process)
        raise COnlyProtocolError("driver process status is unknown; reconcile before resuming")
    if status == "failed":
        # A phase file written before a later manager/publication failure is
        # useful forensic evidence, but it is not an automatic commit point.
        # Applying it could turn a broken driver into a task advance and could
        # also leave the coordinator believing that a paid phase completed
        # when the child process did not exit successfully.  Preserve all
        # stage bytes and require an explicit reconciliation decision.
        _mark_reconcile(
            input_path,
            assignment=assignment,
            phase="driver_stage_after_failed_process",
            error="durable driver phase exists but its process failed; no phase is applied automatically",
            process=process,
        )
        raise COnlyProtocolError("driver process failed; durable phases require reconciliation before application")
    if process.get("returncode") != 0 or process.get("timed_out") is True:
        _mark_reconcile(
            input_path,
            assignment=assignment,
            phase="driver_stage_process_returncode",
            error="durable phase has a non-success process returncode or timeout marker",
            process=process,
        )
        raise COnlyProtocolError("driver phase process was not a clean success; reconcile before application")
    if process.get("input_path") != str(input_path) or process.get("input_sha256") != sha256_file(input_path):
        _mark_reconcile(input_path, assignment=assignment, phase="driver_stage_process_input_mismatch", error="durable process input binding changed", process=process)
        raise COnlyProtocolError("durable driver process input binding changed; reconcile before application")
    output_path = input_path.with_name("driver-output.json")
    if process.get("output_path") != str(output_path) or not output_path.is_file():
        _mark_reconcile(input_path, assignment=assignment, phase="driver_stage_process_output_missing", error="durable process has no matching final driver output", process=process)
        raise COnlyProtocolError("durable phase process has no matching final output; reconcile before application")
    if process.get("output_sha256") != sha256_file(output_path):
        _mark_reconcile(input_path, assignment=assignment, phase="driver_stage_process_output_mismatch", error="durable final driver output hash changed", process=process)
        raise COnlyProtocolError("durable final driver output hash changed; reconcile before application")
    # Phase payloads keep the launch-time state identity.  The coordinator
    # may have advanced its timestamp/state hash before this recovery pass;
    # permit only the exact state path/hash captured by this immutable input.
    frozen_state_ref = _input_frozen_state_ref(input_path)
    ordinal = {name: index for index, name in enumerate(_DRIVER_PHASES)}
    highest = max(present, key=ordinal.__getitem__)
    expected_prefix = _DRIVER_PHASES[: ordinal[highest] + 1]
    if any(not _driver_stage_path(input_path, phase).is_file() for phase in expected_prefix):
        _mark_reconcile(input_path, assignment=assignment, phase="driver_stage_gap", error="durable driver phases are not contiguous", process=process)
        raise COnlyProtocolError("durable driver phases have a gap; reconcile before resuming")
    stage_payloads: dict[str, dict[str, Any]] = {}
    for phase in expected_prefix:
        loaded = _load_driver_stage(
            input_path,
            assignment=assignment,
            phase=phase,
            strict_manager=not allow_test_fixture,
            manager_root=manager_root,
            frozen_state_ref=frozen_state_ref,
        )
        if loaded is None:
            raise COnlyProtocolError(f"durable driver phase disappeared while resuming: {phase}")
        stage_payloads[phase] = loaded
    # Each stage repeats the immutable trial packet.  Checking the repeated
    # bytes here prevents a caller from combining a valid trial with a
    # different extraction/publication payload after a crash.  The state
    # machine still revalidates every operation when the highest stage is
    # applied, but it must never be asked to infer that the lower phase was
    # the same paid run.
    trial_hash: str | None = None
    for phase, stage in stage_payloads.items():
        trial = stage.get("trial")
        if not isinstance(trial, dict):
            raise COnlyProtocolError(f"durable driver phase {phase} has no trial payload")
        current_hash = sha256_text(canonical_json(trial))
        if trial_hash is None:
            trial_hash = current_hash
        elif current_hash != trial_hash:
            _mark_reconcile(input_path, assignment=assignment, phase="driver_stage_trial_mismatch", error="durable driver phases contain different trial payloads", process=process)
            raise COnlyProtocolError("durable driver phases contain different trial payloads; reconcile before resuming")
    # The stage wrapper and the final output are separate durable artifacts.
    # Verify the completed process output itself, then compare every repeated
    # field before applying a stage.  This prevents recovery from accepting a
    # validly hashed stage whose trial/extraction/publication payload differs
    # from the bytes the child process reported as its final output.
    final_output, _final_process = _load_clean_driver_output(input_path, output_path, assignment)
    _validate_driver_payload_refs(
        final_output,
        field="driver-output.recovery",
        strict_manager=not allow_test_fixture,
        manager_root=manager_root,
        frozen_state_ref=frozen_state_ref,
    )
    for phase, stage in stage_payloads.items():
        for key, expected_value in stage.items():
            if key not in final_output or canonical_json(final_output[key]) != canonical_json(expected_value):
                _mark_reconcile(
                    input_path,
                    assignment=assignment,
                    phase="driver_stage_final_output_mismatch",
                    error=f"final driver output field {key!r} differs from durable {phase} stage",
                    process=process,
                )
                raise COnlyProtocolError(
                    f"final driver output differs from durable {phase} stage at {key!r}; reconcile before application"
                )
    extraction = stage_payloads.get("extraction", {}).get("extraction")
    publication = stage_payloads.get("publication", {}).get("publication")
    if extraction is not None and not isinstance(extraction, dict):
        raise COnlyProtocolError("durable extraction phase has an invalid extraction payload")
    if publication is not None and not isinstance(publication, dict):
        raise COnlyProtocolError("durable publication phase has an invalid publication payload")
    # Stage files are immutable and remain after the coordinator applies a
    # phase.  Determine the highest phase already present in state so a second
    # resume does not repeatedly apply the same stage (or loop forever).  A
    # complete final output can still be replayed idempotently for any missing
    # coordinator phases through the normal invocation path.
    current_assignment = protocol._assignment(assignment["task_id"])
    applied_phase: str | None = None
    if current_assignment.get("status") == "finished":
        if "publication" in current_assignment:
            applied_phase = "publication"
        elif "extraction" in current_assignment:
            applied_phase = "extraction"
        elif isinstance(current_assignment.get("trial_evidence"), dict):
            applied_phase = "trial"
    if applied_phase is not None and ordinal[applied_phase] >= ordinal[highest]:
        return None
    payload = stage_payloads[highest]
    _apply_driver_output(
        protocol,
        _stage_output(input_path, assignment=assignment, phase=highest, payload=payload),
        assignment,
        state_path,
        phase=highest,
        manager_root=manager_root,
        allow_test_fixture=allow_test_fixture,
        frozen_state_ref=frozen_state_ref,
    )
    if highest != "publication":
        _mark_reconcile(input_path, assignment=assignment, phase=f"after_{highest}_phase", error=f"driver stopped before the next durable phase after {highest}", process=process)
        raise COnlyProtocolError(f"driver stopped after durable {highest} phase; next phase requires reconciliation")
    return highest


def _run_driver(
    protocol: COnlyProtocol,
    driver: Path,
    run_dir: Path,
    state_path: Path,
    *,
    allow_test_fixture: bool = False,
    reconciliation_manifest_path: Path | None = None,
    harbor_recovery_manifest_path: Path | None = None,
    stop_after_first_round: bool = False,
    continue_from_extraction: bool = False,
) -> None:
    manager_root = _trusted_manager_root(run_dir)
    reconciliation_manifest_path = (
        Path(reconciliation_manifest_path).resolve()
        if reconciliation_manifest_path is not None
        else None
    )
    reconciliation_trial: dict[str, Any] | None = None
    if reconciliation_manifest_path is not None:
        try:
            reconciliation = load_reconciliation_manifest(reconciliation_manifest_path)
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            raise COnlyProtocolError(f"manager reconciliation manifest is not valid: {error}") from error
        reconciliation_trial = _object(reconciliation.get("trial"), field="reconciliation.trial")
    harbor_recovery_launch: dict[str, Any] | None = None
    if harbor_recovery_manifest_path is not None:
        if reconciliation_manifest_path is not None:
            raise COnlyProtocolError(
                "Harbor artifact recovery cannot be combined with manager reconciliation"
            )
        harbor_recovery_launch = _harbor_recovery_launch(harbor_recovery_manifest_path)
        recovery_value = _object(harbor_recovery_launch["manifest"].get("recovery"), field="recovery.recovery")
        recovery_manager_root = Path(
            _nonempty(recovery_value.get("manager_root"), field="recovery.recovery.manager_root")
        ).resolve()
        if recovery_manager_root != manager_root.resolve():
            raise COnlyProtocolError(
                "Harbor recovery manager root differs from the trusted coordinator execution namespace"
            )
    while True:
        task_id = protocol.current_task_id
        if task_id is None:
            if reconciliation_manifest_path is not None or harbor_recovery_launch is not None:
                raise COnlyProtocolError(
                    "explicit recovery has no matching current assignment; it cannot advance a round or task"
                )
            if protocol.current_round_id == 1:
                if stop_after_first_round:
                    # The diagnostic entrypoint owns a four-task first-round
                    # boundary; it must never schedule an implicit round 2.
                    return
                protocol.start_next_round()
                protocol.save(state_path)
                continue
            if protocol.state["formal_campaign"] == "active":
                protocol.mark_complete()
                protocol.save(state_path)
            return
        assignment = protocol.freeze_task(task_id)
        if reconciliation_trial is not None:
            for field in ("round_id", "task_id", "trial_id"):
                if reconciliation_trial.get(field) != assignment.get(field):
                    raise COnlyProtocolError(
                        f"manager reconciliation is bound to a different current assignment at {field}"
                    )
        protocol.save(state_path)
        input_path = _write_driver_input(protocol, assignment, run_dir, state_path)
        output_path = (
            harbor_recovery_launch["output_path"]
            if harbor_recovery_launch is not None
            else input_path.with_name("driver-output.json")
        )
        if harbor_recovery_launch is not None:
            manifest_input = _object(harbor_recovery_launch["manifest"].get("input"), field="recovery.input")
            expected_input = Path(
                _nonempty(manifest_input.get("path"), field="recovery.input.path")
            ).resolve()
            manifest_assignment = _object(
                harbor_recovery_launch["manifest"].get("assignment"), field="recovery.assignment"
            )
            if expected_input != input_path.resolve():
                raise COnlyProtocolError(
                    "Harbor recovery manifest is bound to a different current driver input"
                )
            for field in ("condition", "round_id", "task_id", "trial_id", "frozen_bank_state_sha256"):
                if manifest_assignment.get(field) != assignment.get(field):
                    raise COnlyProtocolError(
                        f"Harbor recovery manifest differs from the current assignment at {field}"
                    )
        # The input captures one immutable launch boundary.  Later coordinator
        # saves may change only the mutable state digest; recovery validation
        # receives this exact binding so stale cross-run refs remain rejected.
        frozen_state_ref = _input_frozen_state_ref(input_path)
        try:
            # A state transition already applied by a prior invocation owns
            # the corresponding phase.  Complete assignments can advance
            # directly; partially applied assignments can consume only the
            # built-in driver's verified phase files.  Neither path launches
            # another paid Harbor/model process.
            current_assignment = protocol._assignment(task_id)
            if (
                harbor_recovery_launch is not None
                and current_assignment.get("status") == "finished"
                and "publication" in current_assignment
            ):
                # An explicit Harbor artifact recovery is a caller-selected
                # boundary. Never silently ignore it because a prior
                # invocation happened to apply the assignment before the
                # recovery process was reconciled; doing so would conceal a
                # stale/replayed recovery manifest.
                raise COnlyProtocolError(
                    "Harbor recovery manifest targets an already-applied assignment; "
                    "manual reconciliation is required"
                )
            if current_assignment.get("status") == "finished" and "publication" in current_assignment:
                if task_id not in protocol._round().get("completed_tasks", {}):
                    task_graph_marker = current_assignment.get("extraction", {}).get("evidence", {}).get("task", {}).get("graph")
                    if task_graph_marker is not None:
                        process_for_output = (
                            _driver_continuation_process_path(input_path)
                            if _driver_continuation_process_path(input_path).is_file()
                            else _driver_process_path(input_path)
                        )
                        saved_output, _saved_process = _load_clean_driver_output(
                            input_path, output_path, assignment,
                            process_path=process_for_output,
                        )
                        _confirm_task_graph_publication(
                            protocol=protocol, assignment=assignment,
                            output=saved_output, state_path=state_path,
                        )
                    protocol.finish_task(task_id)
                    protocol.save(state_path)
                continue
            if harbor_recovery_launch is not None:
                output, _process = _invoke_driver(
                    driver=driver,
                    input_path=input_path,
                    output_path=output_path,
                    assignment=assignment,
                    protocol=protocol,
                    harbor_recovery_manifest_path=harbor_recovery_launch["path"],
                    manager_root=manager_root,
                    allow_test_fixture=allow_test_fixture,
                )
                _apply_driver_output(
                    protocol,
                    output,
                    assignment,
                    state_path,
                    manager_root=manager_root,
                    allow_test_fixture=allow_test_fixture,
                    frozen_state_ref=frozen_state_ref,
                )
                # This explicit recovery applies to exactly one original
                # failed task.  Once its output has been transactionally
                # applied, resume the ordinary ordered loop for the next
                # task; no recovery flag may leak into a later assignment.
                harbor_recovery_launch = None
                harbor_recovery_manifest_path = None
                continue
            continuation_process_path = _driver_continuation_process_path(input_path)
            if continuation_process_path.is_file():
                _check_existing_reconciliation_process(
                    continuation_process_path,
                    reconciliation_manifest_path=reconciliation_manifest_path,
                    output_path=output_path,
                )
                continuation_process = _object(read_json(continuation_process_path), field="driver-continuation-process")
                if continuation_process.get("status") != "succeeded" or continuation_process.get("returncode") != 0 or continuation_process.get("timed_out") is True:
                    _mark_reconcile(
                        input_path,
                        assignment=assignment,
                        phase="driver_continuation_process_not_successful",
                        error="completed-trial continuation process is failed or uncertain; retry is disabled",
                        process=continuation_process,
                    )
                    raise COnlyProtocolError("completed-trial continuation process failed or is uncertain; reconcile before resuming")
                output, _process = _load_clean_driver_output(
                    input_path,
                    output_path,
                    assignment,
                    process_path=continuation_process_path,
                )
                _apply_driver_output(
                    protocol,
                    output,
                    assignment,
                    state_path,
                    manager_root=manager_root,
                    allow_test_fixture=allow_test_fixture,
                    frozen_state_ref=frozen_state_ref,
                )
                reconciliation_manifest_path = None
                reconciliation_trial = None
                continue
            if continue_from_extraction:
                if current_assignment.get("status") != "finished" or "extraction" not in current_assignment or "publication" in current_assignment:
                    raise COnlyProtocolError("extraction continuation requires exactly the current applied extraction")
                output, _process = _invoke_driver(
                    driver=driver, input_path=input_path, output_path=output_path,
                    assignment=assignment, protocol=protocol,
                    continue_from_extraction=True, manager_root=manager_root,
                    allow_test_fixture=allow_test_fixture,
                )
                _apply_driver_output(
                    protocol, output, assignment, state_path,
                    manager_root=manager_root, allow_test_fixture=allow_test_fixture,
                    frozen_state_ref=frozen_state_ref,
                )
                continue_from_extraction = False
                continue
            if _trial_stage_is_safe_continuation(
                input_path=input_path,
                output_path=output_path,
                assignment=assignment,
                manager_root=manager_root,
                allow_test_fixture=allow_test_fixture,
                allow_reconciled_manager_journal=reconciliation_manifest_path is not None,
            ):
                output, _process = _invoke_driver(
                    driver=driver,
                    input_path=input_path,
                    output_path=output_path,
                    assignment=assignment,
                    protocol=protocol,
                    continue_from_trial=True,
                    reconciliation_manifest_path=reconciliation_manifest_path,
                    manager_root=manager_root,
                    allow_test_fixture=allow_test_fixture,
                )
                _apply_driver_output(
                    protocol,
                    output,
                    assignment,
                    state_path,
                    manager_root=manager_root,
                    allow_test_fixture=allow_test_fixture,
                    frozen_state_ref=frozen_state_ref,
                )
                reconciliation_manifest_path = None
                reconciliation_trial = None
                continue
            if reconciliation_manifest_path is not None:
                _mark_reconcile(
                    input_path,
                    assignment=assignment,
                    phase="reconciliation_continuation_stage_not_safe",
                    error="explicit manager reconciliation did not find the exact completed-trial continuation boundary",
                )
                raise COnlyProtocolError(
                    "manager reconciliation requires the exact completed-trial continuation boundary; automatic Harbor retry is disabled"
                )
            recovered = _recover_driver_stages(
                protocol,
                assignment=assignment,
                input_path=input_path,
                state_path=state_path,
                manager_root=manager_root,
                allow_test_fixture=allow_test_fixture,
            )
            if recovered is not None:
                continue
            # A generic driver may have completed atomically before the
            # coordinator crashed, leaving only the durable process/output
            # pair.  Reuse that output through _invoke_driver (which verifies
            # both hashes) before refusing a paid retry.  If neither artifact
            # exists, a finished phase is ambiguous and must stop.
            if current_assignment.get("status") == "finished" and not (output_path.is_file() or _driver_process_path(input_path).is_file()):
                _mark_reconcile(input_path, assignment=assignment, phase="state_phase_without_driver_stage", error="state contains a completed phase but no immutable driver phase artifact")
                raise COnlyProtocolError("completed C-only phase has no durable driver stage; automatic paid retry is disabled")
            output, _process = _invoke_driver(
                driver=driver,
                input_path=input_path,
                output_path=output_path,
                assignment=assignment,
                protocol=protocol,
                manager_root=manager_root,
                allow_test_fixture=allow_test_fixture,
            )
            _apply_driver_output(
                protocol,
                output,
                assignment,
                state_path,
                manager_root=manager_root,
                allow_test_fixture=allow_test_fixture,
                frozen_state_ref=frozen_state_ref,
            )
        except BaseException as error:
            _mark_reconcile(input_path, assignment=assignment, phase="protocol_phase_application", error=error)
            raise


def start(args: argparse.Namespace) -> dict[str, Any]:
    if not args.confirm_user_start:
        raise COnlyProtocolError("formal start is gated; pass --confirm-user-start only after the user explicitly approves")
    if not args.trial_driver.is_file():
        raise COnlyProtocolError(f"official C-only Harbor driver is not a file: {args.trial_driver}")
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = args.state
    if state_path.exists():
        protocol = COnlyProtocol.load(state_path, args.config, args.baseline_manifest)
        if protocol.state["formal_campaign"] == "complete":
            raise COnlyProtocolError("formal campaign is already complete")
    else:
        protocol = COnlyProtocol.initialize(args.config, args.baseline_manifest, state_path)
    _ensure_runtime_parity_gate(
        protocol,
        accept_runtime_deviation=bool(getattr(args, "accept_runtime_deviation", False)),
        state_path=state_path,
    )
    protocol.authorize_start()
    protocol.save(state_path)
    with _execution_lock(run_dir, state_path=state_path):
        _run_driver(protocol, args.trial_driver, run_dir, state_path)
    return {"status": protocol.state["formal_campaign"], "state": str(state_path), "planned_trials": len(protocol.tasks) * 2}


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    actions = value.add_subparsers(dest="command", required=True)
    prepare_parser = actions.add_parser("prepare")
    prepare_parser.add_argument("--baseline-manifest", type=Path, required=True)
    prepare_parser.add_argument("--gpu-evidence", type=Path)
    prepare_parser.add_argument(
        "--task-audit",
        type=Path,
        help="public task.toml and Docker identity audit used to pin formal task artifacts",
    )
    prepare_parser.add_argument("--output-config", type=Path, required=True)
    prepare_parser.add_argument("--state", type=Path)
    prepare_parser.set_defaults(func=prepare)
    for name in ("check", "resume"):
        command = actions.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--baseline-manifest", type=Path, required=True)
        command.add_argument("--state", type=Path, required=True if name == "resume" else False)
        if name == "resume":
            command.add_argument("--run-dir", type=Path, required=False)
            command.add_argument("--trial-driver", type=Path, default=None)
            command.add_argument(
                "--reconciliation-manifest",
                type=Path,
                default=None,
                help="resume one explicitly audited completed trial without replaying its saved manager call",
            )
            command.add_argument(
                "--harbor-recovery-manifest",
                type=Path,
                default=None,
                help="recover one original Harbor trial from an immutable artifact manifest without relaunching Harbor",
            )
            command.add_argument("--confirm-user-start", action="store_true")
            command.add_argument(
                "--accept-runtime-deviation",
                action="store_true",
                help="explicitly accept the prepared runtime parity deviation after reviewing its evidence",
            )
        command.set_defaults(func=check if name == "check" else resume)
    start_parser = actions.add_parser("start")
    start_parser.add_argument("--config", type=Path, required=True)
    start_parser.add_argument("--baseline-manifest", type=Path, required=True)
    start_parser.add_argument("--state", type=Path, required=True)
    start_parser.add_argument("--run-dir", type=Path, required=True)
    start_parser.add_argument("--trial-driver", type=Path, default=ROOT / "scripts" / "run_r015_c_only_harbor_driver.py")
    start_parser.add_argument("--confirm-user-start", action="store_true")
    start_parser.add_argument(
        "--accept-runtime-deviation",
        action="store_true",
        help="explicitly accept the prepared runtime parity deviation after reviewing its evidence",
    )
    start_parser.set_defaults(func=start)
    return value


def main() -> None:
    args = parser().parse_args()
    try:
        result = args.func(args)
    except (COnlyProtocolError, OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"{type(error).__name__}: {error}") from error
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
