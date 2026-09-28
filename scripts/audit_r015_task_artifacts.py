#!/usr/bin/env python3
"""Audit public Terminal-Bench task artifacts for the C-only preparation.

Only each task's public ``task.toml`` and optional Docker image metadata are
read.  Solution and verifier source files are deliberately outside this audit.
The output is metadata used to bind a prepared trial to the exact public task
version and task-derived timeouts; it is not solver input.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from codeskill_rebuild.types import sha256_file, utc_now, write_json


def _docker_id(image: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"status": "unavailable", "error_type": type(error).__name__, "error": str(error)}
    if completed.returncode != 0 or not completed.stdout.strip():
        return {"status": "unavailable", "returncode": completed.returncode, "stderr": completed.stderr.strip()}
    try:
        decoded = json.loads(completed.stdout)
        entry = decoded[0] if isinstance(decoded, list) and decoded else None
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        return {
            "status": "inspect_parse_error",
            "returncode": completed.returncode,
            "error_type": type(error).__name__,
            "error": str(error),
            "stdout_sha256": _sha256_text(completed.stdout),
        }
    if not isinstance(entry, dict):
        return {"status": "inspect_parse_error", "returncode": completed.returncode, "error": "docker inspect returned no image object"}
    config = entry.get("Config") if isinstance(entry.get("Config"), dict) else {}
    return {
        "status": "observed",
        "image_id": entry.get("Id"),
        "repo_digests": entry.get("RepoDigests", []),
        "created": entry.get("Created"),
        "architecture": entry.get("Architecture"),
        "os": entry.get("Os"),
        "config_user": config.get("User"),
        "config_working_dir": config.get("WorkingDir"),
        "rootfs_layers": (entry.get("RootFS") or {}).get("Layers", []) if isinstance(entry.get("RootFS"), dict) else [],
        "stdout_sha256": _sha256_text(completed.stdout),
        "stderr_sha256": _sha256_text(completed.stderr),
    }


def _sha256_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _resolve_public_task_dir(task_root: Path, task_id: str) -> Path:
    """Resolve one public task directory without entering hidden material."""
    candidates = [task_root / task_id, task_root / "terminal-bench" / task_id]
    task_path = next((candidate.resolve() for candidate in candidates if (candidate / "task.toml").is_file()), None)
    if task_path is None:
        raise FileNotFoundError(f"public task.toml not found under {task_root}: tried {candidates}")
    return task_path


def audit(
    task_root: Path,
    task_ids: list[str],
    *,
    include_docker: bool = True,
    historical_task_root: Path | None = None,
) -> dict[str, Any]:
    import tomllib

    tasks: list[dict[str, Any]] = []
    for order, task_id in enumerate(task_ids, start=1):
        # TB2.1 has appeared with both ``tasks/<task>`` and
        # ``tasks/terminal-bench/<task>`` layouts.  Resolve the public task
        # directory explicitly instead of silently selecting a similarly
        # named directory or requiring a particular checkout wrapper.
        task_path = _resolve_public_task_dir(task_root, task_id)
        task_toml = task_path / "task.toml"
        parsed = tomllib.loads(task_toml.read_text(encoding="utf-8"))
        task = parsed.get("task") if isinstance(parsed, dict) else {}
        environment = parsed.get("environment") if isinstance(parsed, dict) else {}
        agent = parsed.get("agent") if isinstance(parsed, dict) else {}
        verifier = parsed.get("verifier") if isinstance(parsed, dict) else {}
        if not isinstance(task, dict) or task.get("name") != f"terminal-bench/{task_id}":
            raise ValueError(f"public task identity mismatch in {task_toml}")
        image = environment.get("docker_image") if isinstance(environment, dict) else None
        record: dict[str, Any] = {
            "order": order,
            "task_id": task_id,
            "task_name": task.get("name"),
            "task_path": str(task_path),
            "task_toml": {"path": str(task_toml), "sha256": sha256_file(task_toml), "size_bytes": task_toml.stat().st_size},
            "public_task": {
                "description": task.get("description"),
                "difficulty": (parsed.get("metadata") or {}).get("difficulty") if isinstance(parsed.get("metadata"), dict) else None,
                "category": (parsed.get("metadata") or {}).get("category") if isinstance(parsed.get("metadata"), dict) else None,
            },
            "environment": {
                "docker_image": image,
                "build_timeout_sec": environment.get("build_timeout_sec") if isinstance(environment, dict) else None,
                "cpus": environment.get("cpus") if isinstance(environment, dict) else None,
                "memory_mb": environment.get("memory_mb") if isinstance(environment, dict) else None,
                "storage_mb": environment.get("storage_mb") if isinstance(environment, dict) else None,
                "gpus": environment.get("gpus") if isinstance(environment, dict) else None,
                "allow_internet": environment.get("allow_internet") if isinstance(environment, dict) else None,
            },
            "agent_timeout_sec": agent.get("timeout_sec") if isinstance(agent, dict) else None,
            "verifier_timeout_sec": verifier.get("timeout_sec") if isinstance(verifier, dict) else None,
            "hidden_material_read": False,
        }
        if historical_task_root is not None:
            historical_path = _resolve_public_task_dir(historical_task_root, task_id)
            historical_toml = historical_path / "task.toml"
            historical_sha256 = sha256_file(historical_toml)
            current_sha256 = record["task_toml"]["sha256"]
            record["historical_public_task_toml"] = {
                "path": str(historical_toml),
                "sha256": historical_sha256,
                "size_bytes": historical_toml.stat().st_size,
                "matches_prepared_public_toml": historical_sha256 == current_sha256,
                "comparison_scope": "task.toml bytes only; no solution or verifier source read",
            }
        if include_docker and isinstance(image, str) and image:
            record["docker_image"] = _docker_id(image)
        else:
            record["docker_image"] = {"status": "not_requested"}
        tasks.append(record)
    return {
        "schema_version": 1,
        "kind": "r015_public_tb21_task_artifact_audit",
        "created_at_utc": utc_now(),
        "task_root": str(task_root.resolve()),
        "historical_task_root": str(historical_task_root.resolve()) if historical_task_root is not None else None,
        "tasks": tasks,
        "hidden_material_read": False,
        "source_policy": "public task.toml and optional docker image inspect only; solution/verifier source was not read",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-root", type=Path, required=True)
    parser.add_argument("--tasks", nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--historical-task-root",
        type=Path,
        help="optional second public task tree used for exact task.toml byte comparison",
    )
    parser.add_argument("--no-docker", action="store_true")
    args = parser.parse_args()
    value = audit(
        args.task_root,
        args.tasks,
        include_docker=not args.no_docker,
        historical_task_root=args.historical_task_root,
    )
    write_json(args.output, value)
    print(json.dumps({"output": str(args.output), "tasks": len(value["tasks"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
