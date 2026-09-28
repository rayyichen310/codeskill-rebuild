"""Audit the immutable historical coding baseline without importing its data.

The resulting manifest is metadata only.  It records the exact task order,
trial paths, hashes, outcome classification, and observed OpenClaw runtime
signals.  It deliberately does not copy sessions, trajectories, answers, or
verifier payloads into a new C-only run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DATASET_REF = "sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a"
DATASET_NAME = "terminal-bench/terminal-bench-2-1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _duration(started: Any, finished: Any) -> float | None:
    if not isinstance(started, str) or not isinstance(finished, str):
        return None
    try:
        start = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end = datetime.fromisoformat(finished.replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((end - start).total_seconds(), 6)


def _hash_files(trial: Path, root_config: Path, root_lock: Path) -> dict[str, Any]:
    relative_paths = [
        "result.json",
        "config.json",
        "agent/openclaw.effective.json",
        "agent/run.env",
        "agent/openclaw.txt",
        "agent/openclaw.session.jsonl",
        "agent/trajectory.json",
        "agent/instruction.txt",
        "verifier/reward.txt",
        "verifier/verifier.log",
        "agent/agent_state/main/openclaw-agent.sqlite",
    ]
    files: dict[str, dict[str, Any]] = {}
    for relative in relative_paths:
        path = trial / relative
        if path.is_file():
            files[relative] = {"path": str(path), "sha256": _sha256(path), "size_bytes": path.stat().st_size}
    return {
        "root_config": {"path": str(root_config), "sha256": _sha256(root_config)},
        "root_lock": {"path": str(root_lock), "sha256": _sha256(root_lock)},
        "trial_files": files,
    }


def _session_observation(session_path: Path) -> dict[str, Any]:
    if not session_path.is_file():
        return {
            "present": False,
            "record_count": 0,
            "model_calls": 0,
            "tool_calls": 0,
            "tool_results": 0,
            "thinking_blocks": 0,
            "usage": {"input": 0, "cache_read": 0, "output": 0, "total": 0},
            "record_types": {},
            "wire_observation": {},
        }

    record_types: dict[str, int] = {}
    messages = 0
    model_calls = 0
    tool_calls = 0
    tool_results = 0
    thinking_blocks = 0
    usage = {"input": 0, "cache_read": 0, "output": 0, "total": 0}
    providers: set[str] = set()
    models: set[str] = set()
    apis: set[str] = set()
    stop_reasons: set[str] = set()

    for raw_line in session_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        record_type = record.get("type")
        if isinstance(record_type, str):
            record_types[record_type] = record_types.get(record_type, 0) + 1
        if record_type != "message":
            continue
        messages += 1
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "assistant":
            provider = message.get("provider")
            model = message.get("model")
            api = message.get("api")
            if isinstance(provider, str):
                providers.add(provider)
            if isinstance(model, str):
                models.add(model)
            if isinstance(api, str):
                apis.add(api)
            if provider and model and api:
                model_calls += 1
            stop_reason = message.get("stopReason")
            if isinstance(stop_reason, str):
                stop_reasons.add(stop_reason)
            message_usage = message.get("usage")
            if isinstance(message_usage, dict):
                for source, target in (
                    ("input", "input"),
                    ("cacheRead", "cache_read"),
                    ("output", "output"),
                    ("totalTokens", "total"),
                ):
                    value = message_usage.get(source)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        usage[target] += int(value)
            content = message.get("content")
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "thinking":
                        thinking_blocks += 1
                    if item.get("type") == "toolCall":
                        tool_calls += 1
        elif role == "toolResult":
            tool_results += 1

    return {
        "present": True,
        "record_count": sum(record_types.values()),
        "message_count": messages,
        "record_types": dict(sorted(record_types.items())),
        "model_calls": model_calls,
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "thinking_blocks": thinking_blocks,
        "usage": usage,
        "wire_observation": {
            "providers": sorted(providers),
            "models": sorted(models),
            "apis": sorted(apis),
            "stop_reasons": sorted(stop_reasons),
        },
    }


def _effective_observation(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"present": False}
    value = _read_json(path)
    defaults = value.get("agents", {}).get("defaults", {})
    models = defaults.get("models", {})
    model_name = defaults.get("model", {}).get("primary")
    model_config = models.get(model_name, {}) if isinstance(models, dict) else {}
    params = model_config.get("params", {}) if isinstance(model_config, dict) else {}
    providers = value.get("models", {}).get("providers", {})
    provider_id = model_name.split("/", 1)[0] if isinstance(model_name, str) and "/" in model_name else None
    provider = providers.get(provider_id, {}) if isinstance(providers, dict) and provider_id else {}
    provider_models = provider.get("models", []) if isinstance(provider, dict) else []
    selected_model = provider_models[0] if isinstance(provider_models, list) and provider_models and isinstance(provider_models[0], dict) else {}
    return {
        "present": True,
        "primary_model": model_name,
        "thinking_default": defaults.get("thinkingDefault"),
        "context_tokens": defaults.get("contextTokens"),
        "max_concurrent": defaults.get("maxConcurrent"),
        "compaction": defaults.get("compaction"),
        "params": params,
        "provider_id": provider_id,
        "provider_base_url": provider.get("baseUrl") if isinstance(provider, dict) else None,
        "provider_timeout_seconds": provider.get("timeoutSeconds") if isinstance(provider, dict) else None,
        "provider_model": selected_model,
    }


def _wire_log_observation(path: Path) -> dict[str, Any]:
    """Extract non-secret request facts from OpenClaw's debug log."""
    if not path.is_file():
        return {"present": False}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    starts = [line for line in lines if "[model-fetch] start" in line]
    responses = [line for line in lines if "[model-fetch] response" in line]
    run_starts = [line for line in lines if "embedded run start" in line]
    temperature_values: set[str] = set()
    for line in lines:
        match = re.search(r"creating streamFn wrapper with params:\s*(\{.*\})", line)
        if match:
            try:
                params = json.loads(match.group(1))
            except json.JSONDecodeError:
                params = {}
            if isinstance(params, dict) and "temperature" in params:
                temperature_values.add(str(params["temperature"]))
    endpoint_urls = sorted(
        {
            match.group(1)
            for line in starts
            if (match := re.search(r"\burl=(\S+)", line)) is not None
        }
    )
    timeout_values = sorted(
        {
            match.group(1)
            for line in starts
            if (match := re.search(r"\btimeoutMs=(\d+)", line)) is not None
        }
    )
    status_values = sorted(
        {
            match.group(1)
            for line in responses
            if (match := re.search(r"\bstatus=(\d+)", line)) is not None
        }
    )
    return {
        "present": True,
        "embedded_run_start_count": len(run_starts),
        "model_fetch_start_count": len(starts),
        "model_fetch_response_count": len(responses),
        "extra_body_reasoning_effort_log_count": sum("extra_body overwriting request payload keys: reasoning_effort" in line for line in lines),
        "temperature_values_logged": sorted(temperature_values),
        "endpoint_urls": endpoint_urls,
        "timeout_ms_values": timeout_values,
        "response_statuses": status_values,
        "thinking_values_logged": sorted(
            {
                match.group(1)
                for line in run_starts
                if (match := re.search(r"\bthinking=([^\s]+)", line)) is not None
            }
        ),
        "model_values_logged": sorted(
            {
                match.group(1)
                for line in run_starts
                if (match := re.search(r"\bmodel=([^\s]+)", line)) is not None
            }
        ),
        "provider_values_logged": sorted(
            {
                match.group(1)
                for line in run_starts
                if (match := re.search(r"\bprovider=([^\s]+)", line)) is not None
            }
        ),
    }


def _outcome(result: dict[str, Any], trial: Path) -> dict[str, Any]:
    exception = result.get("exception_info")
    reward_path = trial / "verifier" / "reward.txt"
    reward_text = reward_path.read_text(encoding="utf-8", errors="replace").strip() if reward_path.is_file() else None
    reward: float | None = None
    if reward_text:
        try:
            reward = float(reward_text.splitlines()[0].strip())
        except ValueError:
            reward = None
    if isinstance(exception, dict):
        return {
            "classification": "infra_failure",
            "reward": reward,
            "reward_text": reward_text,
            "exception_type": exception.get("exception_type"),
            "exception_message": exception.get("exception_message"),
            "agent_calls_observed": bool(result.get("agent_execution")),
        }
    return {
        "classification": "completed",
        "reward": reward,
        "reward_text": reward_text,
        "exception_type": None,
        "exception_message": None,
        "agent_calls_observed": True,
    }


def audit(root: Path) -> dict[str, Any]:
    directories = sorted(path for path in root.glob("2026-*") if path.is_dir())
    if not directories:
        raise ValueError(f"no timestamped baseline runs under {root}")
    tasks: list[dict[str, Any]] = []
    for order, directory in enumerate(directories, start=1):
        trial_dirs = sorted(path for path in directory.iterdir() if path.is_dir() and "__" in path.name)
        if len(trial_dirs) != 1:
            raise ValueError(f"expected one trial directory in {directory}, found {len(trial_dirs)}")
        trial = trial_dirs[0]
        result_path = trial / "result.json"
        root_config = directory / "config.json"
        root_lock = directory / "lock.json"
        result = _read_json(result_path)
        task_name = result.get("task_name")
        if not isinstance(task_name, str) or not task_name:
            task_name = trial.name.split("__", 1)[0]
        canonical_id = task_name.removeprefix("terminal-bench/")
        task_id = result.get("task_id") if isinstance(result.get("task_id"), dict) else {}
        task_digest = task_id.get("ref")
        files = _hash_files(trial, root_config, root_lock)
        tasks.append(
            {
                "order": order,
                "task_name": task_name,
                "canonical_instance_id": canonical_id,
                "task_digest": task_digest,
                "timestamp_directory": str(directory),
                "trial_directory": str(trial),
                "trial_name": result.get("trial_name", trial.name),
                "trial_id": result.get("id"),
                "source": result.get("source", DATASET_NAME),
                "outcome": _outcome(result, trial),
                "timing": {
                    "started_at": result.get("started_at"),
                    "finished_at": result.get("finished_at"),
                    "duration_seconds": _duration(result.get("started_at"), result.get("finished_at")),
                    "agent_setup_seconds": _duration(
                        (result.get("agent_setup") or {}).get("started_at"),
                        (result.get("agent_setup") or {}).get("finished_at"),
                    ),
                },
                "execution": _session_observation(trial / "agent" / "openclaw.session.jsonl"),
                "effective_runtime": _effective_observation(trial / "agent" / "openclaw.effective.json"),
                "wire_log": _wire_log_observation(trial / "agent" / "openclaw.txt"),
                "agent_info": result.get("agent_info"),
                "task_config": result.get("config", {}).get("task", {}),
                "hashes": files,
            }
        )

    completed = [item for item in tasks if item["outcome"]["classification"] == "completed"]
    infra = [item for item in tasks if item["outcome"]["classification"] == "infra_failure"]
    rewarded = [item for item in completed if item["outcome"].get("reward") == 1.0]
    first_config = _read_json(directories[0] / "config.json")
    first_lock = _read_json(directories[0] / "lock.json")
    first_agent = (first_config.get("agents") or [{}])[0]
    return {
        "schema_version": 1,
        "kind": "r015_legacy_coding_baseline_manifest",
        "generated_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": {
            "root": str(root),
            "dataset_name": DATASET_NAME,
            "dataset_ref": DATASET_REF,
            "dataset_ref_observed": first_config.get("datasets", [{}])[0].get("ref") if first_config.get("datasets") else None,
            "root_is_historical_only": True,
            "skills_imported": False,
            "trajectories_imported": False,
        },
        "baseline_protocol": {
            "task_count": len(tasks),
            "completed_count": len(completed),
            "infra_failure_count": len(infra),
            "reward_one_count": len(rewarded),
            "n_concurrent_trials": first_config.get("n_concurrent_trials"),
            "agent_timeout_multiplier": first_config.get("agent_timeout_multiplier"),
            "agent_name": first_agent.get("name"),
            "model_name": first_agent.get("model_name"),
            "kwargs": first_agent.get("kwargs"),
            "environment": first_config.get("environment"),
            "harbor_version": first_lock.get("harbor", {}).get("version"),
            "retry": first_lock.get("retry"),
        },
        "tasks": tasks,
        "comparison_denominator": {
            "historical_trials": len(tasks),
            "historical_completed_trials": len(completed),
            "historical_infra_trials": len(infra),
            "c_only_rounds": 2,
            "c_only_planned_trials": len(tasks) * 2,
            "c_only_formal_start_requires_user_confirmation": True,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="historical baseline root")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = audit(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "tasks": len(value["tasks"]), "completed": value["baseline_protocol"]["completed_count"], "infra": value["baseline_protocol"]["infra_failure_count"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
