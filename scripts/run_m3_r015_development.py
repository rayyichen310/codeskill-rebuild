#!/usr/bin/env python3
"""Run the authorized two-instance R015 A/B/C development lifecycle.

The launcher is deliberately durable and serial.  It freezes all three arm
snapshots for an instance before starting its first Harbor trial, gives each
trial one OpenClaw session and one sidecar, and publishes Arm A candidates to
the next instance only after the current A/B/C assignments have finished.
Harbor remains responsible for the official solver and verifier; this module
only binds the public sidecar and packages their raw evidence.

This command is a development integration runner.  It does not start the
formal 12-task x 3-arm x 2-repeat campaign.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from codeskill_rebuild.bank import SkillBank
from codeskill_rebuild.development_runner import (
    DevelopmentRunnerError,
    release_development_instance,
    validate_arm_a_candidate_records,
)
from codeskill_rebuild.manager import (
    ManagerClient,
    ManagerProfile,
    ServerMessageTokenCounter,
    update_development_ledger_limit,
)
from codeskill_rebuild.r012_execution import (
    R012EvolutionMaintenanceExecutor,
    R012ExecutionError,
    inspect_full_lifecycle_evidence,
    profile_sha256,
    validate_execution_profile,
)
from codeskill_rebuild.retrieval import MiniLMEncoder
from codeskill_rebuild.r015_harbor_evidence import (
    HarborTrialEvidenceError,
    import_harbor_openclaw_trial,
)
from codeskill_rebuild.trial_schedule import InstanceBankFreeze, TrialScheduleError
from codeskill_rebuild.types import (
    canonical_instance_id,
    canonical_json,
    contract_from_files,
    read_json,
    sha256_file,
    sha256_text,
    utc_now,
    write_contract_snapshot,
    write_json,
)


class DevelopmentLauncherError(RuntimeError):
    """The durable R015 launcher cannot safely continue this development run."""


ARM_NAMES = ("A", "B", "C")


def _object(path: Path) -> dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise DevelopmentLauncherError(f"JSON input must be an object: {path}")
    return value


def _ref(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"path": str(path), "exists": False}
    return {"path": str(path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def _hash_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def _arm_bank(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("--arm-bank needs ARM=PATH")
    arm, raw_path = value.split("=", 1)
    if arm not in ARM_NAMES or not raw_path:
        raise argparse.ArgumentTypeError("--arm-bank needs one of A/B/C and a nonempty PATH")
    return arm, Path(raw_path)


def _model_service(path: Path, service_name: str) -> dict[str, Any]:
    config = _object(path)
    services = config.get("services")
    if isinstance(services, dict):
        service = services.get(service_name)
    else:
        service = config.get(service_name)
    if not isinstance(service, dict):
        raise DevelopmentLauncherError(f"model config has no services.{service_name} object")
    base_url = service.get("base_url", service.get("baseUrl"))
    model_id = service.get("model_id", service.get("modelId"))
    if not isinstance(base_url, str) or not base_url.strip():
        raise DevelopmentLauncherError(f"model service {service_name} needs base_url")
    if not isinstance(model_id, str) or not model_id.strip():
        raise DevelopmentLauncherError(f"model service {service_name} needs model_id")
    return {"base_url": base_url.rstrip("/"), "model_id": model_id, **service}


def _profile_instances(profile: dict[str, Any], requested: list[str] | None) -> list[str]:
    development = profile.get("development")
    if not isinstance(development, dict) or not isinstance(development.get("instances"), list):
        raise DevelopmentLauncherError("R015 profile needs development.instances")
    configured = [canonical_instance_id(str(item)) for item in development["instances"]]
    if len(configured) != len(set(configured)) or not all(configured):
        raise DevelopmentLauncherError("R015 profile development.instances must be unique nonempty IDs")
    if requested is None:
        return configured
    selected = [canonical_instance_id(item) for item in requested]
    if len(selected) != len(set(selected)):
        raise DevelopmentLauncherError("--instance cannot repeat an instance")
    unknown = sorted(set(selected) - set(configured))
    if unknown:
        raise DevelopmentLauncherError("--instance names are absent from the R015 profile: " + ", ".join(unknown))
    return [item for item in configured if item in set(selected)]


def _validate_profile(profile: dict[str, Any], *, minilm_revision: str | None) -> dict[str, Any]:
    configured = validate_execution_profile(profile)
    injection = configured.get("sidecar_injection")
    if not isinstance(injection, dict) or not isinstance(injection.get("arms"), dict):
        raise DevelopmentLauncherError("R015 profile needs sidecar_injection.arms")
    arms = injection["arms"]
    if set(arms) != set(ARM_NAMES):
        raise DevelopmentLauncherError("R015 development requires exactly A/B/C sidecar arm controls")
    for arm in ARM_NAMES:
        value = arms[arm]
        if not isinstance(value, dict) or not isinstance(value.get("enable_task"), bool) or not isinstance(value.get("enable_event"), bool):
            raise DevelopmentLauncherError(f"R015 profile sidecar_injection.arms.{arm} needs boolean task/event controls")
    development = configured.get("development")
    if not isinstance(development, dict):
        raise DevelopmentLauncherError("R015 profile needs development settings")
    if development.get("arm_order") != list(ARM_NAMES) or development.get("release_order") != list(ARM_NAMES):
        raise DevelopmentLauncherError("R015 development arm and release order must both be A,B,C")
    if development.get("repeat_id") != "development":
        raise DevelopmentLauncherError("R015 launcher requires the single repeat id development")
    if development.get("same_instance_freeze_before_trials") is not True or development.get("later_instance_only_after_release") is not True:
        raise DevelopmentLauncherError("R015 profile must freeze the instance before trials and release before a later instance")
    runtime = configured.get("runtime")
    if not isinstance(runtime, dict):
        raise DevelopmentLauncherError("R015 profile needs runtime settings")
    expected_revision = runtime.get("minilm_revision")
    if not isinstance(expected_revision, str) or not expected_revision:
        raise DevelopmentLauncherError("R015 runtime needs the exact MiniLM revision")
    if minilm_revision is not None and minilm_revision != expected_revision:
        raise DevelopmentLauncherError("--minilm-revision differs from the frozen R015 profile")
    if runtime.get("harbor_concurrency") != 1 or runtime.get("retry_count") != 0:
        raise DevelopmentLauncherError("R015 development requires Harbor concurrency 1 and retry count 0")
    return configured


def _state_value(path: Path, profile_hash: str, contract: dict[str, str]) -> dict[str, Any]:
    value = _object(path)
    if value.get("kind") != "r012_instance_lifecycle_state":
        raise DevelopmentLauncherError("lifecycle state has an invalid kind")
    profile = validate_execution_profile(value.get("profile"))
    if profile_sha256(profile) != profile_hash or value.get("profile_sha256") != profile_hash:
        raise DevelopmentLauncherError("lifecycle state profile hash differs from this run profile")
    if value.get("contract") != contract:
        raise DevelopmentLauncherError("lifecycle state contract differs from the current contract documents")
    if not isinstance(value.get("coordinator"), dict):
        raise DevelopmentLauncherError("lifecycle state has no coordinator")
    return value


def _save_state(path: Path, state: dict[str, Any], coordinator: InstanceBankFreeze) -> None:
    state["coordinator"] = coordinator.to_dict()
    state["updated_at_utc"] = utc_now()
    write_json(path, state)


def _assignment(coordinator: InstanceBankFreeze, trial_id: str) -> dict[str, Any]:
    try:
        return coordinator._assignment(trial_id)
    except TrialScheduleError as error:
        raise DevelopmentLauncherError(str(error)) from error


def _trial_id(instance_id: str, arm: str, repeat_id: str) -> str:
    return f"{canonical_instance_id(instance_id)}:{arm}:{repeat_id}"


def _session_id(trial_id: str) -> str:
    return "r015-" + sha256_text(trial_id)[:28]


def _shell_env(repo_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    source_path = str((repo_root / "src").resolve())
    old = env.get("PYTHONPATH")
    env["PYTHONPATH"] = source_path if not old else source_path + os.pathsep + old
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _python_executable(args: argparse.Namespace) -> str:
    value = getattr(args, "python", None)
    return str(value) if isinstance(value, Path) else sys.executable


def _wait_listener(process: subprocess.Popen[Any], host: str, port: int, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(1)
    return False


def _stop_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=15)


def _find_harbor_trial(jobs_dir: Path, expected_task: str) -> Path | None:
    expected_leaf = expected_task.rsplit("/", 1)[-1]
    candidates: list[Path] = []
    for result_path in jobs_dir.rglob("result.json") if jobs_dir.exists() else []:
        trial_dir = result_path.parent
        config_path = trial_dir / "config.json"
        if not config_path.is_file():
            continue
        try:
            config = _object(config_path)
        except DevelopmentLauncherError:
            continue
        task = config.get("task")
        if not isinstance(task, dict):
            continue
        # Harbor 0.16.1 writes ``task.name`` as null for a local task and
        # records the authoritative task identity in ``task.path``.  Keep the
        # name comparison for older Harbor output, then fall back to the
        # canonical path leaf so a completed official trial is not discarded
        # as an infrastructure failure merely because the name field is null.
        task_name = task.get("name")
        task_path = task.get("path")
        path_leaf = Path(task_path).name if isinstance(task_path, str) else None
        if task_name == expected_task or path_leaf == expected_leaf:
            candidates.append(trial_dir)
    if not candidates:
        return None
    if len(candidates) > 1:
        # A one-attempt job must produce one trial.  Refuse to guess which
        # directory belongs to this paid call if Harbor left stale output.
        raise DevelopmentLauncherError("Harbor produced more than one matching trial directory")
    return candidates[0]


def _raw_failure_refs(
    trial_root: Path,
    process_refs: dict[str, Any],
    trial_dir: Path | None,
    host_database: Path | None = None,
) -> dict[str, Any]:
    values: dict[str, Any] = {"process": deepcopy(process_refs), "trial_dir": str(trial_dir) if trial_dir else None}
    for name in ("harbor.stdout.log", "harbor.stderr.log", "sidecar.stdout.log", "sidecar.stderr.log"):
        path = trial_root / name
        values[name] = _ref(path)
    if trial_dir is not None:
        for relative in (
            Path("result.json"),
            Path("config.json"),
            Path("verifier") / "reward.txt",
            Path("agent") / "openclaw.session.jsonl",
            Path("agent") / "session-export.json",
            Path("agent") / "codeskill-openclaw-state" / "openclaw-agent.sqlite",
        ):
            values[f"trial/{relative.as_posix()}"] = _ref(trial_dir / relative)
    if host_database is not None:
        for relative in (
            Path("openclaw-state") / "agents" / "main" / "agent" / "openclaw-agent.sqlite",
            Path("openclaw-state") / "agents" / "main" / "agent" / "openclaw-agent.sqlite-wal",
            Path("openclaw-state") / "agents" / "main" / "agent" / "openclaw-agent.sqlite-shm",
        ):
            values[f"host/{relative.as_posix()}"] = _ref(trial_root / relative)
    return values


def _profile_sidecar_rules(profile: dict[str, Any]) -> dict[str, Any]:
    injection = profile["sidecar_injection"]
    task = injection["task_selection"]
    event = injection["event_selection"]
    root_event = profile["event_selection"]
    return {
        "taskSelection": {
            "selectionRuleRef": task["selection_rule_ref"],
            "threshold": task["threshold"],
            "maxMatchingSkills": task["max_matching_skills"],
        },
        "eventSelection": {
            "profileRef": root_event["profile_ref"],
            "selectionRuleRef": root_event["selection_rule_ref"],
            "threshold": event["threshold"],
            "maxMatchingSkills": root_event["max_matching_skills"],
            "skillTokenBudget": root_event["skill_token_budget"],
            "budgetScope": injection["event_skill_token_budget_scope"],
        },
    }


def _host_openclaw_config(*, model_id: str, sidecar_url: str, plugin_path: Path, permit_directory: Path, trial_id: str, session_id: str, audit_path: Path, context_tokens: int, max_output_tokens: int) -> dict[str, Any]:
    return {
        "gateway": {"mode": "local"},
        "models": {
            "providers": {
                "codeskill-r012": {
                    "baseUrl": sidecar_url,
                    "apiKey": "${CODESKILL_SIDECAR_KEY}",
                    "api": "openai-completions",
                    "models": [
                        {
                            "id": model_id,
                            "name": "CODESKILL sidecar upstream model",
                            "reasoning": False,
                            "input": ["text"],
                            "contextWindow": context_tokens,
                            "contextTokens": context_tokens,
                            "maxTokens": max_output_tokens,
                            "compat": {"supportsUsageInStreaming": True},
                        }
                    ],
                }
            }
        },
        "agents": {
            "defaults": {
                "model": {"primary": f"codeskill-r012/{model_id}"},
                "maxConcurrent": 1,
            }
        },
        "plugins": {
            "allow": ["codeskill-r012-sidecar"],
            "load": {"paths": [str(plugin_path.resolve())]},
            "entries": {
                "codeskill-r012-sidecar": {
                    "enabled": True,
                    "config": {
                        "permitDirectory": str(permit_directory.resolve()),
                        "trialId": trial_id,
                        "sessionId": session_id,
                        "auditPath": str(audit_path.resolve()),
                    },
                }
            },
        },
    }


def _build_trial_context(
    *,
    args: argparse.Namespace,
    profile: dict[str, Any],
    profile_hash: str,
    state_path: Path,
    state_hash: str,
    assignment: dict[str, Any],
    service: dict[str, Any],
    task_path: Path,
    ordinal: int,
) -> dict[str, Any]:
    trial_id = assignment["trial_id"]
    instance_id = assignment["instance_id"]
    arm = assignment["arm"]
    session_id = _session_id(trial_id)
    trial_root = args.run_dir / "trials" / sha256_text(trial_id)[:20]
    if trial_root.exists():
        # The launch intent is the paid-call boundary.  A pre-existing trial
        # without a terminal finish record requires a human reconciliation.
        # A finish record while the coordinator still says ``pending`` is
        # also ambiguous (the normal writer persists coordinator state first),
        # so never overwrite it while attempting a resume.
        if (trial_root / "finish-record.json").is_file():
            raise DevelopmentLauncherError(
                f"{trial_id}: finish-record.json exists while the coordinator assignment is pending; manual reconciliation is required"
            )
        if (trial_root / "launch-intent.json").is_file():
            raise DevelopmentLauncherError(
                f"{trial_id}: existing launch intent requires manual reconciliation; automatic replay is forbidden"
            )
        raise DevelopmentLauncherError(
            f"{trial_id}: trial directory already exists without a durable launch intent"
        )
    trial_root.mkdir(parents=True, exist_ok=True)
    sidecar_dir = trial_root / "sidecar"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    permit_dir = trial_root / "native-summary-permits"
    permit_dir.mkdir(parents=True, exist_ok=True)
    openclaw_state_host = trial_root / "openclaw-state"
    openclaw_state_host.mkdir(parents=True, exist_ok=True)
    # Pre-create the canonical OpenClaw agent directory and database inode
    # before Docker starts the task.  The official task container runs its
    # bootstrap as root, while the host-side sidecar runs as the task user.
    # An absent bind-mounted file is therefore created root-owned and becomes
    # unreadable to the detector.  A zero-byte, user-owned inode has the same
    # official SQLite initialization semantics (user_version=0, no schema),
    # while allowing the official runtime to initialize it in place without
    # changing its schema or session lifecycle.
    openclaw_agent_dir = openclaw_state_host / "agents" / "main" / "agent"
    openclaw_agent_dir.mkdir(parents=True, exist_ok=True)
    openclaw_database_host = openclaw_agent_dir / "openclaw-agent.sqlite"
    openclaw_database_host.touch(mode=0o600, exist_ok=False)
    overlay_state = sidecar_dir / "overlay-state.json"
    sidecar_port = int(args.sidecar_port_base) + ordinal
    advertised_host = args.sidecar_advertised_host
    sidecar_url = f"http://{advertised_host}:{sidecar_port}/v1"
    host_config_path = trial_root / "openclaw-host.json"
    audit_path = permit_dir / "plugin-audit.jsonl"
    write_json(
        host_config_path,
        _host_openclaw_config(
            model_id=service["model_id"],
            sidecar_url=sidecar_url,
            plugin_path=args.plugin_path,
            permit_directory=permit_dir,
            trial_id=trial_id,
            session_id=session_id,
            audit_path=audit_path,
            context_tokens=int(profile["runtime"]["official_solver_model_context_tokens"]),
            max_output_tokens=int(profile["runtime"]["proxy_max_output_tokens"]),
        ),
    )
    snapshot = assignment.get("frozen_bank")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("state_sha256"), str):
        raise DevelopmentLauncherError(f"{trial_id}: frozen assignment lacks a bank snapshot hash")
    runtime = profile["runtime"]
    upstream = {"endpoint": service["base_url"] + "/chat/completions", "timeoutSeconds": int(runtime["upstream_timeout_seconds"])}
    auth_env = service.get("authorization_env", service.get("authorizationEnv"))
    if isinstance(auth_env, str) and auth_env:
        upstream["authorizationEnv"] = auth_env
    retrieval = {
        "trialId": trial_id,
        "instanceId": instance_id,
        "lifecycleStatePath": str(state_path.resolve()),
        "lifecycleStateSha256": state_hash,
        "profileSha256": profile_hash,
        "bankSnapshotSha256": snapshot["state_sha256"],
        "encoder": {
            "kind": "minilm",
            "repoId": runtime["minilm_repo_id"],
            "revision": runtime["minilm_revision"],
        },
        **_profile_sidecar_rules(profile),
    }
    sidecar_config = {
        "schema_version": 1,
        "kind": "r015_openclaw_sidecar_binding",
        "trialId": trial_id,
        "sessionId": session_id,
        "sessionMarker": f"sqlite:main:{session_id}:{(openclaw_state_host / 'agents' / 'main' / 'agent' / 'openclaw-agent.sqlite').resolve()}",
        "permitDirectory": str(permit_dir.resolve()),
        "overlay": {
            "statePath": str(overlay_state.resolve()),
            "evidenceDirectory": str(sidecar_dir.resolve()),
            "maxInputTokens": int(runtime["proxy_max_input_tokens"]),
        },
        "tokenizer": {"baseUrl": service["base_url"], "timeoutSeconds": 60},
        "upstream": upstream,
        "listen": {"host": args.sidecar_listen_host, "advertisedHost": advertised_host, "port": sidecar_port},
        "openclaw": {
            "configPath": str(host_config_path.resolve()),
            "pluginPath": str(args.plugin_path.resolve()),
            "providerId": "codeskill-r012",
            "modelId": service["model_id"],
        },
        "selection": {"mode": "frozen-bank"},
        "retrieval": retrieval,
        "maxForwardedRequests": int(runtime["proxy_max_forwarded_requests"]),
        "maxOutputTokens": int(runtime["proxy_max_output_tokens"]),
    }
    sidecar_config_path = trial_root / "sidecar.json"
    write_json(sidecar_config_path, sidecar_config)
    container_plugin_source_path = "/opt/codeskill/openclaw-sidecar-src"
    container_plugin_path = "/opt/codeskill/openclaw-sidecar"
    container_permit_path = "/var/lib/codeskill/native-summary-permits"
    container_state_path = "/var/lib/codeskill/openclaw-state"
    job_dir = trial_root / "harbor"
    jobs_dir = job_dir / "jobs"
    job_dir.mkdir(parents=True, exist_ok=True)
    job_config = {
        "schema_version": 1,
        "job_name": f"r015-{instance_id}-{arm}-{sha256_text(trial_id)[:12]}",
        "jobs_dir": str(jobs_dir.resolve()),
        "n_attempts": 1,
        "n_concurrent_trials": 1,
        "quiet": False,
        "debug": True,
        "retry": {"max_retries": 0},
        "environment": {
            "type": "docker",
            "delete": True,
            "extra_allowed_hosts": [advertised_host],
            "env": {"OPENCLAW_STATE_DIR": container_state_path},
            "mounts": [
                {"type": "bind", "source": str(args.plugin_path.resolve()), "target": container_plugin_source_path, "read_only": True},
                {"type": "bind", "source": str(permit_dir.resolve()), "target": container_permit_path},
                {"type": "bind", "source": str(openclaw_state_host.resolve()), "target": container_state_path},
            ],
        },
        "verifier": {"override_timeout_sec": float(runtime["verifier_timeout_seconds"])},
        "agents": [
            {
                "import_path": "codeskill_rebuild.harbor_openclaw_adapter:CODESKILLHarborOpenClaw",
                "model_name": f"codeskill-r012/{service['model_id']}",
                "n_concurrent": 1,
                "override_timeout_sec": float(runtime["agent_timeout_seconds"]),
                "override_setup_timeout_sec": float(runtime["build_timeout_seconds"]),
                "extra_allowed_hosts": [advertised_host],
                "kwargs": {
                    "version": runtime["official_openclaw_package"].removeprefix("openclaw@"),
                    "sidecar_base_url": sidecar_url,
                    "sidecar_model_id": service["model_id"],
                    "plugin_path": container_plugin_path,
                    "permit_directory": container_permit_path,
                    "plugin_audit_path": container_permit_path + "/plugin-audit.jsonl",
                    "trial_id": trial_id,
                    "session_id": session_id,
                    "context_tokens": int(runtime["official_solver_model_context_tokens"]),
                    "max_output_tokens": int(runtime["proxy_max_output_tokens"]),
                    "thinking": "off",
                    "session_to_trajectory": True,
                    "openclaw_config": {},
                },
                "env": {
                    "CODESKILL_SIDECAR_KEY": "dev-local",
                    "OPENCLAW_STATE_DIR": container_state_path,
                },
            }
        ],
        "tasks": [{"path": str(task_path.resolve())}],
    }
    job_config_path = job_dir / "job.json"
    write_json(job_config_path, job_config)
    intent = {
        "schema_version": 1,
        "kind": "r015_harbor_launch_intent",
        "created_at_utc": utc_now(),
        "trial_id": trial_id,
        "instance_id": instance_id,
        "arm": arm,
        "repeat_id": assignment["repeat_id"],
        "session_id": session_id,
        "frozen_bank_snapshot_sha256": snapshot["state_sha256"],
        "lifecycle_state": _ref(state_path),
        "profile_sha256": profile_hash,
        "task": _ref(task_path / "task.toml"),
        "sidecar_config": _ref(sidecar_config_path),
        "job_config": _ref(job_config_path),
        "official_runtime": {
            "harbor_version": runtime["official_harbor_version"],
            "openclaw_package": runtime["official_openclaw_package"],
            "tb21_commit": runtime["official_tb21_commit"],
            "node_runtime": "24.16.0 via CODESKILL public Harbor subclass alias for inherited nvm use 22",
            "openclaw_source_mount": "forbidden",
            "agent_user_uid": 1000,
            "agent_home": "/home/codeskill",
            "agent_workspace": "/app",
            "agent_workspace_preparation": "root-prepared ephemeral Harbor container path",
        },
        "native_database_seed": _ref(openclaw_database_host),
    }
    write_json(trial_root / "launch-intent.json", intent)
    return {
        "trial_id": trial_id,
        "instance_id": instance_id,
        "arm": arm,
        "session_id": session_id,
        "trial_root": trial_root,
        "sidecar_dir": sidecar_dir,
        "sidecar_config_path": sidecar_config_path,
        "sidecar_port": sidecar_port,
        "host_config_path": host_config_path,
        "permit_dir": permit_dir,
        "openclaw_state_host": openclaw_state_host,
        "openclaw_database_host": openclaw_database_host,
        "job_config_path": job_config_path,
        "jobs_dir": jobs_dir,
        "task_path": task_path,
        "state_path": state_path,
    }


def _sidecar_check(args: argparse.Namespace, context: dict[str, Any], repo_root: Path) -> tuple[bool, dict[str, Any]]:
    command = [_python_executable(args), str(args.sidecar_script), "--config", str(context["sidecar_config_path"]), "--check-config"]
    output_path = context["trial_root"] / "sidecar-check.stdout.log"
    error_path = context["trial_root"] / "sidecar-check.stderr.log"
    started = time.monotonic()
    return_code: int | None = None
    timed_out = False
    error_type: str | None = None
    error_message: str | None = None

    def captured(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value if isinstance(value, str) else ""

    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            env=_shell_env(repo_root),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        return_code = completed.returncode
        stdout = captured(completed.stdout)
        stderr = captured(completed.stderr)
    except subprocess.TimeoutExpired as error:
        timed_out = True
        error_type = type(error).__name__
        error_message = str(error)
        stdout = captured(error.stdout)
        stderr = captured(error.stderr)
    except OSError as error:
        error_type = type(error).__name__
        error_message = str(error)
    output_path.write_text(stdout, encoding="utf-8")
    error_path.write_text(stderr, encoding="utf-8")
    evidence = {
        "kind": "r015_sidecar_check_config",
        "command": command,
        "return_code": return_code,
        "timed_out": timed_out,
        "error_type": error_type,
        "error": error_message,
        "elapsed_seconds": time.monotonic() - started,
        "stdout": _ref(output_path),
        "stderr": _ref(error_path),
    }
    write_json(context["trial_root"] / "sidecar-check.json", evidence)
    return return_code == 0 and not timed_out, evidence


def _run_harbor_trial(*, args: argparse.Namespace, context: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    trial_root = context["trial_root"]
    check_ok, check_evidence = _sidecar_check(args, context, repo_root)
    if not check_ok:
        process = {
            "kind": "r015_harbor_process",
            "status": "infra_failure",
            "classification": "sidecar_check_failed",
            "check": check_evidence,
            "sidecar_command": None,
            "sidecar_return_code": None,
            "sidecar_ready": False,
            "harbor_command": None,
            "harbor_return_code": None,
        }
        write_json(trial_root / "harbor-process.json", process)
        return process
    sidecar_stdout = trial_root / "sidecar.stdout.log"
    sidecar_stderr = trial_root / "sidecar.stderr.log"
    harbor_stdout = trial_root / "harbor.stdout.log"
    harbor_stderr = trial_root / "harbor.stderr.log"
    sidecar_command = [_python_executable(args), str(args.sidecar_script), "--config", str(context["sidecar_config_path"])]
    sidecar_process: subprocess.Popen[Any] | None = None
    sidecar_spawn_error: OSError | None = None
    try:
        with sidecar_stdout.open("w", encoding="utf-8") as stdout, sidecar_stderr.open("w", encoding="utf-8") as stderr:
            sidecar_process = subprocess.Popen(
                sidecar_command,
                cwd=repo_root,
                env=_shell_env(repo_root),
                stdout=stdout,
                stderr=stderr,
                text=True,
            )
    except OSError as error:
        sidecar_spawn_error = error
    if sidecar_process is None:
        process = {
            "kind": "r015_harbor_process",
            "status": "infra_failure",
            "classification": "sidecar_spawn_failed",
            "check": check_evidence,
            "sidecar_command": sidecar_command,
            "sidecar_return_code": None,
            "sidecar_ready": False,
            "sidecar_spawn_error_type": type(sidecar_spawn_error).__name__ if sidecar_spawn_error else "OSError",
            "sidecar_spawn_error": str(sidecar_spawn_error) if sidecar_spawn_error else "sidecar process did not start",
            "sidecar_stdout": _ref(sidecar_stdout),
            "sidecar_stderr": _ref(sidecar_stderr),
            "harbor_command": None,
            "harbor_return_code": None,
        }
        write_json(trial_root / "harbor-process.json", process)
        return process
    sidecar_ready = _wait_listener(sidecar_process, "127.0.0.1", int(context["sidecar_port"]), int(args.sidecar_startup_timeout))
    if not sidecar_ready:
        _stop_process(sidecar_process)
        process = {
            "kind": "r015_harbor_process",
            "status": "infra_failure",
            "classification": "sidecar_startup_failed",
            "check": check_evidence,
            "sidecar_command": sidecar_command,
            "sidecar_return_code": sidecar_process.returncode,
            "sidecar_ready": False,
            "sidecar_stdout": _ref(sidecar_stdout),
            "sidecar_stderr": _ref(sidecar_stderr),
            "harbor_command": None,
            "harbor_return_code": None,
        }
        write_json(trial_root / "harbor-process.json", process)
        return process
    harbor_command = [str(args.harbor), "job", "start", "--config", str(context["job_config_path"]), "--yes"]
    started = time.monotonic()
    harbor_return_code: int | None = None
    timed_out = False
    harbor_spawn_error: OSError | None = None
    try:
        with harbor_stdout.open("w", encoding="utf-8") as out, harbor_stderr.open("w", encoding="utf-8") as err:
            completed = subprocess.run(
                harbor_command,
                cwd=repo_root,
                env=_shell_env(repo_root),
                stdout=out,
                stderr=err,
                timeout=int(args.outer_timeout_seconds),
                check=False,
            )
        harbor_return_code = completed.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
    except OSError as error:
        harbor_spawn_error = error
    finally:
        _stop_process(sidecar_process)
    if timed_out:
        classification = "harbor_timeout"
    elif harbor_spawn_error is not None:
        classification = "harbor_spawn_failed"
    elif harbor_return_code != 0:
        classification = "harbor_nonzero"
    else:
        classification = "completed"
    process = {
        "kind": "r015_harbor_process",
        "status": "completed" if classification == "completed" else "infra_failure",
        "classification": classification,
        "check": check_evidence,
        "sidecar_command": sidecar_command,
        "sidecar_return_code": sidecar_process.returncode,
        "sidecar_ready": sidecar_ready,
        "sidecar_stdout": _ref(sidecar_stdout),
        "sidecar_stderr": _ref(sidecar_stderr),
        "harbor_command": harbor_command,
        "harbor_return_code": harbor_return_code,
        "harbor_timed_out": timed_out,
        "harbor_spawn_error_type": type(harbor_spawn_error).__name__ if harbor_spawn_error else None,
        "harbor_spawn_error": str(harbor_spawn_error) if harbor_spawn_error else None,
        "elapsed_seconds": time.monotonic() - started,
        "harbor_stdout": _ref(harbor_stdout),
        "harbor_stderr": _ref(harbor_stderr),
    }
    write_json(trial_root / "harbor-process.json", process)
    return process


def _read_attempt_values(sidecar_dir: Path, trial_id: str) -> list[dict[str, Any]]:
    path = sidecar_dir / "upstream_requests"
    values: list[dict[str, Any]] = []
    if not path.is_dir():
        return values
    for candidate in sorted(path.glob("attempt-*.json")):
        value = _object(candidate)
        if value.get("trial_id") != trial_id:
            raise DevelopmentLauncherError(f"{trial_id}: sidecar evidence belongs to a different trial: {candidate}")
        values.append(value)
    return values


def _collect_trial_evidence(context: dict[str, Any], process: dict[str, Any]) -> dict[str, Any]:
    trial_id = context["trial_id"]
    trial_dir = _find_harbor_trial(context["jobs_dir"], f"terminal-bench/{context['instance_id']}")
    attempts = _read_attempt_values(context["sidecar_dir"], trial_id)
    packet_dir = context["trial_root"] / "finish-packet"
    if trial_dir is not None and attempts:
        try:
            packet_manifest = import_harbor_openclaw_trial(
                trial_id=trial_id,
                instance_id=context["instance_id"],
                harbor_trial_dir=trial_dir,
                sidecar_evidence_dir=context["sidecar_dir"],
                output_dir=packet_dir,
                session_source_path=context["openclaw_database_host"],
            )
            result_value = _object(packet_dir / "trial-result.json")
            trajectory = _object(packet_dir / "trajectory-evidence.json")
            copied_attempts = [_object(path) for path in sorted((packet_dir / "proxy-attempts").glob("attempt-*.json"))]
            return {
                "kind": "r012_finished_trial_evidence",
                "classification": "official_harbor_trial",
                "trial_result": result_value,
                "trial_result_value": result_value,
                "proxy_attempt_records": copied_attempts,
                "trajectory_evidence": trajectory,
                "packet_manifest": packet_manifest,
                "raw_evidence": _raw_failure_refs(context["trial_root"], process, trial_dir, context["openclaw_database_host"]),
            }
        except HarborTrialEvidenceError as error:
            import_error = {"error_type": type(error).__name__, "error": str(error)}
        else:  # pragma: no cover - return above or exception.
            import_error = {}
    else:
        import_error = {
            "error_type": "HarborTrialIncomplete",
            "error": "no completed official Harbor trial and sidecar evidence pair was available",
        }
    if attempts:
        # Keep any same-trial proxy records visible.  inspect_full_lifecycle_evidence
        # will still require a normalized trajectory when they prove injection.
        return {
            "kind": "r012_finished_trial_evidence",
            "classification": "infra_failure",
            "trial_result": {"trial_id": trial_id, "instance_id": context["instance_id"], "official_reward": None, "process": process},
            "trial_result_value": {"trial_id": trial_id, "instance_id": context["instance_id"], "official_reward": None, "process": process},
            "proxy_attempt_records": attempts,
            "trajectory_evidence": None,
            "infra_failure": {
                "trial_id": trial_id,
                "instance_id": context["instance_id"],
                "error_type": import_error["error_type"],
                "error": import_error["error"],
            "raw_evidence": _raw_failure_refs(context["trial_root"], process, trial_dir, context["openclaw_database_host"]),
            },
        }
    return {
        "kind": "r012_finished_trial_evidence",
        "classification": "infra_failure",
        "trial_result": {"trial_id": trial_id, "instance_id": context["instance_id"], "official_reward": None, "process": process},
        "trial_result_value": {"trial_id": trial_id, "instance_id": context["instance_id"], "official_reward": None, "process": process},
        "proxy_attempt_records": [],
        "trajectory_evidence": None,
        "infra_failure": {
            "trial_id": trial_id,
            "instance_id": context["instance_id"],
            "error_type": import_error["error_type"],
            "error": import_error["error"],
            "raw_evidence": _raw_failure_refs(context["trial_root"], process, trial_dir, context["openclaw_database_host"]),
        },
    }


def _finish_trial(*, args: argparse.Namespace, state: dict[str, Any], coordinator: InstanceBankFreeze, context: dict[str, Any], profile_hash: str) -> dict[str, Any]:
    assignment = _assignment(coordinator, context["trial_id"])
    if assignment.get("status") != "pending":
        if (context["trial_root"] / "finish-record.json").is_file():
            return _object(context["trial_root"] / "finish-record.json")["result_evidence"]
        raise DevelopmentLauncherError(f"{context['trial_id']}: coordinator is finished but finish-record.json is missing")
    process = _run_harbor_trial(args=args, context=context, repo_root=args.repo_root)
    evidence = _collect_trial_evidence(context, process)
    # Validate before changing coordinator state.  This catches injected skill
    # records without a same-trial trajectory while preserving all raw logs.
    inspect_full_lifecycle_evidence(
        trial_id=context["trial_id"],
        instance_id=context["instance_id"],
        result_evidence=evidence,
    )
    coordinator.finish(context["trial_id"], result_evidence=evidence)
    _save_state(args.state_path, state, coordinator)
    finish_record = {
        "schema_version": 1,
        "kind": "r015_trial_finish_record",
        "finished_at_utc": utc_now(),
        "trial_id": context["trial_id"],
        "instance_id": context["instance_id"],
        "arm": context["arm"],
        "profile_sha256": profile_hash,
        "process": process,
        "result_evidence": evidence,
        "lifecycle_state": _ref(args.state_path),
    }
    write_json(context["trial_root"] / "finish-record.json", finish_record)
    return evidence


def _extract_arm_a_candidates(*, args: argparse.Namespace, state: dict[str, Any], coordinator: InstanceBankFreeze, instance_id: str, profile: dict[str, Any], profile_hash: str, trace_ref: Path | None) -> list[dict[str, Any]]:
    instance_root = args.run_dir / "instances" / instance_id
    extraction_dir = instance_root / "event-extraction"
    candidate_path = instance_root / "arm-a-candidate-records.json"
    if candidate_path.is_file():
        return validate_arm_a_candidate_records(_object(candidate_path), instance_id=instance_id)
    if trace_ref is None:
        write_json(candidate_path, {"kind": "r015_arm_a_candidate_records", "status": "no_trajectory_available", "candidate_records": []})
        return []
    if not trace_ref.is_file():
        raise DevelopmentLauncherError(f"{instance_id}: Arm A trajectory reference is not a file: {trace_ref}")
    intent_path = instance_root / "event-extraction-intent.json"
    if intent_path.is_file() and not (extraction_dir / "run-status.json").is_file():
        raise DevelopmentLauncherError(f"{instance_id}: event extraction has an unresolved launch intent; automatic manager-call replay is forbidden")
    if extraction_dir.exists() and not (extraction_dir / "run-status.json").is_file():
        raise DevelopmentLauncherError(f"{instance_id}: existing event extraction directory has no durable run-status; automatic manager-call replay is forbidden")
    source_manifest = instance_root / "event-source-manifest.json"
    write_json(
        source_manifest,
        {
            "schema_version": 1,
            "kind": "r015_arm_a_event_source_manifest",
            "sources": [{"canonical_instance_id": instance_id, "normalized_path": str(trace_ref.resolve())}],
        },
    )
    if not extraction_dir.exists():
        write_json(
            intent_path,
            {
                "kind": "r015_arm_a_event_extraction_intent",
                "created_at_utc": utc_now(),
                "instance_id": instance_id,
                "source_manifest": _ref(source_manifest),
                "runtime_prompt": _ref(args.event_prompt),
                "profile_sha256": profile_hash,
            },
        )
        command = [
            _python_executable(args),
            str(args.event_runner),
            "--run-dir",
            str(extraction_dir),
            "--source-manifest",
            str(source_manifest),
            "--runtime-prompt",
            str(args.event_prompt),
            "--evidence-compaction-prompt",
            str(args.evidence_compaction_prompt),
            "--spec",
            str(args.spec),
            "--decisions",
            str(args.decisions),
            "--prior-run-reference",
            str(args.run_dir / "trials" / sha256_text(_trial_id(instance_id, "A", "development"))[:20] / "launch-intent.json"),
            "--execute-manager",
            "--activate-unlimited-development-ledger",
            "--config",
            str(args.model_config),
            "--ledger",
            str(args.ledger),
        ]
        # The event runner owns creation of its run directory and refuses to
        # adopt an existing one.  Keep launcher stdout/stderr in a sibling
        # directory while it creates the durable extraction directory; doing
        # this before launch would turn every first real extraction into a
        # FileExistsError and leave no manager result.
        process_log_dir = instance_root / "event-extraction-launch"
        process_log_dir.mkdir(parents=True, exist_ok=True)
        process_path = extraction_dir / "process.json"
        stdout_path = process_log_dir / "process.stdout.log"
        stderr_path = process_log_dir / "process.stderr.log"
        started = time.monotonic()
        timed_out = False
        return_code: int | None = None
        spawn_error: OSError | None = None
        try:
            with stdout_path.open("w", encoding="utf-8") as out, stderr_path.open("w", encoding="utf-8") as err:
                completed = subprocess.run(command, cwd=args.repo_root, env=_shell_env(args.repo_root), stdout=out, stderr=err, timeout=int(args.extraction_timeout_seconds), check=False)
            return_code = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
        except OSError as error:
            spawn_error = error
        process = {
            "kind": "r015_arm_a_event_extraction_process",
            "command": command,
            "return_code": return_code,
            "timed_out": timed_out,
            "spawn_error_type": type(spawn_error).__name__ if spawn_error else None,
            "spawn_error": str(spawn_error) if spawn_error else None,
            "elapsed_seconds": time.monotonic() - started,
            "stdout": _ref(stdout_path),
            "stderr": _ref(stderr_path),
        }
        # A spawn/import failure may happen before the child creates its run
        # directory.  Preserve that process boundary beside the intent so the
        # failure remains reviewable while the normal successful path keeps
        # process.json with the extraction outputs.
        if not extraction_dir.exists():
            process_path = instance_root / "event-extraction-process.json"
        write_json(process_path, process)
        if timed_out or spawn_error is not None or return_code != 0 or not (extraction_dir / "run-status.json").is_file():
            raise DevelopmentLauncherError(f"{instance_id}: event extraction process ended without a durable run-status; manager replay is forbidden")
    status = _object(extraction_dir / "run-status.json")
    write_json(instance_root / "arm-a-event-extraction-status.json", status)
    if status.get("status") != "completed":
        write_json(candidate_path, {"kind": "r015_arm_a_candidate_records", "status": "blocked_or_failed", "run_status": status, "candidate_records": []})
        return []
    candidates: list[dict[str, Any]] = []
    for schedule in status.get("schedules", []):
        if not isinstance(schedule, dict):
            continue
        for attempt in schedule.get("attempts", []):
            if not isinstance(attempt, dict) or attempt.get("outcome") not in {"generated", "repaired_generated"}:
                continue
            ordinal_value = attempt.get("initial_attempt_ordinal")
            if isinstance(ordinal_value, bool) or not isinstance(ordinal_value, int) or ordinal_value <= 0:
                raise DevelopmentLauncherError(f"{instance_id}: event extraction generated attempt has an invalid initial_attempt_ordinal")
            result = attempt.get("result")
            skill = result.get("skill") if isinstance(result, dict) else None
            if not isinstance(skill, dict):
                continue
            candidates.append(
                {
                    "skill": deepcopy(skill),
                    "source_instance_ids": [instance_id],
                    "source_instance_ids_raw": [f"terminal-bench/{instance_id}"],
                    "candidate_record": {
                        "kind": "r015_arm_a_event_candidate",
                        "initial_attempt_ordinal": ordinal_value,
                        "candidate_id": attempt.get("candidate_id"),
                        "candidate_fingerprint": attempt.get("candidate_fingerprint"),
                        "attempt_path": str(extraction_dir / "extraction" / "event" / instance_id / f"initial-{ordinal_value:02d}.json"),
                        "source_trace": str(trace_ref.resolve()),
                    },
                }
            )
    checked = validate_arm_a_candidate_records({"candidate_records": candidates}, instance_id=instance_id)
    write_json(candidate_path, {"kind": "r015_arm_a_candidate_records", "status": "completed", "candidate_records": checked})
    return checked


def _selection_manifest(*, instance_id: str, profile_hash: str, assignments: dict[str, Any], path: Path) -> dict[str, Any]:
    if path.is_file():
        value = _object(path)
        if value.get("profile_sha256") != profile_hash or value.get("instance_id") != instance_id:
            raise DevelopmentLauncherError(f"{instance_id}: existing selection manifest differs from the frozen profile")
        return value
    values = []
    for trial_id, assignment in sorted(assignments.items()):
        arm = assignment.get("arm")
        if arm in {"A", "B"}:
            values.append({"trial_id": trial_id, "action": "skip", "reason": f"R015 development arm {arm} is not a Full Lifecycle arm"})
        elif arm == "C":
            values.append({"trial_id": trial_id, "action": "evaluate_all_supplied", "reason": "R015 development C must evaluate every skill actually supplied by the frozen sidecar"})
        else:
            raise DevelopmentLauncherError(f"{instance_id}: unexpected arm in frozen assignment: {arm}")
    value = {
        "kind": "r012_evolution_selection_manifest",
        "schema_version": 1,
        "instance_id": instance_id,
        "profile_sha256": profile_hash,
        "release_order": [_trial_id(instance_id, arm, "development") for arm in ARM_NAMES],
        "selections": values,
    }
    write_json(path, value)
    return value


def _release_instance(*, args: argparse.Namespace, state: dict[str, Any], coordinator: InstanceBankFreeze, instance_id: str, profile: dict[str, Any], profile_hash: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    instance_root = args.run_dir / "instances" / instance_id
    group = coordinator.instances.get(instance_id)
    if not isinstance(group, dict):
        raise DevelopmentLauncherError(f"{instance_id}: instance was not frozen")
    selection = _selection_manifest(instance_id=instance_id, profile_hash=profile_hash, assignments=group["assignments"], path=instance_root / "selection-manifest.json")
    release_path = instance_root / "release.json"
    if release_path.is_file():
        if group.get("release_state") != "released" or group.get("released") is not True:
            raise DevelopmentLauncherError(
                f"{instance_id}: release.json exists while the coordinator release is not durable; manual reconciliation is required"
            )
        released = _object(release_path)
        released_selection = released.get("selection_manifest")
        expected_selection_sha = _ref(instance_root / "selection-manifest.json").get("sha256")
        if (
            released.get("instance_id") != instance_id
            or not isinstance(released_selection, dict)
            or released_selection.get("sha256") != expected_selection_sha
        ):
            raise DevelopmentLauncherError(f"{instance_id}: existing release.json does not match the frozen release manifest")
        return released
    if group.get("release_state") == "released" or group.get("released") is True:
        raise DevelopmentLauncherError(
            f"{instance_id}: coordinator release is durable but release.json is missing; automatic replay is forbidden"
        )
    failed_trials = sorted(
        trial_id
        for trial_id, assignment in group.get("assignments", {}).items()
        if isinstance(assignment, dict)
        and isinstance(assignment.get("result_evidence"), dict)
        and assignment["result_evidence"].get("classification") == "infra_failure"
    )
    if failed_trials:
        # A completed Harbor process is not sufficient evidence for publication:
        # importer/session/binding failures retain raw artifacts but leave the
        # instance unreleased.  Publishing a no-op bank here would incorrectly
        # make a later instance appear to follow a successful A/B/C lifecycle.
        message = (
            f"{instance_id}: cannot publish next-instance banks after infrastructure failures: "
            + ", ".join(failed_trials)
        )
        group["release_state"] = "blocked_after_infrastructure_failure"
        group["release_error"] = {
            "error_type": "DevelopmentLauncherError",
            "error": message,
            "failed_trials": failed_trials,
        }
        _save_state(args.state_path, state, coordinator)
        raise DevelopmentLauncherError(message)
    c_trial = _trial_id(instance_id, "C", "development")
    c_assignment = _assignment(coordinator, c_trial)
    inspected = inspect_full_lifecycle_evidence(trial_id=c_trial, instance_id=instance_id, result_evidence=c_assignment.get("result_evidence"))
    manager_needed = bool(candidates) or bool(inspected["supplied"])
    manager = None
    encoder = None
    evolution_prompt = None
    maintenance_prompt = None
    if manager_needed:
        if not args.execute_manager or args.model_config is None or args.ledger is None:
            raise DevelopmentLauncherError(f"{instance_id}: manager-backed release needs explicit live manager configuration")
        service = _model_service(args.model_config, args.manager_service_name)
        ledger = update_development_ledger_limit(args.ledger, new_limit=None, reason="R014 explicit unlimited-development-ledger activation for R015 A/B/C development release", contract=state["contract"])
        write_json(args.run_dir / "development-ledger-activation.json", {"kind": "r014_unlimited_development_ledger_activation", "ledger_path": str(args.ledger), "ledger_limit": ledger["limit"], "calls_preserved": len(ledger["calls"]), "limit_history": ledger.get("limit_history", [])})
        manager_profile = ManagerProfile(base_url=service["base_url"], model=service["model_id"], max_total_calls=None)
        manager = ManagerClient(manager_profile, args.run_dir / "manager-calls", state["contract"], args.ledger, exact_token_counter=ServerMessageTokenCounter(service["base_url"]))
        revision = profile["runtime"]["minilm_revision"]
        encoder = MiniLMEncoder(repo_id=profile["runtime"]["minilm_repo_id"], revision=revision)
        encoder.load()
        evolution_prompt = args.evolution_prompt.read_text(encoding="utf-8")
        maintenance_prompt = args.maintenance_prompt.read_text(encoding="utf-8")
    executor = R012EvolutionMaintenanceExecutor(
        manager=manager,
        encoder=encoder,
        journal_root=args.run_dir / "manager-journals" / instance_id,
        instance_id=instance_id,
        profile=profile,
        selections={item["trial_id"]: item for item in selection["selections"]},
        evolution_prompt=evolution_prompt,
        maintenance_prompt=maintenance_prompt,
    )
    try:
        released = release_development_instance(
            coordinator,
            instance_id=instance_id,
            repeat_id="development",
            profile=profile,
            selection_manifest=selection,
            arm_a_candidate_records=candidates,
            evolution_executor=executor,
        )
    except BaseException:
        _save_state(args.state_path, state, coordinator)
        raise
    _save_state(args.state_path, state, coordinator)
    result = {
        "kind": "r015_development_instance_release_record",
        "released_at_utc": utc_now(),
        "instance_id": instance_id,
        "selection_manifest": _ref(instance_root / "selection-manifest.json"),
        "candidate_records": candidates,
        "release": released,
        "lifecycle_state": _ref(args.state_path),
    }
    write_json(release_path, result)
    return result


def _trace_for_a(coordinator: InstanceBankFreeze, instance_id: str) -> Path | None:
    assignment = _assignment(coordinator, _trial_id(instance_id, "A", "development"))
    evidence = assignment.get("result_evidence")
    if not isinstance(evidence, dict) or evidence.get("classification") == "infra_failure":
        return None
    trajectory = evidence.get("trajectory_evidence")
    manifest = evidence.get("packet_manifest")
    if not isinstance(trajectory, dict):
        return None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("trajectory_evidence"), dict):
        raise DevelopmentLauncherError(
            f"{assignment['trial_id']}: normalized trajectory exists without an immutable Harbor packet manifest"
        )
    reference = manifest["trajectory_evidence"]
    path_value = reference.get("path")
    if not isinstance(path_value, str) or not path_value:
        raise DevelopmentLauncherError(f"{assignment['trial_id']}: Harbor packet trajectory reference has no path")
    path = Path(path_value)
    if not path.is_file():
        raise DevelopmentLauncherError(f"{assignment['trial_id']}: Harbor packet trajectory file is missing: {path}")
    stated_hash = reference.get("sha256")
    if not isinstance(stated_hash, str) or sha256_file(path) != stated_hash:
        raise DevelopmentLauncherError(f"{assignment['trial_id']}: Harbor packet trajectory hash does not match its manifest")
    return path


def _run(args: argparse.Namespace) -> dict[str, Any]:
    args.run_dir.mkdir(parents=True, exist_ok=True)
    profile = _validate_profile(_object(args.profile), minilm_revision=args.minilm_revision)
    profile_hash = profile_sha256(profile)
    contract = contract_from_files(args.spec, args.decisions)
    instances = _profile_instances(profile, args.instance)
    configured_instances = _profile_instances(profile, None)
    if len(configured_instances) != 2:
        raise DevelopmentLauncherError("R015 development profile must contain exactly two ordered instances")
    if instances != configured_instances:
        raise DevelopmentLauncherError("R015 development requires the complete two-instance sequence; --instance cannot shrink the authorized lifecycle")
    if args.execute_manager and (args.model_config is None or args.ledger is None):
        raise DevelopmentLauncherError("--execute-manager requires --model-config and --ledger")
    if args.execute_manager and not args.activate_unlimited_development_ledger:
        raise DevelopmentLauncherError("--execute-manager requires --activate-unlimited-development-ledger")
    if not args.execute_manager and not args.check_config:
        raise DevelopmentLauncherError("a live R015 development run requires --execute-manager")
    if not args.harbor.is_file():
        raise DevelopmentLauncherError(f"official Harbor executable is not a file: {args.harbor}")
    if not args.python.is_file():
        raise DevelopmentLauncherError(f"Python runtime is not a file: {args.python}")
    if not args.plugin_path.is_dir() or not (args.plugin_path / "openclaw.plugin.json").is_file():
        raise DevelopmentLauncherError(f"public CODESKILL plugin path is incomplete: {args.plugin_path}")
    required_files = (
        (args.sidecar_script, "R012 sidecar script"),
        (args.event_runner, "R012 event extraction runner"),
        (args.event_prompt, "event extraction prompt"),
        (args.evidence_compaction_prompt, "evidence compaction prompt"),
        (args.evolution_prompt, "evolution prompt"),
        (args.maintenance_prompt, "maintenance prompt"),
    )
    for path, label in required_files:
        if not path.is_file():
            raise DevelopmentLauncherError(f"{label} is not a file: {path}")
    if args.model_config is not None and not args.model_config.is_file():
        raise DevelopmentLauncherError(f"model configuration is not a file: {args.model_config}")
    if args.ledger is not None and not args.ledger.is_file():
        raise DevelopmentLauncherError(f"development ledger is not a file: {args.ledger}")
    if not isinstance(args.sidecar_advertised_host, str) or not args.sidecar_advertised_host.strip():
        raise DevelopmentLauncherError("--sidecar-advertised-host must be a nonempty host or address")
    if args.sidecar_port_base < 1 or args.sidecar_port_base + len(instances) * len(ARM_NAMES) > 65535:
        raise DevelopmentLauncherError("R015 sidecar port range is outside 1..65535")
    for instance in instances:
        task_path = args.task_root / instance
        if not task_path.is_dir() or not (task_path / "task.toml").is_file():
            raise DevelopmentLauncherError(f"official TB2.1 task path is missing: {task_path}")
    service = _model_service(args.model_config, args.manager_service_name) if args.model_config is not None else None
    if args.check_config:
        return {
            "status": "valid",
            "kind": "r015_development_launcher_check",
            "profile_sha256": profile_hash,
            "contract": contract,
            "instances": instances,
            "harbor": _ref(args.harbor),
            "python": _ref(args.python),
            "plugin": _ref(args.plugin_path / "openclaw.plugin.json"),
            "model_service": {"base_url": service["base_url"], "model_id": service["model_id"]} if service else None,
            "formal_campaign": "not_started",
        }
    state_path = args.state_path
    if state_path.is_file():
        state = _state_value(state_path, profile_hash, contract)
        recorded_development = state.get("development")
        if not isinstance(recorded_development, dict) or recorded_development.get("selected_instances") != instances:
            raise DevelopmentLauncherError("lifecycle state selected instances differ from this complete two-instance run")
        if args.arm_bank:
            raise DevelopmentLauncherError("existing lifecycle state owns its frozen/live banks; do not replace --arm-bank inputs")
        coordinator = InstanceBankFreeze.from_dict(state["coordinator"])
    else:
        if args.run_dir != state_path.parent and any(args.run_dir.iterdir()):
            raise DevelopmentLauncherError("a nonempty run directory without lifecycle state cannot be adopted automatically")
        banks: dict[str, SkillBank] = {}
        bank_sources: dict[str, Any] = {}
        supplied = {}
        for arm, path in args.arm_bank:
            if arm in supplied:
                raise DevelopmentLauncherError(f"duplicate --arm-bank for {arm}")
            if not path.is_file():
                raise DevelopmentLauncherError(f"arm {arm} bank is not a file: {path}")
            banks[arm] = SkillBank.load(path)
            supplied[arm] = path
            bank_sources[arm] = _ref(path)
        if "A" not in banks:
            banks["A"] = SkillBank.empty("terminal-bench")
            bank_sources["A"] = {"kind": "constructed_empty_baseline_bank", "benchmark": "terminal-bench"}
        for required_arm in ("B", "C"):
            if required_arm not in banks:
                raise DevelopmentLauncherError(f"first R015 freeze requires --arm-bank {required_arm}=PATH")
        if set(banks) != set(ARM_NAMES):
            raise DevelopmentLauncherError("first R015 freeze requires exactly A/B/C banks")
        benchmarks = {bank.benchmark for bank in banks.values()}
        if benchmarks != {"terminal-bench"}:
            raise DevelopmentLauncherError("R015 development banks must all use benchmark terminal-bench")
        repeat_id = profile["development"]["repeat_id"]
        coordinator = InstanceBankFreeze(banks, repeat_ids=(repeat_id,))
        state = {
            "schema_version": 1,
            "kind": "r012_instance_lifecycle_state",
            "created_at_utc": utc_now(),
            "profile": profile,
            "profile_sha256": profile_hash,
            "profile_source": _ref(args.profile),
            "contract": contract,
            "bank_sources": bank_sources,
            "development": {
                "selected_instances": instances,
                "formal_campaign_started": False,
                "runner": "scripts/run_m3_r015_development.py",
                "source_revision": os.environ.get("CODESKILL_SOURCE_REVISION", "unknown-source-revision"),
            },
        }
        state["contract_snapshot"] = write_contract_snapshot(args.run_dir, args.spec, args.decisions, contract)
        _save_state(state_path, state, coordinator)
    write_json(
        args.run_dir / "manifest.json",
        {
            "schema_version": 1,
            "kind": "r015_two_instance_abc_development_manifest",
            "created_at_utc": state.get("created_at_utc", utc_now()),
            "updated_at_utc": utc_now(),
            "profile": _ref(args.profile),
            "profile_sha256": profile_hash,
            "contract": contract,
            "selected_instances": instances,
            "repeat_id": profile["development"]["repeat_id"],
            "official_harbor": _ref(args.harbor),
            "python_runtime": _ref(args.python),
            "official_task_root": str(args.task_root.resolve()),
            "plugin_path": _ref(args.plugin_path / "openclaw.plugin.json"),
            "model_service": {"base_url": service["base_url"], "model_id": service["model_id"]} if service else None,
            "formal_campaign": "not_started",
        },
    )
    for ordinal, instance_id in enumerate(instances, start=1):
        existing = coordinator.instances.get(instance_id)
        if isinstance(existing, dict) and existing.get("release_state") == "released":
            release_path = args.run_dir / "instances" / instance_id / "release.json"
            if not release_path.is_file():
                raise DevelopmentLauncherError(f"{instance_id}: coordinator is released but its release.json is missing")
            continue
        if existing is None:
            assignments = coordinator.freeze(instance_id)
            _save_state(state_path, state, coordinator)
            write_json(args.run_dir / "instances" / instance_id / "freeze.json", {"kind": "r015_instance_freeze_record", "instance_id": instance_id, "assignments": assignments, "lifecycle_state": _ref(state_path)})
        elif existing.get("release_state") != "pending":
            raise DevelopmentLauncherError(f"{instance_id}: lifecycle state is {existing.get('release_state')}, automatic replay is forbidden")
        for arm_index, arm in enumerate(ARM_NAMES):
            trial_id = _trial_id(instance_id, arm, profile["development"]["repeat_id"])
            assignment = _assignment(coordinator, trial_id)
            if assignment.get("status") == "finished":
                finish_path = args.run_dir / "trials" / sha256_text(trial_id)[:20] / "finish-record.json"
                if not finish_path.is_file():
                    raise DevelopmentLauncherError(f"{trial_id}: coordinator is finished but its durable finish-record.json is missing")
                continue
            if service is None:
                raise DevelopmentLauncherError("live R015 run needs a model service configuration")
            context = _build_trial_context(
                args=args,
                profile=profile,
                profile_hash=profile_hash,
                state_path=state_path,
                state_hash=sha256_file(state_path),
                assignment=assignment,
                service=service,
                task_path=args.task_root / instance_id,
                ordinal=(ordinal - 1) * 3 + arm_index + 1,
            )
            _finish_trial(args=args, state=state, coordinator=coordinator, context=context, profile_hash=profile_hash)
            write_json(args.run_dir / "run-status.json", {"status": "in_progress", "last_trial_id": trial_id, "lifecycle_state": _ref(state_path), "instances": deepcopy(coordinator.instances), "formal_campaign": "not_started"})
        trace_ref = _trace_for_a(coordinator, instance_id)
        candidates = _extract_arm_a_candidates(args=args, state=state, coordinator=coordinator, instance_id=instance_id, profile=profile, profile_hash=profile_hash, trace_ref=trace_ref)
        _release_instance(args=args, state=state, coordinator=coordinator, instance_id=instance_id, profile=profile, profile_hash=profile_hash, candidates=candidates)
        write_json(args.run_dir / "run-status.json", {"status": "in_progress", "released_instance_id": instance_id, "lifecycle_state": _ref(state_path), "instances": deepcopy(coordinator.instances), "formal_campaign": "not_started"})
    final = {
        "status": "completed_development_instances",
        "kind": "r015_two_instance_abc_development_result",
        "completed_instances": [instance for instance in instances if isinstance(coordinator.instances.get(instance), dict) and coordinator.instances[instance].get("release_state") == "released"],
        "lifecycle_state": _ref(state_path),
        "profile_sha256": profile_hash,
        "formal_campaign": "not_started",
        "naturally_unobserved": "Any task/event/native-compaction path absent from raw official trial evidence remains unobserved.",
    }
    write_json(args.run_dir / "run-status.json", final)
    return final


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--state", dest="state_path", type=Path)
    parser.add_argument("--profile", type=Path, default=Path("configs/m3-r015-development.json"))
    parser.add_argument("--spec", type=Path, default=Path("docs/archive/REPRODUCTION_SPEC.md"))
    parser.add_argument("--decisions", type=Path, default=Path("docs/archive/RESEARCH_DECISIONS.md"))
    parser.add_argument("--model-config", type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--manager-service-name", default="deepseek_flash")
    parser.add_argument("--arm-bank", type=_arm_bank, action="append", default=[])
    parser.add_argument("--instance", action="append")
    parser.add_argument("--harbor", type=Path, required=True)
    parser.add_argument("--task-root", type=Path, required=True)
    parser.add_argument("--plugin-path", type=Path, required=True)
    parser.add_argument("--python", type=Path, help="Python runtime for the sidecar and manager runner (must include MiniLM dependencies)")
    parser.add_argument("--sidecar-script", type=Path, default=Path("scripts/run_openclaw_r012_sidecar.py"))
    parser.add_argument("--event-runner", type=Path, default=Path("scripts/run_m2_r012_event_extraction.py"))
    parser.add_argument("--event-prompt", type=Path, default=Path("prompts/custom/r012_fig07_event_extraction_evidence.md"))
    parser.add_argument("--evidence-compaction-prompt", type=Path, default=Path("prompts/custom/m2_evidence_compaction_v2.md"))
    parser.add_argument("--evolution-prompt", type=Path, default=Path("prompts/paper/fig08_evolution.md"))
    parser.add_argument("--maintenance-prompt", type=Path, default=Path("prompts/paper/fig09_maintenance.md"))
    parser.add_argument("--minilm-revision")
    parser.add_argument("--sidecar-port-base", type=int, default=18500)
    parser.add_argument("--sidecar-listen-host", default="0.0.0.0")
    parser.add_argument("--sidecar-advertised-host", default="172.17.0.1")
    parser.add_argument("--sidecar-startup-timeout", type=int, default=900)
    parser.add_argument("--outer-timeout-seconds", type=int, default=2700)
    parser.add_argument("--extraction-timeout-seconds", type=int, default=3600)
    parser.add_argument("--execute-manager", action="store_true")
    parser.add_argument("--activate-unlimited-development-ledger", action="store_true")
    parser.add_argument("--check-config", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    invocation_root = Path.cwd().resolve()
    source_root = Path(__file__).resolve().parents[1]

    def resolve_input(path: Path, *, prefer_source: bool = False, follow_symlinks: bool = True) -> Path:
        if path.is_absolute():
            return path.resolve() if follow_symlinks else path.absolute()
        invocation_path = invocation_root / path
        source_path = source_root / path
        if prefer_source and source_path.exists():
            return source_path.resolve() if follow_symlinks else source_path.absolute()
        if invocation_path.exists() or not source_path.exists():
            return invocation_path.resolve() if follow_symlinks else invocation_path.absolute()
        return source_path.resolve() if follow_symlinks else source_path.absolute()

    args.run_dir = (invocation_root / args.run_dir).resolve() if not args.run_dir.is_absolute() else args.run_dir.resolve()
    # The launcher and all repository-owned helper scripts must run with the
    # committed source root on PYTHONPATH even when invoked from another cwd.
    args.repo_root = source_root
    args.profile = resolve_input(args.profile, prefer_source=True)
    args.spec = resolve_input(args.spec, prefer_source=True)
    args.decisions = resolve_input(args.decisions, prefer_source=True)
    args.state_path = (args.state_path or (args.run_dir / "lifecycle.json")).resolve()
    args.harbor = resolve_input(args.harbor)
    args.task_root = resolve_input(args.task_root)
    args.plugin_path = resolve_input(args.plugin_path)
    args.sidecar_script = resolve_input(args.sidecar_script, prefer_source=True)
    args.event_runner = resolve_input(args.event_runner, prefer_source=True)
    args.event_prompt = resolve_input(args.event_prompt, prefer_source=True)
    args.evidence_compaction_prompt = resolve_input(args.evidence_compaction_prompt, prefer_source=True)
    args.evolution_prompt = resolve_input(args.evolution_prompt, prefer_source=True)
    args.maintenance_prompt = resolve_input(args.maintenance_prompt, prefer_source=True)
    args.model_config = resolve_input(args.model_config) if args.model_config is not None else None
    args.ledger = resolve_input(args.ledger) if args.ledger is not None else None
    args.arm_bank = [(arm, resolve_input(path)) for arm, path in args.arm_bank]
    # Preserve an explicitly selected virtualenv launcher symlink.  Resolving
    # it to the base interpreter would drop that environment's site-packages
    # (notably sentence-transformers) on Python installations built with
    # ``venv --system-site-packages``.
    args.python = resolve_input(args.python, follow_symlinks=False) if args.python is not None else Path(sys.executable)
    try:
        result = _run(args)
    except (DevelopmentLauncherError, R012ExecutionError, DevelopmentRunnerError, HarborTrialEvidenceError, TrialScheduleError, OSError, ValueError, json.JSONDecodeError) as error:
        if not args.check_config:
            try:
                write_json(args.run_dir / "run-status.json", {"status": "blocked_or_failed", "error_type": type(error).__name__, "error": str(error), "formal_campaign": "not_started"})
            except OSError:
                pass
        raise SystemExit(f"{type(error).__name__}: {error}") from error
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
