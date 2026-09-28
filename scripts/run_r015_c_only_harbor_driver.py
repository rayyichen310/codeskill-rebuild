#!/usr/bin/env python3
"""Run one durable C-only trial through the official Harbor/OpenClaw path.

The outer C-only coordinator freezes the assignment and owns the ordered
two-round state machine.  This driver owns exactly one assignment at a time:
it starts the public R012 sidecar, asks Harbor to run the official task agent
and verifier, imports the raw result/session/proxy evidence, and performs the
same-round manager extraction and Fig.8/Fig.9 decisions.  The driver never
reads a baseline trajectory or skill bank and never mutates the coordinator
state directly; its JSON output is applied transactionally by
``run_r015_c_only.py``.

``--check-config`` validates the public adapter/sidecar binding and task
metadata without starting Harbor, Docker, a model request, or a campaign.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from codeskill_rebuild.arm_banks import same_granularity_top5  # noqa: E402
from codeskill_rebuild.bank import BankError, SkillBank, validate_skill_candidate  # noqa: E402
from codeskill_rebuild.c_only_protocol import COnlyProtocolError  # noqa: E402
from codeskill_rebuild.event_graph import EventGraphRunner  # noqa: E402
from codeskill_rebuild.event_graph_stages import EventGraphStages  # noqa: E402
from codeskill_rebuild.compaction import action_observation_segments, expand_evidence_fragments  # noqa: E402
from codeskill_rebuild.context import ContextBlocked  # noqa: E402
from codeskill_rebuild.evolution import supplied_skills_for_evolution  # noqa: E402
from codeskill_rebuild.harbor_recovery import (  # noqa: E402
    HarborRecoveryError,
    load_harbor_recovery_manifest,
)
from codeskill_rebuild.harbor_openclaw_adapter import CODESKILLHarborOpenClaw  # noqa: E402
from codeskill_rebuild.manager import (  # noqa: E402
    ManagerCallError,
    ManagerClient,
    ManagerProfile,
    ServerMessageTokenCounter,
    TokenizationError,
    update_development_ledger_limit,
)
from codeskill_rebuild.manager_reconciliation import load_reconciliation_manifest  # noqa: E402
from codeskill_rebuild.manager_projection import (  # noqa: E402
    HISTORICAL_THINKING_POLICY_VERSION,
    ProjectionError,
    project_historical_thinking,
    project_trace_for_manager,
    validate_historical_thinking_policy,
)
from codeskill_rebuild.pipeline import (  # noqa: E402
    maintenance_from_skills_messages,
    validate_maintenance_from_skills,
    validate_budget_summary,
    validate_evidence_summary,
)
from codeskill_rebuild.r012_execution import (  # noqa: E402
    R012EvolutionMaintenanceExecutor,
    R012ExecutionError,
    evolution_messages,
    inspect_full_lifecycle_evidence,
    profile_sha256,
    validate_evolution_output,
    validate_execution_profile,
)
from codeskill_rebuild.retrieval import MiniLMEncoder  # noqa: E402
from codeskill_rebuild.r015_harbor_evidence import (  # noqa: E402
    HarborTrialEvidenceError,
    import_harbor_openclaw_trial,
    non_forwarded_terminal_disposition,
    non_forwarded_terminal_schema,
)
from codeskill_rebuild.task_graph import TaskGraphRunner  # noqa: E402
from codeskill_rebuild.task_graph_model import TaskChatBoundary  # noqa: E402
from codeskill_rebuild.task_graph_stages import TaskGraphStages  # noqa: E402
from codeskill_rebuild.traces import TraceImportError, normalize_openclaw_trial  # noqa: E402
from codeskill_rebuild.types import (  # noqa: E402
    canonical_instance_id,
    canonical_json,
    contract_from_files,
    read_json,
    sha256_file,
    sha256_text,
    utc_now,
    write_json,
)


class COnlyHarborDriverError(RuntimeError):
    """The one-trial official driver cannot produce a safe output."""


# Keep the driver's durable phase vocabulary local.  The coordinator owns the
# same ordered protocol, but importing its private constant would create a
# script-to-script dependency at the subprocess boundary.  Recovery validates
# the original missing-stage boundary before it writes any derived stage.
_DRIVER_PHASES = ("trial", "extraction", "publication")


def _object(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise COnlyHarborDriverError(f"{field} must be an object")
    return value


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise COnlyHarborDriverError(f"{field} must be a nonempty string")
    return value.strip()


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise COnlyHarborDriverError(f"{field} must be a positive integer")
    return value


def _hash_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def _ref(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        return {"path": str(path), "exists": False}
    # Keep the presence marker explicit.  Several driver boundaries use
    # ``exists`` to distinguish a verified immutable file from the deliberate
    # ``exists=false`` absence marker; omitting it for present files makes a
    # valid trajectory/ledger reference look absent during the same process.
    return {"path": str(path), "exists": True, "sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def _write_phase_stage(
    *,
    input_path: Path,
    context: dict[str, Any],
    phase: str,
    payload: dict[str, Any],
    stage_path: Path | None = None,
) -> Path:
    """Persist one immutable completed phase before returning to Harbor.

    The outer coordinator can apply these files after a process exit without
    launching the solver again.  They are intentionally bound to the exact
    driver input bytes and assignment; a missing or changed phase remains a
    reconciliation condition.
    """
    if phase not in {"trial", "extraction", "publication"}:
        raise COnlyHarborDriverError(f"unknown C-only driver phase: {phase}")
    assignment = _object(context.get("assignment"), field="driver context.assignment")
    phase_payload = deepcopy(payload)
    # This marker is produced only by the built-in official driver.  The
    # outer coordinator requires it for production application; controlled
    # parser/IO fixtures use the explicit test-only boundary instead.
    phase_payload.setdefault("evidence_mode", "official_live")
    # Normal launches place stages beside the immutable driver input.  An
    # explicit Harbor artifact recovery uses a separate stage namespace while
    # retaining the original input path/hash inside every stage binding.
    path = stage_path or input_path.with_name(f"driver-stage-{phase}.json")
    path = Path(path).resolve()
    value = {
        "schema_version": 1,
        "kind": "r015_c_only_driver_stage",
        "status": "complete",
        "phase": phase,
        "condition": "C-only",
        "round_id": assignment["round_id"],
        "task_id": assignment["task_id"],
        "trial_id": assignment["trial_id"],
        "session_id": context["session_id"],
        "input_path": str(input_path),
        "input_sha256": sha256_file(input_path),
        "payload_sha256": _hash_json(phase_payload),
        "payload": phase_payload,
        "created_at_utc": utc_now(),
    }
    if path.exists():
        existing = _read_json_file(path, field=f"driver-stage-{phase}")
        if canonical_json(existing) != canonical_json(value):
            raise COnlyHarborDriverError(f"durable driver stage already exists with different bytes: {path}")
    else:
        write_json(path, value)
    return path


def _wait_extraction_ack(
    *, input_value: dict[str, Any], input_path: Path, output_path: Path,
    assignment: dict[str, Any], extraction_stage: Path,
    extraction: dict[str, Any],
) -> None:
    if os.environ.get("CODESKILL_EXTRACT_HANDOFF") != "1":
        return
    handoff_path = output_path.with_name("extraction-awaiting-ack.json")
    ack_path = output_path.with_name("extraction-ack.json")
    if handoff_path.exists() or ack_path.exists():
        raise COnlyHarborDriverError("Task extraction handoff already exists; reconcile before retry")
    handoff = {"kind": "r015_task_extraction_handoff_v1",
               "round_id": assignment["round_id"],
               "task_id": assignment["task_id"],
               "trial_id": assignment["trial_id"],
               "input_sha256": sha256_file(input_path),
               "extraction_stage_path": str(extraction_stage),
               "extraction_stage_sha256": sha256_file(extraction_stage)}
    write_json(handoff_path, handoff)
    deadline = time.monotonic() + 600
    while not ack_path.is_file():
        if time.monotonic() >= deadline:
            raise COnlyHarborDriverError("coordinator did not durably acknowledge Task extraction")
        time.sleep(0.1)
    ack = _read_json_file(ack_path, field="Task extraction coordinator acknowledgement")
    state_path = Path(input_value["state"]["path"])
    if ack.get("kind") != "r015_task_extraction_ack_v1" or \
            ack.get("handoff_sha256") != sha256_file(handoff_path) or \
            ack.get("durable_state_sha256") != sha256_file(state_path):
        raise COnlyHarborDriverError("Task extraction coordinator acknowledgement differs")
    durable = _read_json_file(state_path, field="durable Task extraction state")
    saved_assignment = durable["rounds"][str(assignment["round_id"])]["assignments"][assignment["task_id"]]
    if saved_assignment.get("extraction", {}).get("evidence", {}).get("task", {}).get("graph") != \
            extraction.get("evidence", {}).get("task", {}).get("graph"):
        raise COnlyHarborDriverError("Task Graph extraction was not durably saved before Event Fig.9")


def _read_json_file(path: Path, *, field: str) -> dict[str, Any]:
    try:
        value = read_json(path)
    except (OSError, ValueError) as error:
        raise COnlyHarborDriverError(f"cannot read {field}: {path}: {error}") from error
    return _object(value, field=field)


def _reject_historical(value: Any, *, path: str = "input") -> None:
    forbidden = {
        "baseline_skills",
        "baseline_trajectory",
        "historical_skills",
        "historical_trajectory",
        "old_bank",
        "old_skill_bank",
        "verifier_hidden_answers",
        "solver_hidden_answer",
        "hidden_answer",
        "replay_of_baseline",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key.casefold() in forbidden:
                raise COnlyHarborDriverError(f"{path}.{key} is forbidden in the C-only driver input")
            _reject_historical(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_historical(child, path=f"{path}[{index}]")


def _session_id(trial_id: str) -> str:
    return "r015-" + sha256_text(trial_id)[:28]


def _resolve_driver_setting(driver: dict[str, Any], key: str, *, env_name: str, default: str | None = None) -> str | None:
    value = driver.get(key)
    if isinstance(value, str) and (value.startswith("env:") or value.strip().casefold() == "current interpreter"):
        value = None
    if not isinstance(value, str) or not value.strip():
        value = os.environ.get(env_name, default)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _resolve_path(value: str | None, *, base: Path, field: str) -> Path:
    if value is None:
        raise COnlyHarborDriverError(f"{field} is not configured")
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _resolve_executable_path(value: str | None, *, base: Path, field: str) -> Path:
    """Resolve an executable without following a virtualenv symlink.

    On the audited T2 installation ``.venv/bin/python`` is a symlink to the
    system interpreter.  Calling ``Path.resolve()`` would silently discard
    the virtualenv directory and make the sidecar run with the wrong
    ``sys.prefix`` and dependencies.  ``absolute()`` normalizes the path
    while preserving the executable spelling and therefore its virtualenv
    context.
    """
    if value is None:
        raise COnlyHarborDriverError(f"{field} is not configured")
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path.absolute()


def _task_path(task_root: Path, task_id: str) -> Path:
    candidates = [task_root / task_id, task_root / "terminal-bench" / task_id]
    for candidate in candidates:
        if (candidate / "task.toml").is_file():
            return candidate.resolve()
    raise COnlyHarborDriverError(
        f"official TB2.1 task {task_id} is unavailable under {task_root}; set CODESKILL_TB21_TASK_ROOT"
    )


def _public_git_revision(path: Path) -> str | None:
    """Read the public checkout revision without entering task solutions."""
    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            try:
                completed = subprocess.run(
                    ["git", "-C", str(parent), "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            value = completed.stdout.strip()
            return value if completed.returncode == 0 and value else None
    return None


def _docker_image_identity(image: Any) -> dict[str, Any]:
    """Capture a locally available task image identity without pulling it."""
    if not isinstance(image, str) or not image.strip():
        return {"status": "not_declared", "image": image}
    docker = shutil.which("docker")
    if docker is None:
        return {"status": "docker_unavailable", "image": image}
    command = [docker, "image", "inspect", image.strip()]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"status": "inspect_error", "image": image, "command": command, "error_type": type(error).__name__, "error": str(error)}
    result: dict[str, Any] = {
        # Keep the status vocabulary identical to audit_r015_task_artifacts.py.
        # A pinned task image is only safe to launch when Docker returned a
        # parsed immutable image object; "available" would make the audit and
        # the production binding disagree at the final boundary.
        "status": "observed" if completed.returncode == 0 else "unavailable",
        "image": image,
        "command": command,
        "returncode": completed.returncode,
        "stdout_sha256": sha256_text(completed.stdout),
        "stderr_sha256": sha256_text(completed.stderr),
    }
    if completed.returncode == 0:
        try:
            decoded = json.loads(completed.stdout)
            entry = decoded[0] if isinstance(decoded, list) and decoded else {}
            if isinstance(entry, dict):
                result.update({"image_id": entry.get("Id"), "repo_digests": entry.get("RepoDigests", [])})
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            result.update({"status": "inspect_parse_error", "parse_error": str(error)})
    else:
        result["stderr"] = completed.stderr
    return result


def _task_metadata(
    task_path: Path,
    *,
    expected_task_name: str,
    expected_digest: str | None,
    expected_task_toml_sha256: str | None = None,
    expected_image: str | None = None,
    expected_image_id: str | None = None,
    expected_repo_digests: list[str] | None = None,
) -> dict[str, Any]:
    """Read public task metadata only; never enter solution/hidden verifier files."""
    task_toml = task_path / "task.toml"
    if not task_toml.is_file():
        raise COnlyHarborDriverError(f"official task has no task.toml: {task_toml}")
    try:
        import tomllib

        parsed = tomllib.loads(task_toml.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise COnlyHarborDriverError(f"cannot parse official task.toml: {task_toml}: {error}") from error
    task_section = parsed.get("task") if isinstance(parsed, dict) else None
    if not isinstance(task_section, dict) or task_section.get("name") != expected_task_name:
        raise COnlyHarborDriverError(
            f"official task.toml identity differs: expected {expected_task_name}, got {task_section.get('name') if isinstance(task_section, dict) else None}"
        )
    environment = parsed.get("environment") if isinstance(parsed, dict) else {}
    agent = parsed.get("agent") if isinstance(parsed, dict) else {}
    verifier = parsed.get("verifier") if isinstance(parsed, dict) else {}
    visible_files: list[dict[str, Any]] = []
    # These are public environment/build descriptors, while solution and
    # verifier source are intentionally excluded from this audit.
    for relative in (
        "task.toml",
        "instruction.md",
        "README.md",
        ".gitignore",
        "environment",
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
    ):
        path = task_path / relative
        if path.is_file():
            visible_files.append(_ref(path) | {"relative": relative})
        elif path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file():
                    visible_files.append(_ref(child) | {"relative": child.relative_to(task_path).as_posix()})
    image = environment.get("docker_image") if isinstance(environment, dict) else None
    task_toml_sha256 = sha256_file(task_toml)
    if expected_task_toml_sha256 is not None and task_toml_sha256 != expected_task_toml_sha256:
        raise COnlyHarborDriverError(
            "official task.toml bytes differ from the pinned public task audit: "
            f"expected {expected_task_toml_sha256}, got {task_toml_sha256}"
        )
    if expected_image is not None and image != expected_image:
        raise COnlyHarborDriverError(
            f"official task image tag differs from the pinned public task audit: expected {expected_image}, got {image}"
        )
    image_identity = _docker_image_identity(image)
    if expected_image_id is not None:
        if image_identity.get("status") != "observed" or image_identity.get("image_id") != expected_image_id:
            raise COnlyHarborDriverError(
                "official task Docker image identity differs from the pinned public task audit: "
                + json.dumps(
                    {
                        "expected_image_id": expected_image_id,
                        "observed": image_identity,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
    if expected_repo_digests is not None:
        observed_repo_digests = sorted(str(value) for value in image_identity.get("repo_digests", []))
        if image_identity.get("status") != "observed" or observed_repo_digests != sorted(expected_repo_digests):
            raise COnlyHarborDriverError(
                "official task Docker repo digests differ from the pinned public task audit: "
                + json.dumps(
                    {
                        "expected_repo_digests": sorted(expected_repo_digests),
                        "observed_repo_digests": observed_repo_digests,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
    return {
        "task_name": expected_task_name,
        "task_path": str(task_path),
        "task_toml": _ref(task_toml),
        "pinned_task_toml_sha256": expected_task_toml_sha256,
        "task_checkout_commit": _public_git_revision(task_path),
        "expected_dataset_task_digest": expected_digest,
        "task_digest_comparison": {
            "status": "dataset_ref_is_not_task_toml_sha256",
            "expected": expected_digest,
            "task_toml_sha256": sha256_file(task_toml),
            "verified_by": "Harbor task identity plus official task.toml; dataset ref retained for result comparison",
        },
        "public_environment": {
            "docker_image": image,
            "docker_image_identity": image_identity,
            "pinned_docker_image": expected_image,
            "pinned_docker_image_id": expected_image_id,
            "pinned_docker_repo_digests": deepcopy(expected_repo_digests),
            "build_timeout_sec": environment.get("build_timeout_sec") if isinstance(environment, dict) else None,
            "agent_timeout_sec": agent.get("timeout_sec") if isinstance(agent, dict) else None,
            "verifier_timeout_sec": verifier.get("timeout_sec") if isinstance(verifier, dict) else None,
            "cpus": environment.get("cpus") if isinstance(environment, dict) else None,
            "memory_mb": environment.get("memory_mb") if isinstance(environment, dict) else None,
            "gpus": environment.get("gpus") if isinstance(environment, dict) else None,
            "allow_internet": environment.get("allow_internet") if isinstance(environment, dict) else None,
        },
        "visible_files": visible_files,
        "hidden_material_read": False,
    }


def _effective_task_limits(
    metadata: dict[str, Any],
    *,
    job_value: dict[str, Any],
    agent_setup_timeout_seconds: float,
) -> dict[str, Any]:
    """Materialize Harbor's task-derived phase limits before a trial starts.

    Harbor applies the task's agent/verifier/build values through the job
    multipliers at trial construction time.  Recording the calculation in the
    launch metadata makes the effective limits auditable before Docker starts
    and keeps a later result tied to the exact public task TOML that supplied
    them.  ``None`` is retained when the task omits a phase timeout rather than
    inventing a default here.
    """
    environment = _object(metadata.get("public_environment"), field="task_metadata.public_environment")
    agent_base = environment.get("agent_timeout_sec")
    verifier_base = environment.get("verifier_timeout_sec")
    build_base = environment.get("build_timeout_sec")
    agent_multiplier = job_value.get("agent_timeout_multiplier")
    verifier_multiplier = job_value.get("verifier_timeout_multiplier", job_value.get("timeout_multiplier", 1.0))
    build_multiplier = job_value.get("environment_build_timeout_multiplier", job_value.get("timeout_multiplier", 1.0))
    for value, field in (
        (agent_multiplier, "job.agent_timeout_multiplier"),
        (verifier_multiplier, "job.verifier_timeout_multiplier"),
        (build_multiplier, "job.environment_build_timeout_multiplier"),
        (agent_setup_timeout_seconds, "agent_setup_timeout_seconds"),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise COnlyHarborDriverError(f"{field} must be a positive number")

    def scaled(value: Any, multiplier: float, *, field: str) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise COnlyHarborDriverError(f"task public_environment.{field} must be a positive number or null")
        return float(value) * float(multiplier)

    return {
        "source": "Harbor Trial._compute_*_timeout_sec public implementation",
        "task_base_seconds": {
            "agent": agent_base,
            "agent_setup": None,
            "verifier": verifier_base,
            "environment_build": build_base,
        },
        "multipliers": {
            "agent": float(agent_multiplier),
            "agent_setup": 1.0,
            "verifier": float(verifier_multiplier),
            "environment_build": float(build_multiplier),
        },
        "effective_seconds": {
            "agent": scaled(agent_base, float(agent_multiplier), field="agent_timeout_sec"),
            "agent_setup": float(agent_setup_timeout_seconds),
            "verifier": scaled(verifier_base, float(verifier_multiplier), field="verifier_timeout_sec"),
            "environment_build": scaled(build_base, float(build_multiplier), field="build_timeout_sec"),
        },
        "override_setup_timeout_seconds": float(agent_setup_timeout_seconds),
    }


def _profile_rules(profile: dict[str, Any]) -> dict[str, Any]:
    injection = _object(profile.get("sidecar_injection"), field="retrieval_profile.sidecar_injection")
    task = _object(injection.get("task_selection"), field="retrieval_profile.sidecar_injection.task_selection")
    event = _object(injection.get("event_selection"), field="retrieval_profile.sidecar_injection.event_selection")
    root_event = _object(profile.get("event_selection"), field="retrieval_profile.event_selection")
    return {
        "taskSelection": {
            "selectionRuleRef": _text(task.get("selection_rule_ref"), field="task selection rule"),
            "threshold": task.get("threshold"),
            "maxMatchingSkills": task.get("max_matching_skills"),
        },
        "eventSelection": {
            "profileRef": _text(root_event.get("profile_ref"), field="event profile"),
            "selectionRuleRef": _text(root_event.get("selection_rule_ref"), field="event selection rule"),
            "threshold": event.get("threshold"),
            "maxMatchingSkills": root_event.get("max_matching_skills"),
            "skillTokenBudget": root_event.get("skill_token_budget"),
            "budgetScope": _text(injection.get("event_skill_token_budget_scope"), field="event budget scope"),
        },
    }


def _render_openclaw_config(
    *,
    model_id: str,
    sidecar_url: str,
    plugin_path: Path,
    permit_directory: Path,
    trial_id: str,
    session_id: str,
    audit_path: Path,
    context_tokens: int,
    max_output_tokens: int,
    thinking: str,
    reasoning_effort: str,
    temperature: float,
    top_p: float,
    provider_timeout_seconds: int,
) -> dict[str, Any]:
    """Render through the public adapter's binding method, without patches."""
    adapter = object.__new__(CODESKILLHarborOpenClaw)
    adapter._codeskill_sidecar_base_url = sidecar_url.rstrip("/")
    adapter._codeskill_sidecar_model_id = model_id
    adapter._codeskill_plugin_path = "/opt/codeskill/openclaw-sidecar"
    adapter._codeskill_permit_directory = "/var/lib/codeskill/native-summary-permits"
    adapter._codeskill_plugin_audit_path = "/var/lib/codeskill/native-summary-permits/plugin-audit.jsonl"
    adapter._codeskill_trial_id = trial_id
    adapter._codeskill_session_id = session_id
    adapter._codeskill_context_tokens = context_tokens
    adapter._codeskill_max_output_tokens = max_output_tokens
    adapter._codeskill_thinking = thinking
    adapter._codeskill_reasoning_effort = reasoning_effort
    adapter._codeskill_temperature = temperature
    adapter._codeskill_top_p = top_p
    adapter._codeskill_provider_timeout_seconds = provider_timeout_seconds
    # The paths below are host-side evidence paths used by the sidecar config;
    # the official Harbor adapter renders container paths at runtime.
    rendered = adapter._bind_public_plugin({"gateway": {"mode": "local"}, "models": {}, "agents": {"defaults": {}}, "plugins": {}})
    # ``_bind_public_plugin`` is also used by the official Harbor adapter
    # inside the container, where the paths above are intentionally mounted
    # container paths.  The sidecar check runs on the host, however, and must
    # validate the exact host paths that it will serve.  Keep the adapter
    # rendering as the source of the structure, then bind only these public
    # host-side paths in the host config written beside the sidecar.
    plugins = rendered.setdefault("plugins", {})
    load = plugins.setdefault("load", {})
    load["paths"] = [str(plugin_path.resolve())]
    entries = plugins.setdefault("entries", {})
    entry = entries.setdefault("codeskill-r012-sidecar", {})
    entry_config = entry.setdefault("config", {})
    entry_config.update(
        {
            "permitDirectory": str(permit_directory.resolve()),
            "trialId": trial_id,
            "sessionId": session_id,
            "auditPath": str(audit_path.resolve()),
        }
    )
    return rendered


def _process_group_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": False}


def _stop_process(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, text=True, check=False)
        return
    # Children are deliberately kept in the driver's process group so that
    # the durable outer runner can terminate the complete Harbor/sidecar tree
    # on an explicit timeout.  Killing the child's process group here would
    # therefore also kill this driver.  Stop just this child; outer cleanup
    # owns group-wide termination.
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            return


def _wait_listener(process: subprocess.Popen[Any], host: str, port: int, timeout_seconds: int = 120) -> bool:
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


def _driver_paths(input_value: dict[str, Any]) -> dict[str, Path | str | None]:
    config = _object(input_value.get("config"), field="input.config")
    driver = config.get("driver") if isinstance(config.get("driver"), dict) else {}
    task_root_value = _resolve_driver_setting(driver, "task_root", env_name="CODESKILL_TB21_TASK_ROOT")
    if task_root_value is None:
        candidates = [
            ROOT / "cache" / "parent-official-tb21-metadata" / "tasks",
            Path("/home/<T2_HOST>/ray/hicache-tb21/src/terminal-bench-2-1/tasks"),
        ]
        task_root = next((candidate.resolve() for candidate in candidates if candidate.is_dir()), candidates[0].resolve())
    else:
        task_root = _resolve_path(task_root_value, base=ROOT, field="task_root")
    harbor = _resolve_driver_setting(driver, "harbor_executable", env_name="CODESKILL_HARBOR_BIN", default="harbor")
    if harbor == "harbor":
        # The dedicated T2 installations are not necessarily activated in
        # the caller's PATH. Select the audited binary matching the prepared
        # Harbor version, then retain PATH as a portable fallback. The old
        # 0.17.1 environment is available and public-API compatible with the
        # current adapter, so prefer it when the prepared target uses the
        # historical Harbor version.
        runtime = _object(config.get("runtime_alignment"), field="input.config.runtime_alignment")
        target = _object(runtime.get("prepared_target"), field="input.config.runtime_alignment.prepared_target")
        harbor_version = target.get("harbor_version")
        audited_candidates = (
            [
                Path("/home/<T2_HOST>/anaconda3/envs/terminal-bench2/bin/harbor"),
                Path("/home/<T2_HOST>/ray/codeskill/.venv/bin/harbor"),
            ]
            if harbor_version == "0.17.1"
            else [
                Path("/home/<T2_HOST>/ray/codeskill/.venv/bin/harbor"),
                Path("/home/<T2_HOST>/anaconda3/envs/terminal-bench2/bin/harbor"),
            ]
        )
        audited_harbor = next((candidate for candidate in audited_candidates if candidate.is_file()), None)
        if audited_harbor is not None:
            harbor = str(audited_harbor)
        else:
            harbor_on_path = shutil.which("harbor")
            if harbor_on_path:
                harbor = harbor_on_path
    python_value = _resolve_driver_setting(driver, "python_executable", env_name="CODESKILL_PYTHON", default=sys.executable)
    plugin_value = _resolve_driver_setting(driver, "plugin_path", env_name="CODESKILL_PLUGIN_PATH", default="openclaw_plugin")
    sidecar_value = _resolve_driver_setting(driver, "sidecar_script", env_name="CODESKILL_SIDECAR_SCRIPT", default="scripts/run_openclaw_r012_sidecar.py")
    manager_ledger = _resolve_driver_setting(driver, "manager_ledger", env_name="CODESKILL_MANAGER_LEDGER")
    manager_config = _resolve_driver_setting(driver, "manager_model_config", env_name="CODESKILL_MANAGER_CONFIG")
    return {
        "task_root": task_root,
        "harbor": harbor,
        "python": _resolve_executable_path(python_value, base=ROOT, field="python_executable") if python_value else Path(sys.executable),
        "plugin": _resolve_path(plugin_value, base=ROOT, field="plugin_path"),
        "sidecar": _resolve_path(sidecar_value, base=ROOT, field="sidecar_script"),
        "manager_ledger": _resolve_path(manager_ledger, base=ROOT, field="manager_ledger") if manager_ledger else None,
        "manager_config": _resolve_path(manager_config, base=ROOT, field="manager_model_config") if manager_config else None,
    }


def _load_input(path: Path) -> dict[str, Any]:
    value = _read_json_file(path, field="driver input")
    if value.get("kind") != "r015_c_only_trial_driver_input":
        raise COnlyHarborDriverError("driver input kind is not r015_c_only_trial_driver_input")
    if value.get("condition") != "C-only" or value.get("historical_baseline_used") is not False:
        raise COnlyHarborDriverError("driver input must be a current C-only assignment")
    assignment = _object(value.get("assignment"), field="input.assignment")
    task = _object(value.get("task"), field="input.task")
    for field in ("trial_id", "task_id", "round_id", "condition", "frozen_bank", "frozen_bank_state_sha256"):
        if field not in assignment:
            raise COnlyHarborDriverError(f"input.assignment.{field} is required")
    if assignment.get("condition") != "C-only" or assignment.get("task_id") != value.get("task", {}).get("canonical_instance_id"):
        raise COnlyHarborDriverError("input assignment/task identity is inconsistent")
    if assignment.get("trial_id") != f"r{assignment.get('round_id')}:C:{assignment.get('task_id')}":
        raise COnlyHarborDriverError("input assignment trial_id does not follow the frozen C-only identity")
    task_name = _text(task.get("task_name"), field="input.task.task_name")
    expected_task = task_name.removeprefix("terminal-bench/")
    if expected_task != assignment.get("task_id"):
        raise COnlyHarborDriverError("input task name and assignment task ID disagree")
    config = _object(value.get("config"), field="input.config")
    config_path_value = Path(_text(config.get("path"), field="input.config.path"))
    if not config_path_value.is_file():
        raise COnlyHarborDriverError("input.config.path does not identify the prepared immutable config")
    config_sha256 = _text(config.get("sha256"), field="input.config.sha256")
    if sha256_file(config_path_value) != config_sha256:
        raise COnlyHarborDriverError("input.config.sha256 does not match the prepared config bytes")
    prepared_config = _read_json_file(config_path_value, field="prepared C-only config")
    if prepared_config.get("kind") != "r015_c_only_two_round_protocol" or prepared_config.get("protocol_id") != value.get("protocol_id"):
        raise COnlyHarborDriverError("input.config bytes are bound to a different C-only protocol")
    for config_key in ("task_artifact_audit", "runtime_alignment", "retrieval_profile", "driver"):
        if canonical_json(config.get(config_key)) != canonical_json(prepared_config.get(config_key)):
            raise COnlyHarborDriverError(f"input.config.{config_key} differs from the prepared config bytes")
    task_audit_ref = config.get("task_artifact_audit")
    if task_audit_ref is not None:
        task_audit_ref = _object(task_audit_ref, field="input.config.task_artifact_audit")
        audit_path = Path(_text(task_audit_ref.get("path"), field="input.config.task_artifact_audit.path"))
        audit_hash = _text(task_audit_ref.get("sha256"), field="input.config.task_artifact_audit.sha256")
        if not audit_path.is_absolute():
            audit_path = ROOT / audit_path
        if not audit_path.is_file() or sha256_file(audit_path) != audit_hash:
            raise COnlyHarborDriverError("input.config.task_artifact_audit is not bound to immutable audit bytes")
    runtime = _object(config.get("runtime_alignment"), field="input.config.runtime_alignment")
    target = _object(runtime.get("prepared_target"), field="input.config.runtime_alignment.prepared_target")
    profile = _object(config.get("retrieval_profile"), field="input.config.retrieval_profile")
    try:
        validate_execution_profile(profile)
    except Exception as error:
        raise COnlyHarborDriverError(f"input retrieval profile is invalid: {error}") from error
    for field in ("model_id", "endpoint", "thinking", "reasoning_effort", "harbor_version", "openclaw_version"):
        _text(target.get(field), field=f"prepared_target.{field}")
    _positive_int(target.get("context_tokens"), field="prepared_target.context_tokens")
    _positive_int(target.get("max_output_tokens"), field="prepared_target.max_output_tokens")
    state = _object(value.get("state"), field="input.state")
    state_path = Path(_text(state.get("path"), field="input.state.path"))
    stated_state_hash = _text(state.get("sha256"), field="input.state.sha256")
    if not state_path.is_file():
        raise COnlyHarborDriverError("input state path does not identify the frozen current C-only state")
    current_state_hash = sha256_file(state_path)
    # Always bind the input to the state configuration and assignment.  The
    # state digest is expected to move after a durable trial/extraction phase,
    # but a caller must not be able to pair an otherwise valid input with a
    # different state file that happens to share the protocol ID.
    current_state = _read_json_file(state_path, field="current C-only state")
    state_config = _object(current_state.get("config"), field="current C-only state.config")
    if state_config.get("sha256") != config_sha256:
        raise COnlyHarborDriverError("input state is bound to a different C-only config")
    if current_state.get("protocol_id") != value.get("protocol_id"):
        raise COnlyHarborDriverError("input state is bound to a different C-only protocol")
    rounds = _object(current_state.get("rounds"), field="current C-only state.rounds")
    round_state = _object(rounds.get(str(assignment["round_id"])), field="current C-only state.round")
    live_assignment = _object(
        _object(round_state.get("assignments"), field="current C-only state.assignments").get(str(assignment["task_id"])),
        field="current C-only state.assignment",
    )
    if live_assignment.get("trial_id") != assignment.get("trial_id") or live_assignment.get("frozen_bank_state_sha256") != assignment.get("frozen_bank_state_sha256"):
        raise COnlyHarborDriverError("input state changed the frozen C-only assignment while resuming")
    if current_state.get("profile_sha256") != profile_sha256(profile):
        raise COnlyHarborDriverError("input state changed the frozen R012 retrieval profile while resuming")
    if current_state_hash != stated_state_hash:
        # Applying a durable driver phase changes the coordinator state file.
        # The immutable input remains valid only because the config, round,
        # task, trial, bank, and frozen profile above still match exactly.
        pass
    round_material = _object(value.get("round_material"), field="input.round_material")
    if (
        not isinstance(round_material.get("trajectory_pool", []), list)
        or not isinstance(round_material.get("description_pool", []), list)
        or not isinstance(round_material.get("task_candidate_pool", []), list)
    ):
        raise COnlyHarborDriverError("input round material pools must be lists")
    _reject_historical(value)
    frozen = SkillBank.from_dict(_object(assignment.get("frozen_bank"), field="assignment.frozen_bank"))
    if frozen.snapshot()["state_sha256"] != assignment.get("frozen_bank_state_sha256"):
        raise COnlyHarborDriverError("frozen assignment bank hash does not match its bank bytes")
    return value


def _task_root_and_service(value: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Path | str | None], Path]:
    assignment = _object(value.get("assignment"), field="input.assignment")
    task = _object(value.get("task"), field="input.task")
    paths = _driver_paths(value)
    task_root = paths["task_root"]
    if not isinstance(task_root, Path) or not task_root.is_dir():
        raise COnlyHarborDriverError(f"TB2.1 task root is not a directory: {task_root}")
    task_path = _task_path(task_root, str(assignment["task_id"]))
    metadata = _task_metadata(
        task_path,
        expected_task_name=str(task["task_name"]),
        expected_digest=task.get("task_digest"),
        expected_task_toml_sha256=task.get("public_task_toml_sha256"),
        expected_image=task.get("public_docker_image"),
        expected_image_id=task.get("public_docker_image_id"),
        expected_repo_digests=task.get("public_docker_repo_digests"),
    )
    return paths, metadata, task_path


def _runtime_values(value: dict[str, Any]) -> dict[str, Any]:
    config = _object(value["config"], field="input.config")
    runtime = _object(config["runtime_alignment"], field="runtime_alignment")
    target = _object(runtime["prepared_target"], field="prepared_target")
    profile = _object(config["retrieval_profile"], field="retrieval_profile")
    upstream = _object(runtime.get("baseline_observed"), field="baseline_observed")
    provider_timeout = _positive_int(profile.get("runtime", {}).get("upstream_timeout_seconds", 900), field="upstream_timeout_seconds") if isinstance(profile.get("runtime"), dict) else 900
    target_timeout = target.get("provider_timeout_seconds")
    if target_timeout is not None:
        target_timeout = _positive_int(target_timeout, field="prepared_target.provider_timeout_seconds")
        if target_timeout != provider_timeout:
            raise COnlyHarborDriverError(
                "prepared target provider_timeout_seconds differs from the frozen sidecar upstream timeout"
            )
    return {
        "target": target,
        "profile": profile,
        "upstream": upstream,
        "context_tokens": _positive_int(target.get("context_tokens"), field="prepared_target.context_tokens"),
        "max_output_tokens": _positive_int(target.get("max_output_tokens"), field="prepared_target.max_output_tokens"),
        "max_input_tokens": _positive_int(target.get("proxy_max_input_tokens", 250000), field="prepared_target.proxy_max_input_tokens"),
        "provider_timeout": provider_timeout,
    }


def _prepare_shared_permit_directory(directory: Path) -> None:
    """Create the host/container lifecycle exchange with a shared GID."""
    directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        # Records are written by the task-container user and consumed by the
        # host sidecar. Preserve the host directory GID for container-created
        # files without changing any benchmark workspace ownership.
        directory.chmod(0o2770)


def _build_trial_context(value: dict[str, Any], *, output_path: Path, paths: dict[str, Path | str | None], metadata: dict[str, Any], check_only: bool = False) -> dict[str, Any]:
    assignment = _object(value["assignment"], field="input.assignment")
    task = _object(value["task"], field="input.task")
    task_path = Path(_text(metadata.get("task_path"), field="task_metadata.task_path"))
    runtime = _runtime_values(value)
    profile = runtime["profile"]
    target = runtime["target"]
    trial_id = str(assignment["trial_id"])
    session_id = _session_id(trial_id)
    artifact_root = output_path.parent / "official-harbor"
    if artifact_root.exists() and not check_only:
        if (artifact_root / "launch-intent.json").is_file() or (artifact_root / "harbor-process.json").is_file():
            raise COnlyHarborDriverError(f"{trial_id}: official-harbor artifacts already exist; reconcile before rerunning")
        raise COnlyHarborDriverError(f"{trial_id}: nonempty official-harbor directory requires reconciliation")
    if not check_only:
        artifact_root.mkdir(parents=True, exist_ok=True)
    sidecar_dir = artifact_root / "sidecar"
    permit_dir = artifact_root / "native-summary-permits"
    state_root = artifact_root / "openclaw-state"
    for directory in (sidecar_dir, state_root):
        if not check_only:
            directory.mkdir(parents=True, exist_ok=True)
    if not check_only:
        _prepare_shared_permit_directory(permit_dir)
    database = state_root / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
    if not check_only:
        database.parent.mkdir(parents=True, exist_ok=True)
        database.touch(mode=0o600, exist_ok=False)
    base_port = 18500
    driver_config = _object(value["config"].get("driver", {}), field="input.config.driver")
    configured_port = driver_config.get("sidecar_port_base")
    if isinstance(configured_port, int) and configured_port > 0:
        base_port = configured_port
    port = base_port + (int(assignment["round_id"]) - 1) * 100 + int(task["order"])
    advertised_host = str(driver_config.get("sidecar_advertised_host") or os.environ.get("CODESKILL_SIDECAR_ADVERTISED_HOST", "172.17.0.1"))
    listen_host = str(driver_config.get("sidecar_listen_host") or os.environ.get("CODESKILL_SIDECAR_LISTEN_HOST", "0.0.0.0"))
    sidecar_url = f"http://{advertised_host}:{port}/v1"
    plugin_path = paths["plugin"]
    sidecar_script = paths["sidecar"]
    python_path = paths["python"]
    if not isinstance(plugin_path, Path) or not plugin_path.is_dir() or not (plugin_path / "openclaw.plugin.json").is_file():
        raise COnlyHarborDriverError(f"public CODESKILL plugin is unavailable: {plugin_path}")
    if not isinstance(sidecar_script, Path) or not sidecar_script.is_file():
        raise COnlyHarborDriverError(f"public sidecar script is unavailable: {sidecar_script}")
    if not isinstance(python_path, Path) or not python_path.is_file():
        raise COnlyHarborDriverError(f"Python runtime is unavailable: {python_path}")
    host_config = artifact_root / "openclaw-host.json"
    audit_path = permit_dir / "plugin-audit.jsonl"
    openclaw_config = _render_openclaw_config(
        model_id=str(target["model_id"]),
        sidecar_url=sidecar_url,
        plugin_path=plugin_path,
        permit_directory=permit_dir,
        trial_id=trial_id,
        session_id=session_id,
        audit_path=audit_path,
        context_tokens=int(target["context_tokens"]),
        max_output_tokens=int(target["max_output_tokens"]),
        thinking=str(target["thinking"]),
        reasoning_effort=str(target["reasoning_effort"]),
        temperature=float(target["temperature"]),
        top_p=float(target["top_p"]),
        provider_timeout_seconds=int(runtime["provider_timeout"]),
    )
    sidecar_config = {
        "schema_version": 1,
        "kind": "r015_openclaw_sidecar_binding",
        "trialId": trial_id,
        "sessionId": session_id,
        "sessionMarker": f"sqlite:main:{session_id}:{database.resolve()}",
        "permitDirectory": str(permit_dir.resolve()),
        "overlay": {
            "statePath": str((sidecar_dir / "overlay-state.json").resolve()),
            "evidenceDirectory": str(sidecar_dir.resolve()),
            "maxInputTokens": int(runtime["max_input_tokens"]),
        },
        "tokenizer": {"baseUrl": str(_text(runtime["upstream"].get("endpoint"), field="baseline_observed.endpoint")), "timeoutSeconds": 60},
        "upstream": {
            "endpoint": str(_text(runtime["upstream"].get("endpoint"), field="baseline_observed.endpoint")).rstrip("/") + "/chat/completions",
            "timeoutSeconds": int(runtime["provider_timeout"]),
        },
        "listen": {"host": listen_host, "advertisedHost": advertised_host, "port": port},
        "openclaw": {
            "configPath": str(host_config.resolve()),
            "pluginPath": str(plugin_path.resolve()),
            "providerId": "codeskill-r012",
            "modelId": str(target["model_id"]),
        },
        "selection": {"mode": "frozen-bank"},
        "retrieval": {
            "trialId": trial_id,
            "instanceId": str(assignment["task_id"]),
            "lifecycleStatePath": str(Path(value["state"]["path"]).resolve()),
            # The sidecar binds to the bytes it will actually read.  A
            # manager-only continuation never starts the sidecar and keeps
            # the original input-bound state pair in its trial evidence.
            # Using the current bytes here keeps any explicit sidecar check
            # strict after a coordinator save without introducing a digest
            # bypass.
            "lifecycleStateSha256": sha256_file(Path(value["state"]["path"]).resolve()),
            "profileSha256": profile_sha256(profile),
            "bankSnapshotSha256": str(assignment["frozen_bank_state_sha256"]),
            "encoder": {"kind": "minilm", "repoId": profile.get("runtime", {}).get("minilm_repo_id", "sentence-transformers/all-MiniLM-L6-v2"), "revision": profile.get("runtime", {}).get("minilm_revision")},
            **_profile_rules(profile),
        },
        # A null target is intentional: the historical baseline had a task
        # with 50 model calls, so a hidden sidecar cap would change the task.
        "maxOutputTokens": int(target["max_output_tokens"]),
    }
    harbor = paths["harbor"]
    if not isinstance(harbor, str) or not harbor:
        raise COnlyHarborDriverError("Harbor executable is not configured")
    agent_timeout_multiplier = target.get(
        "agent_timeout_multiplier",
        runtime["upstream"].get("agent_timeout_multiplier", 4.0),
    )
    setup_timeout_seconds = target.get("setup_timeout_seconds", 360)
    if isinstance(agent_timeout_multiplier, bool) or not isinstance(agent_timeout_multiplier, (int, float)) or agent_timeout_multiplier <= 0:
        raise COnlyHarborDriverError("prepared_target.agent_timeout_multiplier must be a positive number")
    if isinstance(setup_timeout_seconds, bool) or not isinstance(setup_timeout_seconds, (int, float)) or setup_timeout_seconds <= 0:
        raise COnlyHarborDriverError("prepared_target.setup_timeout_seconds must be a positive number")
    job_dir = artifact_root / "harbor"
    jobs_dir = job_dir / "jobs"
    job_config = {
        "schema_version": 1,
        "job_name": f"r015-c-only-r{assignment['round_id']}-{assignment['task_id']}-{sha256_text(trial_id)[:12]}",
        "jobs_dir": str(jobs_dir.resolve()),
        "n_attempts": 1,
        "n_concurrent_trials": 1,
        "timeout_multiplier": 1.0,
        "agent_timeout_multiplier": float(agent_timeout_multiplier),
        "verifier_timeout_multiplier": 1.0,
        "agent_setup_timeout_multiplier": 1.0,
        "environment_build_timeout_multiplier": 1.0,
        "quiet": False,
        "debug": True,
        "retry": {"max_retries": 0},
        "environment": {
            "type": "docker",
            "delete": True,
            "extra_allowed_hosts": [advertised_host],
            "env": {"OPENCLAW_STATE_DIR": "/var/lib/codeskill/openclaw-state"},
            "mounts": [
                {"type": "bind", "source": str(plugin_path.resolve()), "target": "/opt/codeskill/openclaw-sidecar-src", "read_only": True},
                {"type": "bind", "source": str(permit_dir.resolve()), "target": "/var/lib/codeskill/native-summary-permits"},
                {"type": "bind", "source": str(state_root.resolve()), "target": "/var/lib/codeskill/openclaw-state"},
            ],
        },
        # No outer timeout is added.  Harbor's task-derived agent/verifier
        # limits and the persisted multiplier remain authoritative.
        "agents": [
            {
                "import_path": "codeskill_rebuild.harbor_openclaw_adapter:CODESKILLHarborOpenClaw",
                "model_name": f"codeskill-r012/{target['model_id']}",
                "n_concurrent": 1,
                "override_setup_timeout_sec": float(setup_timeout_seconds),
                "extra_allowed_hosts": [advertised_host],
                "kwargs": {
                    "version": str(target.get("openclaw_version", "2026.9.3")),
                    "sidecar_base_url": sidecar_url,
                    "sidecar_model_id": str(target["model_id"]),
                    "plugin_path": "/opt/codeskill/openclaw-sidecar",
                    "permit_directory": "/var/lib/codeskill/native-summary-permits",
                    "plugin_audit_path": "/var/lib/codeskill/native-summary-permits/plugin-audit.jsonl",
                    "trial_id": trial_id,
                    "session_id": session_id,
                    "context_tokens": int(target["context_tokens"]),
                    "max_output_tokens": int(target["max_output_tokens"]),
                    "codeskill_thinking": str(target["thinking"]),
                    "codeskill_reasoning_effort": str(target["reasoning_effort"]),
                    "codeskill_temperature": float(target["temperature"]),
                    "codeskill_top_p": float(target["top_p"]),
                    "codeskill_provider_timeout_seconds": int(runtime["provider_timeout"]),
                    "session_to_trajectory": True,
                    "openclaw_config": {},
                },
                "env": {"CODESKILL_SIDECAR_KEY": "dev-local", "OPENCLAW_STATE_DIR": "/var/lib/codeskill/openclaw-state"},
            }
        ],
        "tasks": [{"path": str(task_path.resolve())}],
    }
    metadata["effective_runtime_limits"] = _effective_task_limits(
        metadata,
        job_value=job_config,
        agent_setup_timeout_seconds=float(setup_timeout_seconds),
    )
    metadata["runtime_binding"] = {
        "harbor_executable": str(harbor),
        "harbor_version_target": str(target.get("harbor_version", "")),
        "openclaw_package_target": str(target.get("openclaw_version", "")),
        "task_checkout_commit": metadata.get("task_checkout_commit"),
        "task_toml_sha256": metadata.get("task_toml", {}).get("sha256") if isinstance(metadata.get("task_toml"), dict) else None,
        "public_image": metadata.get("public_environment", {}).get("docker_image") if isinstance(metadata.get("public_environment"), dict) else None,
        "public_image_identity": metadata.get("public_environment", {}).get("docker_image_identity") if isinstance(metadata.get("public_environment"), dict) else None,
    }
    if not check_only:
        write_json(host_config, openclaw_config)
        write_json(artifact_root / "sidecar.json", sidecar_config)
        job_dir.mkdir(parents=True, exist_ok=True)
        write_json(job_dir / "job.json", job_config)
    return {
        "artifact_root": artifact_root,
        "trial_id": trial_id,
        "session_id": session_id,
        "task_id": str(assignment["task_id"]),
        "task_name": str(task["task_name"]),
        "task_path": task_path,
        "task_metadata": metadata,
        "state_path": Path(value["state"]["path"]),
        "state_sha256": str(value["state"]["sha256"]),
        "sidecar_dir": sidecar_dir,
        "permit_dir": permit_dir,
        "state_root": state_root,
        "database": database,
        "host_config": host_config,
        "sidecar_config": artifact_root / "sidecar.json",
        "job_config": job_dir / "job.json",
        "jobs_dir": jobs_dir,
        "sidecar_script": sidecar_script,
        "python": python_path,
        "harbor": harbor,
        "port": port,
        "listen_host": listen_host,
        "advertised_host": advertised_host,
        "sidecar_url": sidecar_url,
        "profile": profile,
        "upstream": runtime["upstream"],
        "driver_config": _object(value["config"].get("driver", {}), field="input.config.driver"),
        "target": target,
        "openclaw_config": openclaw_config,
        # Keep evidence metadata outside the uploaded OpenClaw document.
        # OpenClaw rejects unknown top-level configuration keys, so a
        # host-side audit marker must never be mistaken for an OpenClaw
        # setting.  The exact paths remain in the launch intent and config
        # refs below.
        "openclaw_binding_audit": {
            "host_plugin_path": str(plugin_path.resolve()),
            "host_permit_directory": str(permit_dir.resolve()),
            "host_audit_path": str(audit_path.resolve()),
            "rendered_through": "CODESKILLHarborOpenClaw._bind_public_plugin public API",
        },
        "sidecar_value": sidecar_config,
        "job_value": job_config,
    }


def _shell_environment() -> dict[str, str]:
    environment = os.environ.copy()
    source = str(SRC.resolve())
    old = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = source if not old else source + os.pathsep + old
    environment.setdefault("PYTHONUTF8", "1")
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    return environment


def _sidecar_check(context: dict[str, Any]) -> dict[str, Any]:
    command = [
        str(context["python"]),
        str(context["sidecar_script"]),
        "--config",
        str(context["sidecar_config"]),
        "--check-config",
    ]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=_shell_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
        result = {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "error_type": None,
            "error": None,
        }
    except subprocess.TimeoutExpired as error:
        result = {"returncode": None, "stdout": error.stdout or "", "stderr": error.stderr or "", "error_type": type(error).__name__, "error": str(error)}
    except OSError as error:
        result = {"returncode": None, "stdout": "", "stderr": "", "error_type": type(error).__name__, "error": str(error)}
    result.update({"kind": "r015_c_only_sidecar_config_check", "command": command, "elapsed_seconds": time.monotonic() - started})
    write_json(context["artifact_root"] / "sidecar-check.json", result)
    if result["returncode"] != 0:
        raise COnlyHarborDriverError(
            "public CODESKILL sidecar --check-config failed; see "
            + str(context["artifact_root"] / "sidecar-check.json")
        )
    return result


def _harbor_config_check(context: dict[str, Any]) -> dict[str, Any]:
    """Validate the generated job through Harbor's public ``JobConfig`` API.

    Constructing ``job.json`` is not enough to establish that Harbor will
    accept the agent, environment, and task bindings.  This check performs
    only schema/version validation; it does not build a task image, start a
    trial, or contact the model service.
    """
    harbor_python: str | None = None
    harbor_path = Path(str(context["harbor"]))
    if harbor_path.is_file():
        candidate = harbor_path.parent / ("python.exe" if os.name == "nt" else "python")
        if candidate.is_file():
            harbor_python = str(candidate)
        else:
            try:
                first_line = harbor_path.open("r", encoding="utf-8").readline().strip()
            except (OSError, UnicodeDecodeError):
                first_line = ""
            if first_line.startswith("#!") and Path(first_line[2:].strip()).is_file():
                harbor_python = first_line[2:].strip()
    harbor_python = harbor_python or str(context["python"])
    schema_probe = (
        "import json,sys; "
        "from harbor.models.job.config import JobConfig; "
        "JobConfig(**json.load(open(sys.argv[1], encoding='utf-8'))); "
        "print('valid')"
    )
    try:
        completed = subprocess.run(
            [harbor_python, "-c", schema_probe, str(context["job_config"])],
            cwd=ROOT,
            env=_shell_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        completed = None
        schema_error: dict[str, Any] = {"error_type": type(error).__name__, "error": str(error)}
    else:
        schema_error = {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    if completed is None or completed.returncode != 0 or (completed.stdout or "").strip() != "valid":
        result = {
            "kind": "r015_c_only_harbor_job_config_check",
            "status": "invalid",
            "job_config_schema": "harbor.models.job.config.JobConfig",
            "harbor_python": harbor_python,
            "error": schema_error,
        }
        write_json(context["artifact_root"] / "job-config-check.json", result)
        raise COnlyHarborDriverError(
            "the configured Harbor runtime rejected the generated JobConfig; see "
            + str(context["artifact_root"] / "job-config-check.json")
        )
    harbor_version = None
    version_error: dict[str, Any] | None = None
    try:
        completed = subprocess.run(
            [str(context["harbor"]), "--version"],
            cwd=ROOT,
            env=_shell_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        harbor_version = (completed.stdout or completed.stderr).strip()
        if completed.returncode != 0:
            version_error = {
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
    except (OSError, subprocess.TimeoutExpired) as error:
        version_error = {"error_type": type(error).__name__, "error": str(error)}
    if version_error is not None:
        result = {
            "kind": "r015_c_only_harbor_job_config_check",
            "status": "invalid",
            "job_config_schema": "harbor.models.job.config.JobConfig",
            "harbor_version": harbor_version,
            "error": version_error,
        }
        write_json(context["artifact_root"] / "job-config-check.json", result)
        raise COnlyHarborDriverError(
            "the configured Harbor executable failed its read-only version check; see "
            + str(context["artifact_root"] / "job-config-check.json")
        )
    result = {
        "kind": "r015_c_only_harbor_job_config_check",
        "status": "valid",
        "job_config_schema": "harbor.models.job.config.JobConfig",
        "harbor_python": harbor_python,
        "harbor_version": harbor_version,
        "job_config": _ref(context["job_config"]),
        "task_path": str(context["task_metadata"].get("task_path")),
        "n_concurrent_trials": context["job_value"].get("n_concurrent_trials"),
        "agent_timeout_multiplier": context["job_value"].get("agent_timeout_multiplier"),
        "official_trial_started": False,
    }
    write_json(context["artifact_root"] / "job-config-check.json", result)
    return result


def _read_attempts(
    sidecar_dir: Path,
    *,
    trial_id: str,
    session_id: str,
    state_path: Path,
) -> list[dict[str, Any]]:
    """Load only proxy attempts bound to this config/session.

    ``trial_id`` alone is insufficient: the public provider wrapper writes a
    normal-call boundary carrying the session identity, and a sidecar state
    path can otherwise be copied from a different run.  Every boundary is
    therefore checked against the frozen assignment before manager decisions
    can consume the attempt.  A public overlay can reject a request before
    the provider wrapper runs; those records have no boundary, but only the
    explicitly validated preflight schemas from ``non_forwarded_terminal_disposition``
    are admitted.  An arbitrary missing boundary remains a hard importer
    failure.
    """
    attempts_dir = sidecar_dir / "upstream_requests"
    if not attempts_dir.is_dir():
        return []
    attempts: list[dict[str, Any]] = []
    for path in sorted(attempts_dir.glob("attempt-*.json")):
        value = _read_json_file(path, field="sidecar upstream request")
        if value.get("trial_id") != trial_id:
            raise COnlyHarborDriverError(f"sidecar evidence belongs to a different trial: {path}")
        boundary = value.get("normal_call_boundary")
        if not isinstance(boundary, dict):
            disposition = non_forwarded_terminal_disposition(value)
            if disposition is None:
                raise COnlyHarborDriverError(
                    f"sidecar evidence has no validated public boundary or non-forwarded terminal disposition: {path}"
                )
            value["_non_forwarded_terminal"] = disposition
        else:
            if non_forwarded_terminal_schema(value) is not None:
                raise COnlyHarborDriverError(
                    f"sidecar evidence mixes a public normal_call_boundary with terminal rejection evidence: {path}"
                )
            if boundary.get("trial_id") != trial_id or boundary.get("session_id") != session_id:
                raise COnlyHarborDriverError(f"sidecar normal_call_boundary belongs to a different assignment: {path}")
        recorded_state = value.get("state_path")
        if recorded_state != str(state_path.resolve()):
            raise COnlyHarborDriverError(f"sidecar evidence state path differs from the frozen assignment: {path}")
        value["_evidence_path"] = str(path)
        value["_evidence_sha256"] = sha256_file(path)
        attempts.append(value)
    return attempts


def _find_harbor_trial(jobs_dir: Path, expected_task_name: str) -> Path | None:
    leaf = expected_task_name.rsplit("/", 1)[-1]
    matches: list[Path] = []
    for result_path in sorted(jobs_dir.rglob("result.json")) if jobs_dir.is_dir() else []:
        trial_dir = result_path.parent
        config_path = trial_dir / "config.json"
        if not config_path.is_file():
            continue
        try:
            config = read_json(config_path)
        except (OSError, ValueError):
            continue
        task = config.get("task") if isinstance(config, dict) else None
        if not isinstance(task, dict):
            continue
        task_name = task.get("name")
        task_path = task.get("path")
        path_leaf = Path(task_path).name if isinstance(task_path, str) else None
        if task_name == expected_task_name or path_leaf == leaf:
            matches.append(trial_dir)
    if len(matches) > 1:
        raise COnlyHarborDriverError(f"Harbor produced multiple matching trial directories under {jobs_dir}")
    return matches[0] if matches else None


def _harbor_task_record(value: dict[str, Any]) -> dict[str, Any]:
    """Extract Harbor's public task identity from result/config JSON.

    Harbor 0.17.x uses several public envelopes for the same identity:
    ``TrialResult.task_id`` is a ``LocalTaskId``/``PackageTaskId`` object,
    while a serialized trial config puts the corresponding ``TaskConfig`` in
    ``task``.  Keep the extraction deliberately shallow so an arbitrary
    nested object cannot be treated as the official task anchor.
    """
    for key in ("task", "task_id"):
        candidate = value.get(key)
        if isinstance(candidate, dict):
            return candidate
    for key in ("config", "trial_config"):
        nested = value.get(key)
        if isinstance(nested, dict):
            for task_key in ("task", "task_id"):
                candidate = nested.get(task_key)
                if isinstance(candidate, dict):
                    return candidate
    tasks = value.get("tasks")
    if isinstance(tasks, list) and tasks and isinstance(tasks[0], dict):
        return tasks[0]
    return {}


def _harbor_task_path(value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).expanduser().resolve()


def _frozen_launch_task_identity(context: dict[str, Any]) -> tuple[str, Path | None, str | None, str | None]:
    """Return the identity mode and pinned public launch fields.

    A local task launch is identified by its exact resolved path and public
    task artifacts.  A package launch is identified by ``name`` plus its
    resolved dataset ref.  The dataset digest is not interchangeable with a
    local task's ``task.toml`` or Harbor's ``task_checksum``.
    """
    metadata = context.get("task_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    task_path = _harbor_task_path(metadata.get("task_path"))
    job_value = context.get("job_value")
    if isinstance(job_value, dict):
        tasks = job_value.get("tasks")
        launch = tasks[0] if isinstance(tasks, list) and tasks and isinstance(tasks[0], dict) else None
        if isinstance(launch, dict):
            configured_path = _harbor_task_path(launch.get("path"))
            if configured_path is not None:
                return "local", configured_path, None, None
            configured_name = launch.get("name")
            if isinstance(configured_name, str) and configured_name.strip():
                return "package", None, configured_name.strip(), launch.get("ref")
    if task_path is not None:
        return "local", task_path, None, None
    expected_digest = metadata.get("expected_dataset_task_digest")
    return "package", None, str(context.get("task_name") or ""), expected_digest if isinstance(expected_digest, str) else None


def _validate_local_harbor_task_record(
    record: dict[str, Any],
    *,
    label: str,
    expected_name: str,
    expected_path: Path,
) -> None:
    """Validate a Harbor ``TaskConfig``/``LocalTaskId`` without a package ref."""
    actual_path = _harbor_task_path(record.get("path"))
    if actual_path is None or actual_path != expected_path:
        raise COnlyHarborDriverError(
            f"official Harbor {label} local task path differs from the frozen assignment: "
            f"expected {expected_path}, got {record.get('path')}"
        )
    name = record.get("name") or record.get("task_name")
    organization = record.get("org") or record.get("organization")
    if isinstance(name, str) and isinstance(organization, str) and organization.strip() and "/" not in name:
        name = f"{organization.strip()}/{name.strip()}"
    if name is not None and name not in {expected_name, expected_path.name}:
        raise COnlyHarborDriverError(
            f"official Harbor {label} local task name differs from the frozen assignment"
        )
    # Harbor's public TaskConfig validator rejects ``ref`` for a local path.
    # Treat a non-null ref as evidence of a relabelled package task instead of
    # silently accepting the historical package digest.
    if record.get("ref") not in {None, ""}:
        raise COnlyHarborDriverError(
            f"official Harbor {label} local TaskConfig unexpectedly carries a package ref"
        )


def _structured_verifier_reward(result: dict[str, Any]) -> Any | None:
    """Return an explicitly serialized verifier reward, when present.

    Harbor normally writes ``verifier/reward.txt``.  Some public result
    serializers also retain the verifier reward in ``result.json`` while the
    text artifact is absent (for example after a late artifact-copy failure).
    That record is not a setup failure merely because ``exception_info`` is
    present.  Keep this helper deliberately shallow and typed: a nested
    arbitrary value must never open the success importer or bypass its own
    required-artifact checks.
    """

    verifier = result.get("verifier_result")
    if not isinstance(verifier, dict):
        return None
    rewards = verifier.get("rewards")
    if isinstance(rewards, dict):
        reward = rewards.get("reward")
        if reward is not None and not isinstance(reward, (dict, list)):
            return reward
    reward = verifier.get("reward")
    if reward is not None and not isinstance(reward, (dict, list)):
        return reward
    return None


# Harbor emits a structured result for a small set of failures that happen
# before an agent session can start.  Keep this allow-list deliberately narrow:
# an arbitrary exception type (in particular ``NonZeroAgentExitCodeError``)
# does not establish that the task was infrastructure-only.  A complete
# current-round session with a verifier reward is handled by the normal
# importer above, while every other exception must prove this pre-agent shape
# before it can advance as an infra result.
_PRE_AGENT_SETUP_EXCEPTION_TYPES = frozenset(
    {
        "AgentSetupError",
        "AgentSetupFailure",
        "AgentSetupTimeoutError",
        "TaskSetupError",
        "TaskSetupFailure",
        "TaskSetupTimeoutError",
        "EnvironmentSetupError",
        "EnvironmentSetupFailure",
        "EnvironmentBuildError",
        "DockerBuildError",
        "DockerImagePullError",
    }
)


def _is_pre_agent_setup_failure(
    *,
    exception_type: str,
    result: dict[str, Any],
    context: dict[str, Any],
) -> bool:
    """Return whether an official result proves a pre-agent setup failure.

    The Harbor exception name is only one part of the boundary.  Both result
    envelopes must be empty and the public sidecar must contain no attempt;
    otherwise a task-level agent/tool failure is being reclassified as
    infrastructure merely because it has no reward file.  This check is
    intentionally conservative and does not inspect or alter a raw session.
    """

    if exception_type not in _PRE_AGENT_SETUP_EXCEPTION_TYPES:
        return False
    if result.get("agent_result") not in (None, {}):
        return False
    if result.get("verifier_result") not in (None, {}):
        return False
    sidecar_dir = context.get("sidecar_dir")
    if sidecar_dir is None:
        return True
    attempts_dir = Path(sidecar_dir) / "upstream_requests"
    if not attempts_dir.is_dir():
        return True
    return not any(attempts_dir.glob("attempt-*.json"))


def _official_failure_packet(
    *,
    context: dict[str, Any],
    process: dict[str, Any],
    trial_dir: Path,
) -> dict[str, Any] | None:
    """Classify a genuine Harbor result with no complete agent trial.

    Harbor writes a structured ``result.json`` for setup/build/verifier
    failures.  Such a result is a valid task-level infrastructure outcome,
    but it cannot be passed through the success-only trajectory importer.
    Missing or malformed result artifacts remain driver failures and stop the
    ordered campaign for reconciliation.
    """
    result_path = trial_dir / "result.json"
    config_path = trial_dir / "config.json"
    if not result_path.is_file():
        return None
    result = _read_json_file(result_path, field="official Harbor result")
    # A structured failure is useful only when Harbor's own trial config is
    # present as a second identity anchor.  Without it, a result copied into
    # the jobs directory could be relabelled to this assignment.
    if not config_path.is_file():
        raise COnlyHarborDriverError("official Harbor failure result has no matching config.json")
    config = _read_json_file(config_path, field="official Harbor trial config")
    expected_task_name = str(context["task_name"])
    # Harbor 0.17.x serializes the public task identity as ``task_id`` in
    # result.json, while older result writers and our contract fixtures use
    # ``task``.  Treat both spellings as the same official identity anchor;
    # accepting only ``task_name`` would silently lose the dataset digest
    # check for real setup failures.
    task_record = _harbor_task_record(result)
    config_task = _harbor_task_record(config)
    def public_task_name(record: dict[str, Any], fallback: Any = None) -> Any:
        """Normalize Harbor's ``task_id.org/name`` into the public task name."""
        name = record.get("name") or record.get("task_name")
        organization = record.get("org") or record.get("organization")
        if isinstance(name, str) and isinstance(organization, str) and organization.strip() and "/" not in name:
            return f"{organization.strip()}/{name.strip()}"
        return name if name is not None else fallback

    result_task_name = public_task_name(task_record, result.get("task_name"))
    config_task_name = public_task_name(config_task, config.get("task_name"))
    launch_mode, expected_local_path, configured_package_name, configured_package_ref = _frozen_launch_task_identity(context)
    # A local path launch uses Harbor's LocalTaskId/TaskConfig as its binding;
    # those public records intentionally have name=None and ref=None.  The
    # historical dataset ref is therefore checked only for package launches.
    if launch_mode == "local":
        if expected_local_path is None:
            raise COnlyHarborDriverError("frozen local Harbor task has no resolved launch path")
        if result_task_name not in {None, expected_task_name, expected_local_path.name}:
            raise COnlyHarborDriverError("official Harbor failure result task identity differs from the frozen assignment")
        if config_task_name not in {None, expected_task_name, expected_local_path.name}:
            raise COnlyHarborDriverError("official Harbor failure config task identity differs from the frozen assignment")
        _validate_local_harbor_task_record(
            task_record,
            label="result",
            expected_name=expected_task_name,
            expected_path=expected_local_path,
        )
        _validate_local_harbor_task_record(
            config_task,
            label="config",
            expected_name=expected_task_name,
            expected_path=expected_local_path,
        )
    elif result_task_name != expected_task_name or (config_task_name is not None and config_task_name != expected_task_name):
        # A result without task identity is ambiguous even if exception_info
        # is present; do not let a random Harbor JSON advance this assignment.
        raise COnlyHarborDriverError("official Harbor failure result task identity differs from the frozen assignment")
    expected_digest = context.get("task_metadata", {}).get("expected_dataset_task_digest") if isinstance(context.get("task_metadata"), dict) else None
    result_digest = task_record.get("ref")
    config_digest = config_task.get("ref")
    if launch_mode == "package":
        if expected_digest is None:
            expected_digest = configured_package_ref
        if expected_digest is None or result_digest != expected_digest:
            raise COnlyHarborDriverError("official Harbor failure result task digest is missing or differs from the frozen assignment")
        if config_digest != expected_digest:
            raise COnlyHarborDriverError("official Harbor failure config task digest is missing or differs from the frozen assignment")
    trial_name = result.get("trial_name")
    config_trial_name = config.get("trial_name") if isinstance(config, dict) else None
    if trial_name not in {None, trial_dir.name} or config_trial_name not in {None, trial_dir.name}:
        raise COnlyHarborDriverError("official Harbor failure trial name differs from its result directory")
    config_agent = config.get("agent") if isinstance(config.get("agent"), dict) else {}
    config_kwargs = config_agent.get("kwargs") if isinstance(config_agent.get("kwargs"), dict) else {}
    config_session_ids = [
        value
        for value in (
            config.get("session_id"),
            config.get("sessionId"),
            config_agent.get("session_id"),
            config_agent.get("sessionId"),
            config_kwargs.get("session_id"),
            config_kwargs.get("sessionId"),
        )
        if isinstance(value, str) and value.strip()
    ]
    if config_session_ids and str(context["session_id"]) not in config_session_ids:
        raise COnlyHarborDriverError("official Harbor failure config session identity differs from the frozen assignment")
    config_trial_ids = [
        value
        for value in (
            config.get("trial_id"),
            config.get("trialId"),
            config_agent.get("trial_id"),
            config_agent.get("trialId"),
            config_kwargs.get("trial_id"),
            config_kwargs.get("trialId"),
        )
        if isinstance(value, str) and value.strip()
    ]
    if config_trial_ids and str(context["trial_id"]) not in config_trial_ids:
        raise COnlyHarborDriverError("official Harbor failure config trial identity differs from the frozen assignment")
    # A verifier reward is the normal success boundary.  Some Harbor result
    # writers retain auxiliary exception-shaped fields even for a completed
    # trial; do not route that valid result through the setup-failure packet.
    reward_path = trial_dir / "verifier" / "reward.txt"
    if reward_path.is_file() or _structured_verifier_reward(result) is not None:
        return None
    exception_info = result.get("exception_info")
    if not isinstance(exception_info, dict):
        exception_info = result.get("exception") if isinstance(result.get("exception"), dict) else None
    if not isinstance(exception_info, dict):
        return None
    exception_type = exception_info.get("exception_type") or exception_info.get("type")
    exception_message = exception_info.get("exception_message") or exception_info.get("message")
    if not isinstance(exception_type, str) or not exception_type.strip() or not isinstance(exception_message, str) or not exception_message.strip():
        raise COnlyHarborDriverError("official Harbor failure result has an incomplete exception_info record")
    exception_type = exception_type.strip()
    exception_message = exception_message.strip()
    agent_result = result.get("agent_result")
    verifier_result = result.get("verifier_result")
    if not _is_pre_agent_setup_failure(
        exception_type=exception_type,
        result=result,
        context=context,
    ):
        raise COnlyHarborDriverError(
            "official Harbor result has an exception without a proven pre-agent setup boundary; "
            f"preserve the raw result for reconciliation ({exception_type})"
        )
    # This branch is reserved for results that explicitly have no usable
    # solver or verifier reward/trajectory.
    raw_relative = (
        Path("result.json"),
        Path("config.json"),
        Path("lock.json"),
        Path("exception.txt"),
        Path("trial.log"),
        Path("artifacts") / "manifest.json",
    )
    raw_artifacts: dict[str, Any] = {}
    for relative in raw_relative:
        path = trial_dir / relative
        if path.is_file():
            raw_artifacts[relative.as_posix()] = _ref(path)
    failure_attempt_refs: list[dict[str, Any]] = []
    attempts_dir = context["sidecar_dir"] / "upstream_requests"
    if attempts_dir.is_dir():
        for attempt_path in sorted(attempts_dir.glob("attempt-*.json")):
            attempt = _read_json_file(attempt_path, field="official failure sidecar attempt")
            if attempt.get("trial_id") != str(context["trial_id"]):
                raise COnlyHarborDriverError("official failure sidecar attempt belongs to a different trial")
            boundary = attempt.get("normal_call_boundary")
            if boundary is not None:
                if non_forwarded_terminal_schema(attempt) is not None:
                    raise COnlyHarborDriverError(
                        "official failure sidecar attempt mixes a public normal_call_boundary with terminal rejection evidence"
                    )
                boundary_value = _object(boundary, field="official failure normal_call_boundary")
                if boundary_value.get("trial_id") != str(context["trial_id"]) or boundary_value.get("session_id") != str(context["session_id"]):
                    raise COnlyHarborDriverError("official failure sidecar boundary belongs to a different assignment")
            else:
                if non_forwarded_terminal_disposition(attempt) is None:
                    raise COnlyHarborDriverError(
                        "official failure sidecar attempt has no validated public boundary or terminal disposition"
                    )
            failure_attempt_refs.append({"path": str(attempt_path), "sha256": sha256_file(attempt_path)})
    raw_evidence = {
        "official_harbor_trial": True,
        "evidence_mode": "official_live",
        "official_trial_boundary_started": process.get("official_trial_boundary_started") is True,
        "condition": "C-only",
        "round_id": int(str(context["trial_id"]).split(":", 1)[0].removeprefix("r")),
        "task_id": str(context["task_id"]),
        "trial_id": str(context["trial_id"]),
        "session_id": str(context["session_id"]),
        "historical_baseline_used": False,
        "baseline_imported": False,
        "classification": "infra_failure",
        "session_observed": False,
        "session_binding": {
            "status": "expected_assignment_only",
            "configured_session_id": str(context["session_id"]),
            "raw_normal_call_boundary": None,
            "rule": "no session is claimed when official setup failed before agent launch",
        },
        "process": _process_evidence(process, context),
        "trial_dir": str(trial_dir),
        "raw_artifacts": raw_artifacts,
        "official_failure": {
            "result_id": result.get("id"),
            "exception_type": exception_type.strip(),
            "message": exception_message.strip(),
            "result_trial_uri": result.get("trial_uri"),
            "result_task_identity": deepcopy(task_record),
            "result_path": _ref(result_path),
            "config_path": _ref(config_path) if config_path.is_file() else None,
            "task_name": result_task_name,
            "task_ref": result_digest,
            "task_checksum": result.get("task_checksum"),
            "config_task_ref": config_digest,
            "launch_identity": {
                "mode": launch_mode,
                "local_task_path": str(expected_local_path) if expected_local_path is not None else None,
                "configured_package_name": configured_package_name,
                "configured_package_ref": configured_package_ref,
                "dataset_ref_checked": launch_mode == "package",
                "local_public_artifacts_checked": launch_mode == "local",
            },
            "trial_name": trial_name,
            "agent_result": deepcopy(agent_result),
            "verifier_result": deepcopy(verifier_result),
            "config_identity": {
                "session_ids": config_session_ids,
                "trial_ids": config_trial_ids,
                "session_match": (str(context["session_id"]) in config_session_ids) if config_session_ids else "not_recorded_by_harbor",
                "trial_match": (str(context["trial_id"]) in config_trial_ids) if config_trial_ids else "not_recorded_by_harbor",
            },
        },
        "sidecar_attempt_count": len(failure_attempt_refs),
        "sidecar_attempt_refs": failure_attempt_refs,
    }
    return {
        "outcome": "infra_failure",
        "trajectory": None,
        "raw_evidence": raw_evidence,
        "proxy_attempt_records": [],
        "packet_manifest": None,
        # The packet is written as JSON immediately by the importer; keep
        # paths serializable at this boundary rather than leaking a Path
        # object into the durable official-infra result.
        "trial_dir": str(trial_dir),
        "trace": None,
        "official_failure": deepcopy(raw_evidence["official_failure"]),
    }


def _result_exception_info(result: dict[str, Any]) -> dict[str, str] | None:
    """Return a validated Harbor execution exception, if one was recorded.

    Harbor 0.17.x keeps ``exception_info`` at the top level of the trial
    result, while the normalized trace intentionally retains only its small
    outcome summary.  A verifier reward therefore does not prove that the
    agent completed successfully: a task may have a reward file and still
    terminate with ``NonZeroAgentExitCodeError``.  Keep this check narrow so a
    null field in a successful result remains harmless and malformed failure
    metadata fails closed before any manager call can be made.
    """

    for key in ("exception_info", "trial_exception", "exception"):
        if key not in result or result.get(key) is None:
            continue
        value = result.get(key)
        if not isinstance(value, dict):
            raise COnlyHarborDriverError(
                f"official Harbor result {key} must be an object or null"
            )
        exception_type = value.get("exception_type") or value.get("type")
        message = value.get("exception_message") or value.get("message")
        if (
            not isinstance(exception_type, str)
            or not exception_type.strip()
            or not isinstance(message, str)
            or not message.strip()
        ):
            raise COnlyHarborDriverError(
                f"official Harbor result {key} has an incomplete exception record"
            )
        return {
            "field": key,
            "exception_type": exception_type.strip(),
            "message": message.strip(),
        }
    return None


def _run_official_harbor(context: dict[str, Any]) -> dict[str, Any]:
    artifact_root = context["artifact_root"]
    sidecar_stdout = artifact_root / "sidecar.stdout.log"
    sidecar_stderr = artifact_root / "sidecar.stderr.log"
    harbor_stdout = artifact_root / "harbor.stdout.log"
    harbor_stderr = artifact_root / "harbor.stderr.log"
    sidecar_command = [str(context["python"]), str(context["sidecar_script"]), "--config", str(context["sidecar_config"])]
    sidecar: subprocess.Popen[Any] | None = None
    sidecar_spawn_error: BaseException | None = None
    sidecar_ready = False
    harbor_returncode: int | None = None
    harbor_spawn_error: BaseException | None = None
    started = time.monotonic()
    with sidecar_stdout.open("w", encoding="utf-8") as out, sidecar_stderr.open("w", encoding="utf-8") as err:
        try:
            sidecar = subprocess.Popen(
                sidecar_command,
                cwd=ROOT,
                env=_shell_environment(),
                stdout=out,
                stderr=err,
                text=True,
                **_process_group_kwargs(),
            )
        except OSError as error:
            sidecar_spawn_error = error
    if sidecar is not None:
        sidecar_ready = _wait_listener(sidecar, "127.0.0.1", int(context["port"]))
    if sidecar is None or not sidecar_ready:
        _stop_process(sidecar)
        process = {
            "kind": "r015_c_only_official_process",
            "status": "driver_failure",
            "classification": "sidecar_spawn_failed" if sidecar is None else "sidecar_startup_failed",
            "sidecar_command": sidecar_command,
            "sidecar_ready": sidecar_ready,
            "sidecar_spawn_error_type": type(sidecar_spawn_error).__name__ if sidecar_spawn_error else None,
            "sidecar_spawn_error": str(sidecar_spawn_error) if sidecar_spawn_error else None,
            "sidecar_returncode": sidecar.returncode if sidecar is not None else None,
            "sidecar_stdout": _ref(sidecar_stdout),
            "sidecar_stderr": _ref(sidecar_stderr),
            "elapsed_seconds": time.monotonic() - started,
        }
        write_json(artifact_root / "harbor-process.json", process)
        raise COnlyHarborDriverError("sidecar did not start; no official Harbor trial was launched")
    harbor_command = [str(context["harbor"]), "job", "start", "--config", str(context["job_config"]), "--yes"]
    try:
        with harbor_stdout.open("w", encoding="utf-8") as out, harbor_stderr.open("w", encoding="utf-8") as err:
            harbor = subprocess.Popen(
                harbor_command,
                cwd=ROOT,
                env=_shell_environment(),
                stdout=out,
                stderr=err,
                text=True,
                **_process_group_kwargs(),
            )
            # There is intentionally no synthetic outer timeout.  Harbor's
            # task-derived agent/verifier limits are the authoritative bound;
            # the outer coordinator may terminate this shared process group if
            # an operator explicitly supplies a target timeout in a future
            # audited profile.
            harbor_returncode = harbor.wait()
    except OSError as error:
        harbor_spawn_error = error
    finally:
        _stop_process(sidecar)
    classification = (
        "harbor_spawn_failed"
        if harbor_spawn_error is not None
        else "harbor_nonzero"
        if harbor_returncode not in {0, None}
        else "completed"
    )
    process = {
        "kind": "r015_c_only_official_process",
        "status": "official_trial_boundary_completed" if harbor_spawn_error is None else "driver_failure",
        "classification": classification,
        "official_trial_boundary_started": harbor_spawn_error is None,
        "sidecar_command": sidecar_command,
        "sidecar_ready": sidecar_ready,
        "sidecar_returncode": sidecar.returncode if sidecar is not None else None,
        "sidecar_stdout": _ref(sidecar_stdout),
        "sidecar_stderr": _ref(sidecar_stderr),
        "harbor_command": harbor_command,
        "harbor_returncode": harbor_returncode,
        "harbor_spawn_error_type": type(harbor_spawn_error).__name__ if harbor_spawn_error else None,
        "harbor_spawn_error": str(harbor_spawn_error) if harbor_spawn_error else None,
        "harbor_stdout": _ref(harbor_stdout),
        "harbor_stderr": _ref(harbor_stderr),
        "elapsed_seconds": time.monotonic() - started,
    }
    write_json(artifact_root / "harbor-process.json", process)
    if harbor_spawn_error is not None:
        raise COnlyHarborDriverError("official Harbor could not be launched") from harbor_spawn_error
    return process


def _process_evidence(process: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    artifact_root = context["artifact_root"]
    evidence = deepcopy(process)
    for key in ("sidecar_stdout", "sidecar_stderr", "harbor_stdout", "harbor_stderr"):
        value = evidence.get(key)
        if isinstance(value, dict) and value.get("path"):
            evidence[key] = value
    evidence["task_metadata"] = deepcopy(context["task_metadata"])
    evidence["task_config"] = _ref(context["job_config"])
    evidence["sidecar_config"] = _ref(context["sidecar_config"])
    evidence["openclaw_host_config"] = _ref(context["host_config"])
    evidence["native_database_seed"] = _ref(context["database"])
    evidence["artifact_root"] = str(artifact_root)
    return evidence


def _import_official_evidence(context: dict[str, Any], process: dict[str, Any]) -> dict[str, Any]:
    trial_id = str(context["trial_id"])
    task_id = str(context["task_id"])
    try:
        # A recovery manifest binds one exact completed Harbor trial
        # directory.  Normal launches still discover the only matching result
        # under their private jobs directory; recovery must never silently
        # choose another result with the same task name.
        configured_trial_dir = context.get("harbor_trial_dir")
        trial_dir = (
            Path(str(configured_trial_dir)).resolve()
            if configured_trial_dir is not None
            else _find_harbor_trial(context["jobs_dir"], str(context["task_name"]))
        )
        if trial_dir is not None and not trial_dir.is_dir():
            raise COnlyHarborDriverError(f"configured Harbor trial directory is not a directory: {trial_dir}")
        if trial_dir is not None:
            # Setup/build failures have a valid official result schema but no
            # session, reward, or normal-call boundary.  Route them through
            # the dedicated infra packet before the success-only importer.
            official_failure = _official_failure_packet(
                context=context,
                process=process,
                trial_dir=trial_dir,
            )
            if official_failure is not None:
                write_json(context["artifact_root"] / "official-infra-result.json", official_failure)
                return official_failure
        attempts = _read_attempts(
            context["sidecar_dir"],
            trial_id=trial_id,
            session_id=str(context["session_id"]),
            state_path=context["sidecar_dir"] / "overlay-state.json",
        )
    except (COnlyHarborDriverError, OSError, ValueError) as error:
        # A malformed/mismatched evidence directory is a driver/importer
        # failure, even though Harbor was started.  Persist the exact process
        # context before stopping so a later resume cannot mistake it for an
        # official task-level infrastructure result.
        failure = {
            "kind": "r015_c_only_official_import_failure",
            "official_harbor_trial": True,
            "trial_id": trial_id,
            "task_id": task_id,
            "process": _process_evidence(process, context),
            "error_type": type(error).__name__,
            "error": str(error),
        }
        write_json(context["artifact_root"] / "import-failure.json", failure)
        raise COnlyHarborDriverError("official Harbor evidence discovery failed; current task requires reconciliation") from error
    raw_process = _process_evidence(process, context)
    raw_evidence: dict[str, Any] = {
        "official_harbor_trial": True,
        "evidence_mode": "official_live",
        "official_trial_boundary_started": process.get("official_trial_boundary_started") is True,
        "condition": "C-only",
        "round_id": int(str(trial_id).split(":", 1)[0].removeprefix("r")),
        "task_id": task_id,
        "trial_id": trial_id,
        "session_id": str(context["session_id"]),
        "historical_baseline_used": False,
        "baseline_imported": False,
        "process": raw_process,
        "trial_dir": str(trial_dir) if trial_dir else None,
        "sidecar_attempt_count": len(attempts),
        "sidecar_attempt_refs": [
            {"path": str(item["_evidence_path"]), "sha256": item["_evidence_sha256"]}
            for item in attempts
        ],
        # A preflight rejection is a real overlay observation, but it is not
        # a provider request and must never be counted as one merely because
        # its ordinal was allocated before the rejection.  Keep the bounded
        # decision facts beside the immutable raw attempt reference so the
        # task6 input-budget case remains explicit in the recovered evidence.
        "preflight_terminal_observations": [
            {
                "path": str(item["_evidence_path"]),
                "sha256": item["_evidence_sha256"],
                "kind": item["_non_forwarded_terminal"].get("kind"),
                "outcome": item["_non_forwarded_terminal"].get("outcome"),
                "error_type": item["_non_forwarded_terminal"].get("error_type"),
                "reason": item["_non_forwarded_terminal"].get("reason"),
                "exact_forwarded_input_tokens": item.get("exact_forwarded_input_tokens"),
                "max_input_tokens": item.get("max_input_tokens"),
                "provider_boundary": False,
                "trajectory_eligibility_effect": "retain_as_non_forwarded_observation; do_not_count_as_provider_call",
            }
            for item in attempts
            if isinstance(item.get("_non_forwarded_terminal"), dict)
        ],
        "raw_artifacts": {},
    }
    if trial_dir is not None:
        for relative in (
            Path("result.json"),
            Path("config.json"),
            Path("agent") / "instruction.txt",
            Path("agent") / "openclaw.session.jsonl",
            Path("agent") / "codeskill-openclaw-state" / "openclaw-agent.sqlite",
            Path("verifier") / "reward.txt",
            Path("verifier") / "ctrf.json",
        ):
            raw_evidence["raw_artifacts"][relative.as_posix()] = _ref(trial_dir / relative)
    packet_dir = context["artifact_root"] / "finish-packet"
    if trial_dir is None:
        raw_evidence["import_failure"] = {
            "error_type": "HarborTrialDirectoryMissing",
            "error": "official Harbor returned without a matching result.json directory",
        }
        # A nonzero Harbor process without a matching result is still an
        # ambiguous launcher/setup boundary: it could be a malformed job,
        # failed task startup, or a worker failure that never emitted the
        # official result schema.  Only a real, importable Harbor result may
        # be classified as a task-level infrastructure failure and advance
        # the ordered C-only schedule.  Preserve this process/sidecar evidence
        # and stop for reconciliation rather than turning a broken driver into
        # an empty task result.
        write_json(context["artifact_root"] / "import-failure.json", raw_evidence)
        raise COnlyHarborDriverError("official Harbor exited without an importable result directory; current task requires reconciliation")
    try:
        # Harbor's normalized outcome deliberately omits the large raw
        # exception_info payload.  Read the official result before importing
        # it so a reward file cannot hide an agent/verifier execution failure
        # and accidentally open a manager phase.
        official_result = _read_json_file(trial_dir / "result.json", field="official Harbor result")
        result_exception = _result_exception_info(official_result)
        manifest = import_harbor_openclaw_trial(
            trial_id=trial_id,
            instance_id=task_id,
            harbor_trial_dir=trial_dir,
            sidecar_evidence_dir=context["sidecar_dir"],
            output_dir=packet_dir,
        )
        trace_path = packet_dir / "trajectory-evidence.json"
        trace = _read_json_file(trace_path, field="official normalized trajectory")
        source = _object(trace.get("source"), field="official normalized trajectory.source")
        if source.get("session_id") != context["session_id"]:
            raise COnlyHarborDriverError("official trajectory session does not match the frozen C-only session")
        # normalize_openclaw_trial's ``historical`` flag describes the generic
        # importer, not the provenance of this new trial.  Preserve its raw
        # session and packet hashes while making the derived live status
        # explicit for the C-only coordinator.
        trace["historical"] = False
        trace["source_kind"] = "current_c_only_official_harbor"
        source["live_trial_id"] = trial_id
        source["c_only_round_id"] = raw_evidence["round_id"]
        source["c_only_task_id"] = task_id
        source["historical_baseline_used"] = False
        # The coordinator validates the bytes behind every trajectory ref.  A
        # normalized trace may have several source/session fields, so retain a
        # single immutable binding record in the derived live copy rather than
        # asking a caller to infer identity from a filename or from Harbor's
        # result directory.
        trace["r015_binding"] = {
            "round_id": raw_evidence["round_id"],
            "task_id": task_id,
            "trial_id": trial_id,
            "session_id": str(context["session_id"]),
        }
        live_trace_path = context["artifact_root"] / "trajectory-live.json"
        write_json(live_trace_path, trace)
        live_trace_ref = _ref(live_trace_path)
        if not live_trace_ref.get("exists"):
            raise COnlyHarborDriverError("live trajectory copy was not written")
        raw_evidence["packet_manifest"] = deepcopy(manifest)
        raw_evidence["raw_artifacts"]["finish_packet_manifest"] = _ref(packet_dir / "manifest.json")
        raw_evidence["raw_artifacts"]["trajectory_packet"] = _ref(trace_path)
        raw_evidence["raw_artifacts"]["trajectory_live"] = live_trace_ref
        raw_evidence["session_binding"] = deepcopy(manifest.get("session_binding"))
        result_value = _read_json_file(packet_dir / "trial-result.json", field="official trial result")
        raw_evidence["official_reward"] = result_value.get("official_reward")
        outcome_value = result_value.get("outcome")
        normalized_exception_value = (
            outcome_value.get("trial_exception")
            if isinstance(outcome_value, dict) and outcome_value.get("trial_exception")
            else outcome_value.get("exception")
            if isinstance(outcome_value, dict) and outcome_value.get("exception")
            else None
        )
        normalized_exception = normalized_exception_value is not None
        if result_exception is not None:
            # ``exception_info`` is an official execution observation, not a
            # universal infrastructure classification.  Harbor uses
            # NonZeroAgentExitCodeError for ordinary solver/tool failures as
            # well as for malformed provider tool calls.  Once Harbor has
            # emitted a verifier reward and the public session importer has
            # produced a complete trace, that current-round trace remains the
            # authoritative C-only trajectory.  Preserve the exception next
            # to it and let the existing extraction/event validators decide
            # what can be learned from the trace.
            raw_evidence["classification"] = "agent_failure"
            raw_evidence["official_exception"] = {
                "source_field": result_exception["field"],
                "exception_type": result_exception["exception_type"],
                "message": result_exception["message"],
                "result_path": _ref(trial_dir / "result.json"),
                "result_id": official_result.get("id"),
                "agent_result": deepcopy(official_result.get("agent_result")),
                "verifier_result": deepcopy(official_result.get("verifier_result")),
            }
            raw_evidence["trajectory_eligibility"] = {
                "status": "eligible_current_round_trace",
                "reason": "official verifier reward and complete public session trace are present; exception metadata is retained without blanket infrastructure exclusion",
                "manager_phases_allowed": True,
            }
        elif normalized_exception:
            raw_evidence["classification"] = "agent_failure"
            raw_evidence["official_exception"] = {
                "source_field": "normalized_trace.outcome",
                "value": deepcopy(normalized_exception_value),
            }
            raw_evidence["trajectory_eligibility"] = {
                "status": "eligible_current_round_trace",
                "reason": "the public normalized trace carries an exception observation but remains importable and complete",
                "manager_phases_allowed": True,
            }
        # An importable official reward/trajectory is authoritative even if
        # Harbor's top-level CLI returned nonzero or the result retained an
        # agent exception after writing it.  Setup/build failures with no
        # importable reward/trajectory are handled by _official_failure_packet
        # above and remain infrastructure failures without a trajectory.
        outcome = "completed"
        raw_evidence["process_exit_anomaly"] = process.get("classification") != "completed"
        trajectory = {
            "round_id": raw_evidence["round_id"],
            "task_id": task_id,
            "trial_id": trial_id,
            "session_id": str(context["session_id"]),
            "complete": True,
            "path": str(live_trace_path),
            "sha256": live_trace_ref["sha256"],
        }
        return {
            "outcome": outcome,
            "trajectory": trajectory if outcome == "completed" else None,
            "raw_evidence": raw_evidence,
            "proxy_attempt_records": attempts,
            "packet_manifest": manifest,
            "trial_dir": trial_dir,
            "trace": trace,
        }
    except (HarborTrialEvidenceError, COnlyHarborDriverError, TraceImportError, OSError, ValueError) as error:
        # The official Harbor process did run, but a trial directory that
        # exists and cannot be imported is a driver/importer failure.  Do not
        # downgrade it to a task-level infra result: preserve the raw refs and
        # stop the ordered coordinator for manual reconciliation.
        raw_evidence["import_failure"] = {"error_type": type(error).__name__, "error": str(error)}
        raw_evidence["raw_artifacts"]["finish_packet_dir"] = {"path": str(packet_dir), "exists": packet_dir.exists()}
        write_json(context["artifact_root"] / "import-failure.json", raw_evidence)
        raise COnlyHarborDriverError("official Harbor evidence import failed; current task requires reconciliation") from error


def _prompt_path(name: str) -> Path:
    path = ROOT / "prompts" / name
    if not path.is_file():
        raise COnlyHarborDriverError(f"required paper prompt is missing: {path}")
    return path


def _historical_thinking_policy(context: dict[str, Any]) -> str:
    driver = context.get("driver_config")
    if not isinstance(driver, dict):
        driver = {}
    try:
        return validate_historical_thinking_policy(driver.get("historical_thinking_policy", "keep"))
    except ProjectionError as error:
        raise COnlyHarborDriverError(str(error)) from error


def _historical_thinking_policy_identity(context: dict[str, Any]) -> dict[str, str]:
    policy = _historical_thinking_policy(context)
    identity = {
        "policy": policy,
        "policy_version": HISTORICAL_THINKING_POLICY_VERSION,
    }
    identity["policy_sha256"] = _hash_json(identity)
    return identity


def _manager_context(context: dict[str, Any], *, output_path: Path) -> tuple[ManagerClient, R012EvolutionMaintenanceExecutor, dict[str, Any]]:
    target = _object(context["target"], field="prepared_target")
    upstream = _object(context.get("upstream", {}), field="baseline_observed")
    base_url = str(upstream.get("endpoint") or target.get("endpoint") or "").rstrip("/")
    if not base_url:
        raise COnlyHarborDriverError("manager endpoint is not configured")
    model_id = str(target["model_id"])
    configured_manager_root = context.get("trusted_manager_root")
    if configured_manager_root is not None:
        raw_manager_root = Path(str(configured_manager_root))
        if not raw_manager_root.is_absolute():
            raise COnlyHarborDriverError("trusted manager root must be absolute")
        manager_root = raw_manager_root.resolve()
        run_dir = manager_root.parent
    else:
        # Normal launches place output under <run>/round-N/task.  Recovery
        # launches may place output under a dedicated child namespace, but
        # they always supply the coordinator-owned manager root explicitly.
        run_dir = output_path.parents[2]
        manager_root = run_dir / "manager"
    driver_config = _object(context.get("driver_config", {}), field="driver_config")
    configured_ledger = driver_config.get("manager_ledger")
    if isinstance(configured_ledger, str) and configured_ledger and not configured_ledger.startswith("env:") and not configured_ledger.startswith("run-dir/"):
        ledger_path = _resolve_path(configured_ledger, base=ROOT, field="manager_ledger")
    else:
        ledger_path = run_dir / "manager-ledger.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    contract: dict[str, str] = {}
    for label, path in (("spec", ROOT / "docs" / "REPRODUCTION_SPEC.md"), ("decisions", ROOT / "docs" / "RESEARCH_DECISIONS.md")):
        if path.is_file():
            contract[label] = sha256_file(path)
    ledger = update_development_ledger_limit(
        ledger_path,
        new_limit=None,
        reason="R015 C-only official driver: durable development manager ledger activation",
        contract=contract,
    )
    output_budget_profile = driver_config.get("manager_output_budget_profile", "default")
    if output_budget_profile == "default":
        manager_max_output_tokens = 8192
    elif output_budget_profile == "r015_thinking_ab_16k":
        manager_max_output_tokens = 16384
    else:
        raise COnlyHarborDriverError("unknown manager output budget profile")
    profile = ManagerProfile(
        base_url=base_url,
        model=model_id,
        timeout_seconds=300,
        max_output_tokens=manager_max_output_tokens,
        manager_context_tokens=int(target["context_tokens"]),
        safety_tokens=4096,
        temperature=0.0,
        reasoning_effort=str(target["reasoning_effort"]),
        max_total_calls=None,
        output_budget_profile=output_budget_profile,
    )
    manager = ManagerClient(profile, manager_root, contract, ledger_path, exact_token_counter=ServerMessageTokenCounter(base_url))
    profile_value = context["profile"]
    encoder: Any | None = None
    # MiniLM is loaded lazily by _ensure_encoder once a Fig.9 operation is
    # actually needed.  This keeps an explicit model-output skip from turning
    # into a network-dependent setup failure.
    executor = R012EvolutionMaintenanceExecutor(
        manager=manager,
        encoder=encoder,
        journal_root=run_dir / "manager-journals" / str(context["task_id"]),
        instance_id=str(context["task_id"]),
        profile=profile_value,
        selections={
            str(context["trial_id"]): {
                "trial_id": str(context["trial_id"]),
                "instance_id": str(context["task_id"]),
                "arm": "C",
                "action": "evaluate_all_supplied",
                "reason": "C-only current trial evaluates every actually supplied skill",
            }
        },
        evolution_prompt=_prompt_path("custom/r015_fig08_evolution_preserve_code_examples.md").read_text(encoding="utf-8"),
        maintenance_prompt=_prompt_path("custom/r015_fig09_maintenance_preserve_code_examples.md").read_text(encoding="utf-8"),
        use_visible_maintenance=True,
    )
    context["driver_config"] = driver_config
    context["manager_ledger"] = ledger_path
    context["manager"] = manager
    context["executor"] = executor
    context["manager_contract"] = contract
    context["manager_ledger_before_calls"] = len(ledger.get("calls", []))
    return manager, executor, context


def _ensure_encoder(context: dict[str, Any], executor: R012EvolutionMaintenanceExecutor) -> Any:
    if executor.encoder is None:
        profile = _object(context["profile"], field="retrieval_profile")
        runtime = _object(profile.get("runtime"), field="retrieval_profile.runtime")
        encoder = MiniLMEncoder(
            repo_id=str(runtime.get("minilm_repo_id", "sentence-transformers/all-MiniLM-L6-v2")),
            revision=str(runtime.get("minilm_revision")),
        )
        encoder.load()
        executor.encoder = encoder
    return executor.encoder


def _response_ref(manager: ManagerClient, call_id: str) -> dict[str, Any]:
    path = manager.run_dir / "model_calls" / call_id / "response.json"
    if not path.is_file():
        raise COnlyHarborDriverError(f"manager call {call_id} has no durable response.json")
    return {"call_id": call_id, "path": str(path), "sha256": sha256_file(path)}


def _model_output_from_response_ref(
    value: dict[str, Any],
    *,
    field: str,
    expected_call_id: str,
) -> dict[str, Any]:
    """Read the model JSON from the immutable ManagerClient response bytes."""
    response = _object(value, field=field)
    if _text(response.get("call_id"), field=f"{field}.call_id") != expected_call_id:
        raise COnlyHarborDriverError(f"{field}.call_id differs from the candidate manager call")
    path = Path(_text(response.get("path"), field=f"{field}.path"))
    digest = _text(response.get("sha256"), field=f"{field}.sha256")
    if not path.is_file() or sha256_file(path) != digest:
        raise COnlyHarborDriverError(f"{field} path/hash changed")
    record = _read_json_file(path, field=field)
    if record.get("kind") != "live_manager_call" or record.get("finish_reason") != "stop":
        raise COnlyHarborDriverError(f"{field} is not a completed ManagerClient response")
    parsed = _object(record.get("parsed_response"), field=f"{field}.parsed_response")
    raw_response = _text(record.get("raw_response"), field=f"{field}.raw_response")
    try:
        parsed_raw = json.loads(raw_response)
    except json.JSONDecodeError as error:
        raise COnlyHarborDriverError(f"{field}.raw_response is not valid JSON") from error
    if canonical_json(parsed_raw) != canonical_json(parsed):
        raise COnlyHarborDriverError(f"{field}.parsed_response differs from raw_response")
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        raise COnlyHarborDriverError(f"{field} has no manager choice")
    choice = _object(choices[0], field=f"{field}.choice")
    if choice.get("finish_reason") != "stop":
        raise COnlyHarborDriverError(f"{field}.choice did not finish with stop")
    message = _object(choice.get("message"), field=f"{field}.choice.message")
    content = _text(message.get("content"), field=f"{field}.choice.message.content")
    try:
        model_output = json.loads(content)
    except json.JSONDecodeError as error:
        raise COnlyHarborDriverError(f"{field} contains malformed model JSON") from error
    return _object(model_output, field=f"{field}.model_output")


def _read_immutable_json_ref(value: dict[str, Any], *, field: str) -> tuple[dict[str, Any], Path]:
    reference = _object(value, field=field)
    path = Path(_text(reference.get("path"), field=f"{field}.path"))
    digest = _text(reference.get("sha256"), field=f"{field}.sha256")
    if not path.is_file() or sha256_file(path) != digest:
        raise COnlyHarborDriverError(f"{field} path/hash changed")
    return _read_json_file(path, field=field), path


def _manager_derivation_binding(
    *,
    context: dict[str, Any],
    executor: R012EvolutionMaintenanceExecutor,
    call_id: str,
    journal_ref: dict[str, Any],
) -> dict[str, Any] | None:
    """Capture immutable files that prove which trajectory view made material.

    Reconciled pre-policy calls and controlled legacy fixtures remain keep-only
    compatibility inputs.  A newly generated exclude artifact must always
    carry this complete request/journal/context binding.
    """
    manager = getattr(executor, "manager", None)
    request_path = getattr(manager, "run_dir", Path()) / "model_calls" / call_id / "request.json"
    if not isinstance(manager, ManagerClient) or not request_path.is_file():
        if _historical_thinking_policy(context) == "keep":
            return None
        raise COnlyHarborDriverError("exclude-derived material has no immutable ManagerClient request binding")
    request = _read_json_file(request_path, field="derived material manager request")
    metadata = _object(request.get("call_metadata"), field="derived material manager request.call_metadata")
    raw_trajectory_context = metadata.get("trajectory_context")
    if not isinstance(raw_trajectory_context, dict):
        if _historical_thinking_policy(context) == "keep":
            return None
        raise COnlyHarborDriverError(
            "exclude-derived material request has no immutable trajectory context binding"
        )
    trajectory_context = _object(
        raw_trajectory_context,
        field="derived material manager request.trajectory_context",
    )
    return {
        "request": _ref(request_path),
        "journal": _journal_evidence_ref(journal_ref),
        "trajectory_context": deepcopy(trajectory_context),
    }


def _validate_manager_derivation_binding(
    value: Any,
    *,
    field: str,
    wrapper_policy: Any,
    expected_policy: dict[str, str],
    expected_traces: list[dict[str, Any]],
    response_ref: dict[str, Any],
    expected_call_id: str,
    allowed_journal_statuses: set[str],
) -> dict[str, Any]:
    """Verify derived material against request, journal and context bytes.

    Missing bindings are accepted only as legacy ``keep`` material.  This is
    intentionally asymmetric: an unknown historical source can never be
    upgraded or relabelled to ``exclude`` by editing its outer wrapper.
    """
    if value is None:
        if expected_policy.get("policy") != "keep":
            raise COnlyHarborDriverError(f"{field} has no request-bound policy provenance for exclude")
        legacy_policy = wrapper_policy
        if legacy_policy is None:
            legacy_policy = expected_policy
        if canonical_json(legacy_policy) != canonical_json(expected_policy):
            raise COnlyHarborDriverError(f"{field} legacy keep policy marker differs")
        return {"binding_kind": "legacy_keep_response_binding"}

    binding = _object(value, field=field)
    request_record, request_path = _read_immutable_json_ref(
        _object(binding.get("request"), field=f"{field}.request"),
        field=f"{field}.request",
    )
    journal, _journal_path = _read_immutable_json_ref(
        _object(binding.get("journal"), field=f"{field}.journal"),
        field=f"{field}.journal",
    )
    context_record, _context_path = _read_immutable_json_ref(
        _object(binding.get("trajectory_context"), field=f"{field}.trajectory_context"),
        field=f"{field}.trajectory_context",
    )
    response = _object(response_ref, field=f"{field}.response")
    response_path = Path(_text(response.get("path"), field=f"{field}.response.path"))
    if request_path.name != "request.json" or response_path.name != "response.json" or request_path.parent != response_path.parent:
        raise COnlyHarborDriverError(f"{field} request/response are not one ManagerClient call")
    if request_path.parent.name != expected_call_id or response.get("call_id") != expected_call_id:
        raise COnlyHarborDriverError(f"{field} call identity differs from request/response paths")

    request_payload = _object(request_record.get("request"), field=f"{field}.request.request")
    request_messages = request_payload.get("messages")
    if not isinstance(request_messages, list):
        raise COnlyHarborDriverError(f"{field} request has no messages")
    metadata = _object(request_record.get("call_metadata"), field=f"{field}.request.call_metadata")
    journal_metadata = _object(journal.get("call_metadata"), field=f"{field}.journal.call_metadata")
    if canonical_json(metadata) != canonical_json(journal_metadata):
        raise COnlyHarborDriverError(f"{field} request and journal metadata differ")
    if canonical_json(journal.get("messages")) != canonical_json(request_messages):
        raise COnlyHarborDriverError(f"{field} request and journal messages differ")
    if journal.get("messages_sha256") != _hash_json(request_messages):
        raise COnlyHarborDriverError(f"{field} journal messages hash differs")
    if journal.get("call_id") != expected_call_id or journal.get("status") not in allowed_journal_statuses:
        raise COnlyHarborDriverError(f"{field} journal is not the completed producing call")
    if request_record.get("purpose") != journal.get("purpose"):
        raise COnlyHarborDriverError(f"{field} request and journal purpose differ")

    actual_policy = {
        "policy": metadata.get("historical_thinking_policy"),
        "policy_version": metadata.get("historical_thinking_policy_version"),
    }
    actual_policy["policy_sha256"] = _hash_json({
        "policy": actual_policy["policy"],
        "policy_version": actual_policy["policy_version"],
    })
    if canonical_json(actual_policy) != canonical_json(expected_policy):
        raise COnlyHarborDriverError(f"{field} producing request used a different historical thinking policy")
    if wrapper_policy is None or canonical_json(wrapper_policy) != canonical_json(actual_policy):
        raise COnlyHarborDriverError(f"{field} outer policy marker differs from its producing request")
    if canonical_json(metadata.get("trajectory_context")) != canonical_json(binding.get("trajectory_context")):
        raise COnlyHarborDriverError(f"{field} request points at a different trajectory context")
    if context_record.get("historical_thinking_policy") != expected_policy["policy"] or context_record.get("historical_thinking_policy_version") != expected_policy["policy_version"]:
        raise COnlyHarborDriverError(f"{field} trajectory context used a different historical thinking policy")
    if context_record.get("messages_sha256") != _hash_json(request_messages):
        raise COnlyHarborDriverError(f"{field} trajectory context does not bind the actual request messages")

    sources = context_record.get("sources")
    if not isinstance(sources, list) or len(sources) != len(expected_traces):
        raise COnlyHarborDriverError(f"{field} trajectory context source count differs")
    remaining = list(sources)
    for trace in expected_traces:
        source = _object(trace.get("source"), field=f"{field}.expected_trace.source")
        source_id = canonical_instance_id(_text(source.get("canonical_instance_id"), field=f"{field}.expected_trace.source.canonical_instance_id"))
        matches = [item for item in remaining if isinstance(item, dict)
                   and canonical_instance_id(str(item.get("source", {}).get("canonical_instance_id", ""))) == source_id]
        if len(matches) != 1:
            raise COnlyHarborDriverError(f"{field} trajectory context source identity differs for {source_id}")
        saved = matches[0]
        remaining.remove(saved)
        projected = project_historical_thinking(trace, policy=expected_policy["policy"])["manager_trace"]
        if saved.get("trace_sha256") != _hash_json(trace) or saved.get("manager_view_sha256") != _hash_json(projected):
            raise COnlyHarborDriverError(f"{field} trajectory context source hashes differ for {source_id}")
        if canonical_json(saved.get("source")) != canonical_json(source):
            raise COnlyHarborDriverError(f"{field} trajectory context source metadata differs for {source_id}")
    return deepcopy(binding)


def _snapshot_manager_ledger(context: dict[str, Any], manager_context: dict[str, Any]) -> dict[str, Any]:
    """Copy the mutable run ledger into an immutable per-trial artifact.

    ``ManagerClient`` intentionally appends to one run-level ledger across
    tasks.  A publication must therefore never point at that live file: a
    later task would change its bytes and invalidate an earlier output.  The
    snapshot is taken after all extraction/Fig.8/Fig.9 calls for this task and
    before the publication stage is written.
    """
    source_value = manager_context.get("manager_ledger")
    if source_value is None:
        raise COnlyHarborDriverError("manager context has no ledger path to snapshot")
    source = Path(str(source_value))
    if not source.is_file():
        raise COnlyHarborDriverError(f"manager ledger is missing before immutable snapshot: {source}")
    target = Path(context["artifact_root"]) / "manager-ledger-snapshot.json"
    source_hash = sha256_file(source)
    source_bytes = source.read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != source_bytes:
            raise COnlyHarborDriverError(f"immutable manager ledger snapshot already differs: {target}")
    else:
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_bytes(source_bytes)
        temporary.replace(target)
    reference = _ref(target)
    if not reference.get("exists"):
        raise COnlyHarborDriverError(f"immutable manager ledger snapshot was not written: {target}")
    return {
        **reference,
        "kind": "immutable_manager_ledger_snapshot",
        "source_path": str(source),
        "source_sha256_at_snapshot": source_hash,
        "manager_calls_before_trial": manager_context.get("manager_ledger_before_calls"),
    }


def _validate_context_summary(value, *, segment_steps, final_segment):
    """D03/V02 cites original evidence; R006's three-pair stress cap does not apply."""
    ids = [str(step["source_entry_id"]) for step in segment_steps]
    if "evidence_step_ids" in value:
        checked = validate_evidence_summary(value, {"steps": segment_steps})
        summary = {"summary": checked["summary"], "covered_step_ids": ids,
                   "verbatim_evidence_step_ids": checked["evidence_step_ids"], "coverage_kind": "supplied_segment",
                   "evidence_id_expansions": checked.get("source_step_id_expansions", {})}
    else:
        # Previously validated R006 summaries can be reused without rewriting
        # their model output; a rejected/incomplete coverage list stays invalid.
        summary = validate_budget_summary(value, segment_step_ids=ids)
    evidence_ids = list(summary["verbatim_evidence_step_ids"])
    if final_segment:
        final_observation = next((str(step["source_entry_id"]) for step in reversed(segment_steps)
                                  if step.get("role") == "toolResult"), None)
        if final_observation is not None and final_observation not in evidence_ids:
            evidence_ids.append(final_observation)
    fragments = expand_evidence_fragments(segment_steps, evidence_ids)
    return summary, fragments


def _tokenizer_request_identity(counter: Any) -> dict[str, Any]:
    """Return stable tokenizer/template routing inputs available to the caller.

    The SGLang endpoint owns the chat template.  Its method and endpoint are
    therefore part of summary reuse identity; optional explicit template or
    tokenizer revision attributes are included when a counter exposes them.
    Runtime timing and response bytes are deliberately excluded.
    """
    identity: dict[str, Any] = {
        "counter_type": f"{type(counter).__module__}.{type(counter).__qualname__}",
        "method": getattr(counter, "method", None),
        "base_url": getattr(counter, "base_url", None),
    }
    for field in ("template_identity", "tokenizer_identity", "tokenizer_revision"):
        value = getattr(counter, field, None)
        if value is not None:
            identity[field] = deepcopy(value)
    return identity


def _summary_request_identity(
    *,
    messages: list[dict[str, Any]],
    options: dict[str, Any],
    historical_thinking_policy: dict[str, str],
    source_identity: dict[str, Any],
    counter: Any,
) -> dict[str, Any]:
    value = {
        "schema_version": 1,
        "kind": "r015_context_summary_request_identity",
        "summary_output_schema": {
            "name": "r015_d03_v02_evidence_summary",
            "version": 1,
            "required_fields": [
                "summary",
                "covered_step_ids",
                "verbatim_evidence_step_ids",
            ],
        },
        "messages_sha256": _hash_json(messages),
        "request_options": deepcopy(options),
        "historical_thinking_policy": deepcopy(historical_thinking_policy),
        "source_identity": deepcopy(source_identity),
        "tokenizer_template_identity": _tokenizer_request_identity(counter),
    }
    value["identity_sha256"] = _hash_json(value)
    return value


def _matches_legacy_keep_summary_metadata(
    metadata: dict[str, Any],
    *,
    request_identity: dict[str, Any],
    historical_thinking_policy: dict[str, str],
    payload: dict[str, Any],
) -> bool:
    """Recognize the exact completed-summary metadata emitted by 53e3fc7."""
    if historical_thinking_policy != {
        "policy": "keep",
        "policy_version": HISTORICAL_THINKING_POLICY_VERSION,
    }:
        return False
    if set(metadata) != {
        "condition",
        "trajectory_input_mode",
        "historical_thinking_policy",
        "source_trace_sha256",
        "segment_step_ids",
        "final_segment",
        "segment_allowance_tokens",
    }:
        return False
    source_identity = request_identity.get("source_identity")
    if not isinstance(source_identity, dict):
        return False
    required_source_identity = {
        "immutable_trace_sha256": source_identity.get("immutable_trace_sha256"),
        "manager_view_sha256": source_identity.get("manager_view_sha256"),
        "historical_thinking_policy": source_identity.get("historical_thinking_policy"),
        "historical_thinking_policy_version": source_identity.get(
            "historical_thinking_policy_version"
        ),
    }
    if (
        not all(isinstance(value, str) and value for value in required_source_identity.values())
        or required_source_identity["historical_thinking_policy"] != "keep"
        or required_source_identity["historical_thinking_policy_version"]
        != HISTORICAL_THINKING_POLICY_VERSION
    ):
        return False
    segment_steps = payload.get("segment_steps")
    if not isinstance(segment_steps, list):
        return False
    step_ids = [
        str(step.get("source_entry_id"))
        for step in segment_steps
        if isinstance(step, dict) and step.get("source_entry_id") is not None
    ]
    if len(step_ids) != len(segment_steps):
        return False
    allowance = metadata.get("segment_allowance_tokens")
    return (
        metadata.get("condition") == "C-only"
        and metadata.get("trajectory_input_mode") == "evidence_compacted"
        and metadata.get("historical_thinking_policy") == historical_thinking_policy
        and metadata.get("source_trace_sha256") == _hash_json(required_source_identity)
        and metadata.get("segment_step_ids") == step_ids
        and metadata.get("final_segment") is payload.get("final_segment")
        and isinstance(allowance, int)
        and not isinstance(allowance, bool)
        and allowance > 0
    )


def _reuse_context_summary(
    executor,
    *,
    trial_id,
    messages,
    options,
    request_identity,
    historical_thinking_policy=None,
):
    """Reuse only a completed summary bound to the current actual request."""
    expected_policy = historical_thinking_policy or {
        "policy": "keep",
        "policy_version": HISTORICAL_THINKING_POLICY_VERSION,
    }
    if not isinstance(messages, list) or not messages:
        raise COnlyHarborDriverError("current summary request messages are missing")
    payload = json.loads(messages[-1]["content"])
    journal_root = getattr(executor, "journal_root", None)
    if journal_root is None:
        return None
    saved = [(path, read_json(path)) for path in (Path(journal_root) / sha256_text(trial_id)[:20]).glob("context-summary-*.json")]
    for path, journal in sorted(saved, key=lambda item: (item[1].get("status") != "context_summary_validated", str(item[0]))):
        # Recovery changes importer-envelope hashes. Compare the complete
        # actual source/segment payload below, not that derived envelope.
        if journal.get("status") not in {"context_summary_validated", "context_summary_rejected"}:
            continue
        saved_messages = journal.get("messages")
        if canonical_json(saved_messages) != canonical_json(messages):
            continue
        saved_policy = journal.get("call_metadata", {}).get("historical_thinking_policy")
        if saved_policy is None and expected_policy.get("policy") == "keep":
            saved_policy = expected_policy
        if canonical_json(saved_policy) != canonical_json(expected_policy):
            continue
        saved_identity = journal.get("call_metadata", {}).get("summary_request_identity")
        if saved_identity is None:
            # Legacy summaries are compatible only with keep and only after
            # the exact saved messages/options below match.  An unbound
            # historical summary can never be relabelled as exclude.
            if expected_policy.get("policy") != "keep":
                continue
        elif canonical_json(saved_identity) != canonical_json(request_identity):
            continue
        call_id = journal.get("call_id")
        if call_id is None:
            completed = [call for call in read_json(executor.manager.ledger_path)["calls"]
                         if call.get("purpose") == journal.get("purpose") and call.get("status") == "succeeded"]
            if len(completed) != 1:
                raise COnlyHarborDriverError("saved summary has no unique completed ledger call")
            call_id = completed[0]["call_id"]
        request_path = executor.manager.run_dir / "model_calls" / call_id / "request.json"
        response_path = request_path.with_name("response.json")
        request_record = read_json(request_path)
        request = request_record["request"]
        response = read_json(response_path)
        if journal.get("messages_sha256") != _hash_json(journal["messages"]):
            raise COnlyHarborDriverError("saved context summary lost its request/profile binding")
        if canonical_json(request.get("messages")) != canonical_json(journal["messages"]):
            raise COnlyHarborDriverError("saved context summary lost its request/profile binding")
        if response.get("finish_reason") != "stop":
            raise COnlyHarborDriverError("saved context summary lost its request/profile binding")
        if (journal.get("trial_id") != trial_id
                or journal.get("profile_sha256") != profile_sha256(executor.profile)
                or any(request.get(key) != option for key, option in options.items())):
            continue
        request_metadata = request_record.get("call_metadata")
        if isinstance(request_metadata, dict):
            if canonical_json(request_metadata) != canonical_json(journal.get("call_metadata")):
                continue
            request_policy = request_metadata.get("historical_thinking_policy")
            if request_policy is None and expected_policy.get("policy") == "keep":
                request_policy = expected_policy
            if canonical_json(request_policy) != canonical_json(expected_policy):
                continue
            request_summary_identity = request_metadata.get("summary_request_identity")
            if request_summary_identity is None:
                if expected_policy.get("policy") != "keep":
                    continue
                if not _matches_legacy_keep_summary_metadata(
                    request_metadata,
                    request_identity=request_identity,
                    historical_thinking_policy=expected_policy,
                    payload=payload,
                ):
                    raise COnlyHarborDriverError(
                        "completed legacy context summary cannot be proven equivalent; "
                        "explicit reconciliation is required"
                    )
            elif canonical_json(request_summary_identity) != canonical_json(request_identity):
                continue
            source_identity = request_identity.get("source_identity")
            if request_summary_identity is not None and isinstance(source_identity, dict):
                if request_metadata.get("source_trace_sha256") not in {
                    None,
                    source_identity.get("immutable_trace_sha256"),
                }:
                    continue
                if request_metadata.get("manager_view_sha256") not in {
                    None,
                    source_identity.get("manager_view_sha256"),
                }:
                    continue
        else:
            raise COnlyHarborDriverError(
                "completed legacy context summary has no immutable request metadata; "
                "explicit reconciliation is required"
            )
        if request_record.get("purpose") not in {None, journal.get("purpose")}:
            raise COnlyHarborDriverError("saved context summary request purpose differs from its journal")
        value = json.loads(response["parsed_response"]["choices"][0]["message"]["content"])
        try:
            summary, fragments = _validate_context_summary(
                value, segment_steps=payload["segment_steps"], final_segment=payload["final_segment"],
            )
        except (ValueError, TypeError):
            if journal.get("status") == "context_summary_rejected":
                continue
            raise
        if journal.get("status") == "context_summary_validated" and value != journal.get("model_output", journal.get("summary")):
            raise COnlyHarborDriverError("saved context summary differs from its original response")
        return summary, fragments, {"journal": _ref(path), "response": _response_ref(executor.manager, call_id),
                                    "original_journal_status": journal["status"], "revalidated": True}
    return None


def _refuse_uncertain_context_summary_replay(
    executor: R012EvolutionMaintenanceExecutor,
    *,
    trial_id: str,
    messages: list[dict[str, Any]],
    request_identity: dict[str, Any],
    historical_thinking_policy: dict[str, str],
) -> None:
    """Block an exact summary request even when its phase hash has changed.

    R012's journal guard is phase-path based.  Summary identity revisions can
    legitimately change that path, so scan all same-trial summary journals for
    an earlier exact request whose outcome cannot be reused automatically.
    The original journal remains untouched for explicit reconciliation.
    """
    journal_root = getattr(executor, "journal_root", None)
    if journal_root is None:
        return
    uncertain_statuses = {
        "prepared_before_manager_call",
        "manager_call_raised",
        "manager_result_invalid",
        "context_summary_rejected",
    }
    trial_root = Path(journal_root) / sha256_text(trial_id)[:20]
    for path in sorted(trial_root.glob("context-summary-*.json")):
        journal = read_json(path)
        if journal.get("status") not in uncertain_statuses:
            continue
        if journal.get("trial_id") != trial_id:
            continue
        saved_messages = journal.get("messages")
        if canonical_json(saved_messages) != canonical_json(messages):
            continue
        if journal.get("messages_sha256") != _hash_json(messages):
            raise COnlyHarborDriverError(
                f"uncertain context summary journal lost its message binding: {path}"
            )
        if journal.get("profile_sha256") != profile_sha256(executor.profile):
            continue
        metadata = journal.get("call_metadata")
        if not isinstance(metadata, dict):
            continue
        saved_policy = metadata.get("historical_thinking_policy")
        if saved_policy is None and historical_thinking_policy.get("policy") == "keep":
            saved_policy = historical_thinking_policy
        if canonical_json(saved_policy) != canonical_json(historical_thinking_policy):
            continue
        saved_identity = metadata.get("summary_request_identity")
        if saved_identity is not None and canonical_json(saved_identity) != canonical_json(request_identity):
            # Same rendered messages can still be a genuinely different
            # request when options, source, schema, or tokenizer identity move.
            continue
        raise COnlyHarborDriverError(
            "uncertain context summary request already exists; explicit reconciliation is required: "
            f"{path}"
        )


def _prepare_trajectory_messages(
    *, context: dict[str, Any], executor: R012EvolutionMaintenanceExecutor,
    phase: str, traces: list[dict[str, Any]], builder: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, list[str]]]:
    """D03: full input, lossless projection, then source-cited V02 summaries.

    In-memory summaries are keyed by complete source content. Validated
    durable summaries can be reused after checking identical source payloads;
    uncertain or rejected calls never trigger automatic paid replay.
    Raw trajectories and the protocol's same-round pools are never changed.
    """
    manager = executor.manager
    policy = _historical_thinking_policy(context)
    options = {
        "model": manager.profile.model, "temperature": manager.profile.temperature,
        "max_tokens": manager.profile.max_output_tokens,
        "response_format": {"type": "json_object"},
        "reasoning_effort": manager.profile.reasoning_effort,
    }
    allowance = manager.profile.manager_context_tokens - manager.profile.max_output_tokens - manager.profile.safety_tokens

    policy_views = [project_historical_thinking(trace, policy=policy) for trace in traces]
    policy_traces = [item["manager_trace"] for item in policy_views]
    record: dict[str, Any] = {
        "kind": "r015_d03_manager_context", "phase": phase,
        "historical_thinking_policy": policy,
        "historical_thinking_policy_version": HISTORICAL_THINKING_POLICY_VERSION,
        "trajectory_input_mode": "full",
        "allowance_tokens": allowance, "sources": [],
    }
    for trace, policy_view in zip(traces, policy_views, strict=True):
        manager_view = policy_view["manager_trace"]
        record["sources"].append({
            "source": deepcopy(trace["source"]), "trace_sha256": _hash_json(trace),
            "manager_view_sha256": _hash_json(manager_view),
            "historical_thinking_projection": deepcopy(policy_view["mapping"]),
            "original_step_ids": [str(step["source_entry_id"]) for step in trace["steps"]],
        })

    context_path = Path(context["artifact_root"]) / "manager-context" / f"{phase}.json"
    if context_path.exists():
        raise COnlyHarborDriverError(f"manager context record already exists; refusing replay: {context_path}")

    def count(counted_messages: list[dict[str, Any]], *, stage: str) -> int:
        try:
            return manager.exact_token_counter(counted_messages, request_options=options)
        except (TokenizationError, TimeoutError, OSError, TypeError) as error:
            failure = deepcopy(record)
            failure.update({
                "state": "tokenizer_unavailable",
                "failed_count_stage": stage,
                "error_type": type(error).__name__,
                "error": str(error),
                "tokenizer_exchange": deepcopy(getattr(manager.exact_token_counter, "last_exchange", None)),
            })
            write_json(context_path, failure)
            raise ContextBlocked(
                f"D03 exact tokenizer failed during {stage}; see {context_path}"
            ) from error

    messages = builder(policy_traces)
    full_tokens = count(messages, stage="full_after_historical_policy")
    working = policy_traces
    record.update(
        original_full_request_tokens=full_tokens,
        full_request_tokens_after_historical_policy=full_tokens,
    )
    tokens = full_tokens
    if tokens > allowance:
        projections = [project_trace_for_manager(trace) for trace in policy_traces]
        working = [item["manager_trace"] for item in projections]
        messages = builder(working)
        tokens = count(messages, stage="deduplicated")
        record.update(trajectory_input_mode="deduplicated", deduplicated_request_tokens=tokens)
        for source, projection in zip(record["sources"], projections, strict=True):
            source["projection"] = projection["mapping"]
    if tokens > allowance:
        prompt = _prompt_path("custom/m2_evidence_compaction.md").read_text(encoding="utf-8")
        prompt += "\n\nD03 runtime: apply this V02 evidence contract only after full input exceeds the configured manager budget. The normal context, output reserve, and reasoning settings remain in force."
        # Use the original D03/V02 contract at the approved manager budget;
        # all summaries use the same durable max-effort client.
        # A near-full 257k source exhausted the unchanged 8192-token output
        # budget in reasoning alone. Bound summary material as well as input
        # capacity, so each response can cover its segment and emit evidence.
        segment_allowance = min(allowance, manager.profile.max_output_tokens * 8, 65536)
        record["summary_segment_allowance_tokens"] = segment_allowance
        cache = context.setdefault("trajectory_summary_cache", {})
        compacted = []
        for trace, projected, source_record in zip(traces, working, record["sources"], strict=True):
            source_identity = {
                "immutable_trace_sha256": source_record["trace_sha256"],
                "manager_view_sha256": source_record["manager_view_sha256"],
                "historical_thinking_policy": policy,
                "historical_thinking_policy_version": HISTORICAL_THINKING_POLICY_VERSION,
            }

            def summary_messages(steps: list[dict[str, Any]], *, final: bool) -> list[dict[str, str]]:
                return [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps({
                        "task_context": trace["instruction"], "source": trace["source"],
                        "segment_steps": steps, "final_segment": final,
                        "required_covered_step_ids": [str(step["source_entry_id"]) for step in steps],
                        "coverage_instruction": "Summarize all supplied steps, cite the essential original evidence, and retain observed final results. The driver records complete segment coverage separately.",
                    }, ensure_ascii=False)},
                ]

            segments = action_observation_segments(
                projected["steps"],
                message_count=lambda value: count(value, stage="summary_segment"),
                messages_for_segment=lambda steps: summary_messages(steps, final=True),
                max_input_tokens=segment_allowance,
            )
            segment_requests = []
            policy_identity = {
                "policy": policy,
                "policy_version": HISTORICAL_THINKING_POLICY_VERSION,
            }
            for index, segment in enumerate(segments, start=1):
                final = index == len(segments)
                segment_messages = summary_messages(segment["steps"], final=final)
                segment_requests.append({
                    "segment": segment,
                    "final": final,
                    "messages": segment_messages,
                    "identity": _summary_request_identity(
                        messages=segment_messages,
                        options=options,
                        historical_thinking_policy=policy_identity,
                        source_identity=source_identity,
                        counter=manager.exact_token_counter,
                    ),
                })
            key = _hash_json({
                "source_identity": source_identity,
                "segment_request_identities": [item["identity"] for item in segment_requests],
            })
            source_record["summary_cache_identity"] = key
            source_record["summary_request_identities"] = [deepcopy(item["identity"]) for item in segment_requests]
            cached = cache.get(key)
            if cached is None:
                summaries, summary_refs, selected = [], [], set()
                for segment_request in segment_requests:
                    segment = segment_request["segment"]
                    final = segment_request["final"]
                    # Bind the durable call identity to the exact segment;
                    # different material is a new call, identical material is
                    # still protected by the existing no-replay journal.
                    segment_messages = segment_request["messages"]
                    request_identity = segment_request["identity"]
                    reused = _reuse_context_summary(
                        executor, trial_id=str(context["trial_id"]),
                        messages=segment_messages, options=options,
                        request_identity=request_identity,
                        historical_thinking_policy=policy_identity,
                    )
                    if reused is not None:
                        summary, fragments, refs = reused
                        summaries.append(summary)
                        summary_refs.append(refs)
                        selected.update(str(step["source_entry_id"]) for step in fragments)
                        continue
                    _refuse_uncertain_context_summary_replay(
                        executor,
                        trial_id=str(context["trial_id"]),
                        messages=segment_messages,
                        request_identity=request_identity,
                        historical_thinking_policy=policy_identity,
                    )
                    segment_key = _hash_json({
                        "summary_request_identity": request_identity,
                    })
                    summary_phase = f"context-summary-{segment_key[:20]}"
                    call, journal, invalid = _manager_call(
                        executor, trial_id=str(context["trial_id"]), phase=summary_phase,
                        purpose=f"r015_c_only_evidence_summary:{context['trial_id']}:{segment_key}",
                        messages=summary_messages(segment["steps"], final=final),
                        metadata={"condition": "C-only", "trajectory_input_mode": "evidence_compacted",
                                  "historical_thinking_policy": {
                                      "policy": policy,
                                      "policy_version": HISTORICAL_THINKING_POLICY_VERSION,
                                  },
                                  "summary_request_identity": request_identity,
                                  "source_trace_sha256": source_identity["immutable_trace_sha256"],
                                  "manager_view_sha256": source_identity["manager_view_sha256"],
                                  "segment_step_ids": segment["step_ids"], "final_segment": final,
                                  "segment_allowance_tokens": segment_allowance},
                    )
                    try:
                        if call is None:
                            raise ValueError(f"summary model output is invalid: {invalid}")
                        summary, fragments = _validate_context_summary(
                            call["json"], segment_steps=segment["steps"], final_segment=final,
                        )
                    except (ValueError, TypeError) as error:
                        _finish_manager_journal(executor, journal, status="context_summary_rejected", value={"error": str(error)})
                        raise COnlyHarborDriverError(f"evidence summary failed validation: {error}") from error
                    journal = _finish_manager_journal(
                        executor, journal, status="context_summary_validated",
                        value={"call_id": call["call_id"], "summary": summary, "model_output": deepcopy(call["json"])},
                    )
                    summaries.append(summary)
                    summary_refs.append({"journal": _journal_evidence_ref(journal), "response": journal["response"]})
                    selected.update(str(step["source_entry_id"]) for step in fragments)
                compacted_trace = deepcopy(projected)
                # Keep losslessly projected source entries in original order;
                # do not reintroduce proven duplicate fields or model rewrites.
                compacted_trace["steps"] = [deepcopy(step) for step in projected["steps"] if str(step["source_entry_id"]) in selected]
                compacted_trace.pop("entries", None)
                compacted_trace["trajectory_input_mode"] = "evidence_compacted"
                compacted_trace["evidence_summaries"] = summaries
                cached = {"trace": compacted_trace, "summary_refs": summary_refs,
                          "source_identity": source_identity}
                cache[key] = cached
            compacted.append(cached["trace"])
            source_record["summary_refs"] = deepcopy(cached["summary_refs"])
        working = compacted
        messages = builder(working)
        tokens = count(messages, stage="final_evidence_compacted")
        record["trajectory_input_mode"] = "evidence_compacted"
    visible = {}
    for source_record, trace in zip(record["sources"], working, strict=True):
        retained = [str(step["source_entry_id"]) for step in trace["steps"]]
        source_record["retained_step_ids"] = retained
        source_record["omitted_step_ids"] = [step_id for step_id in source_record["original_step_ids"] if step_id not in set(retained)]
        source_record["omission_reason"] = "covered by source-cited summaries; final claims require retained original evidence" if source_record["omitted_step_ids"] else None
        visible[str(trace["source"]["canonical_instance_id"])] = retained
    record.update(forwarded_request_tokens=tokens, messages_sha256=_hash_json(messages), state="within_budget" if tokens <= allowance else "context_blocked")
    path = context_path
    write_json(path, record)
    if tokens > allowance:
        raise ContextBlocked(f"D03 evidence still exceeds manager budget: {tokens} > {allowance}; see {path}")
    return messages, _ref(path), visible


def _manager_call(
    executor: R012EvolutionMaintenanceExecutor,
    *,
    trial_id: str,
    phase: str,
    purpose: str,
    messages: list[dict[str, str]],
    metadata: dict[str, Any],
    reconciled_call: dict[str, Any] | None = None,
    reconciliation_path: Path | None = None,
    reconciled_context: dict[str, Any] | None = None,
    trajectory_context: dict[str, Any] | None = None,
    source_traces: list[dict[str, Any]] | None = None,
    messages_builder: Any = None,
) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any] | None]:
    """Call the durable manager boundary and preserve model-invalid output.

    Transport, tokenizer, and ledger failures stop the trial.  A response that
    reached the model but failed the JSON contract is a model outcome: its raw
    response remains in the manager call directory and the caller can record a
    bounded invalid extraction attempt.
    """
    manager = executor.manager
    if not isinstance(manager, ManagerClient):
        raise COnlyHarborDriverError("C-only extraction/evolution needs the official ManagerClient")
    if reconciled_call is not None:
        if trajectory_context is not None and _historical_thinking_policy(trajectory_context) != "keep":
            raise COnlyHarborDriverError(
                "a legacy reconciled manager call is bound to historical thinking policy keep; "
                "exclude requires a fresh isolated request"
            )
        if reconciliation_path is None or reconciled_context is None:
            raise COnlyHarborDriverError("a reconciled manager call needs its manifest path and trial context")
        return _reconciled_manager_call(
            executor=executor,
            context=reconciled_context,
            phase=phase,
            purpose=purpose,
            messages=messages,
            reconciliation=reconciled_call,
            reconciliation_path=reconciliation_path,
        )
    visible = None
    if source_traces is not None:
        if trajectory_context is None or messages_builder is None:
            raise COnlyHarborDriverError("trajectory budgeting requires context and its exact message builder")
        messages, context_ref, visible = _prepare_trajectory_messages(
            context=trajectory_context, executor=executor, phase=phase,
            traces=source_traces, builder=messages_builder,
        )
        context_value = _read_json_file(Path(str(context_ref["path"])), field="manager trajectory context")
        metadata = {
            **metadata,
            "trajectory_context": context_ref,
            "historical_thinking_policy": context_value["historical_thinking_policy"],
            "historical_thinking_policy_version": context_value["historical_thinking_policy_version"],
        }
    try:
        call, journal = executor._call_manager(
            trial_id=trial_id,
            phase=phase,
            purpose=purpose,
            messages=messages,
            metadata=metadata,
        )
        ref = _response_ref(manager, str(call["call_id"]))
        if visible is not None:
            call["visible_step_ids_by_source"] = visible
        return call, {"path": str(journal), "sha256": sha256_file(journal), "response": ref}, None
    except ManagerCallError as error:
        call_id = f"call-{manager.call_count:04d}"
        response_path = manager.run_dir / "model_calls" / call_id / "response.json"
        if response_path.is_file():
            response_value = _read_json_file(response_path, field=f"manager {call_id} response")
            classification = response_value.get("classification")
            ref = _response_ref(manager, call_id)
            if classification in {
                "model_output_truncated",
                "model_output_empty",
                "model_output_malformed",
                "model_output_incomplete",
                # Backward-compatible recovery for call artifacts written by
                # manager versions before output failures were split.
                "model_output_invalid",
            }:
                journal_path = executor._journal_path(trial_id, phase)
                return None, {"path": str(journal_path), "sha256": sha256_file(journal_path) if journal_path.is_file() else None, "response": ref}, {"error_type": type(error).__name__, "error": str(error), "classification": classification, "response": ref}
        raise COnlyHarborDriverError(f"manager phase {phase} failed at the transport/preflight boundary: {error}") from error


def _finish_manager_journal(
    executor: R012EvolutionMaintenanceExecutor,
    journal_ref: dict[str, Any],
    *,
    status: str,
    value: dict[str, Any],
) -> dict[str, Any]:
    """Finish a journal and return a reference to its final immutable bytes.

    ``R012EvolutionMaintenanceExecutor._finish_journal`` updates the same
    JSON file that ``_manager_call`` initially hashed.  Returning a fresh
    reference here makes it impossible for a caller to retain that pre-call
    hash after recording a model decision.
    """
    journal_path = Path(str(journal_ref["path"]))
    if not journal_path.is_file():
        raise COnlyHarborDriverError(f"manager journal is missing before finalization: {journal_path}")
    executor._finish_journal(journal_path, status=status, value=value)
    finished = deepcopy(journal_ref)
    finished["path"] = str(journal_path)
    finished["sha256"] = sha256_file(journal_path)
    return finished


def _journal_evidence_ref(journal_ref: dict[str, Any]) -> dict[str, Any]:
    """Return only the immutable path/hash part embedded in D02 evidence."""
    return {
        "path": journal_ref.get("path"),
        "sha256": journal_ref.get("sha256"),
    }


def _reconciled_manager_call(
    *,
    executor: R012EvolutionMaintenanceExecutor,
    context: dict[str, Any],
    phase: str,
    purpose: str,
    messages: list[dict[str, str]],
    reconciliation: dict[str, Any],
    reconciliation_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], None]:
    """Reuse one audited paid response without touching the chat boundary.

    This is an explicit recovery boundary, separate from the normal
    ``_prepare_call`` path.  It accepts exactly the response and request
    audited by ``reconcile_r015_c_only_manager.py``; subsequent phases use the
    ordinary ManagerClient and therefore receive fresh call IDs.  The original
    journal remains untouched and the reused response receives a new journal
    in a dedicated reconciliation namespace.
    """
    manager = executor.manager
    if not isinstance(manager, ManagerClient):
        raise COnlyHarborDriverError("reconciled manager calls require the official ManagerClient")
    assignment = _object(context.get("assignment"), field="reconciled context.assignment")
    trial_id = _text(assignment.get("trial_id"), field="reconciled assignment.trial_id")
    task_id = _text(assignment.get("task_id"), field="reconciled assignment.task_id")
    session_id = _text(context.get("session_id"), field="reconciled context.session_id")
    trial = _object(reconciliation.get("trial"), field="reconciliation.trial")
    for field, expected in (
        ("round_id", assignment.get("round_id")),
        ("task_id", task_id),
        ("trial_id", trial_id),
        ("session_id", session_id),
    ):
        if trial.get(field) != expected:
            raise COnlyHarborDriverError(f"reconciled manager trial differs at {field}")
    if reconciliation.get("phase") != phase or reconciliation.get("purpose") != purpose:
        raise COnlyHarborDriverError("reconciled manager manifest differs from the requested phase")
    resume = _object(reconciliation.get("resume"), field="reconciliation.resume")
    if resume.get("harbor_rerun") is not False or resume.get("reuse_existing_call_id") is None:
        raise COnlyHarborDriverError("reconciled manager manifest does not prohibit Harbor/chat replay")
    if resume.get("new_journal_namespace") != "manager-journals-reconciliation":
        raise COnlyHarborDriverError("reconciled manager manifest has an unexpected journal namespace")
    original = _object(reconciliation.get("original"), field="reconciliation.original")
    reusable = _object(reconciliation.get("reusable_call"), field="reconciliation.reusable_call")
    call_id = _text(reusable.get("call_id"), field="reconciliation.reusable_call.call_id")
    if call_id != _text(original.get("call_id"), field="reconciliation.original.call_id"):
        raise COnlyHarborDriverError("reconciled manager original/reusable call IDs differ")
    request_ref = _object(reusable.get("request"), field="reconciliation.reusable_call.request")
    response_ref = _object(reusable.get("response"), field="reconciliation.reusable_call.response")
    request_path = Path(_text(request_ref.get("path"), field="reconciliation request.path"))
    response_path = Path(_text(response_ref.get("path"), field="reconciliation response.path"))
    if not request_path.is_file() or sha256_file(request_path) != request_ref.get("sha256"):
        raise COnlyHarborDriverError("reconciled manager request bytes changed")
    if not response_path.is_file() or sha256_file(response_path) != response_ref.get("sha256"):
        raise COnlyHarborDriverError("reconciled manager response bytes changed")
    original_request = _object(original.get("request"), field="reconciliation.original.request")
    original_response = _object(original.get("response"), field="reconciliation.original.response")
    if canonical_json(original_request) != canonical_json(request_ref) or canonical_json(original_response) != canonical_json(response_ref):
        raise COnlyHarborDriverError("reconciled manager original and reusable call references differ")
    for label, path in (("request", request_path), ("response", response_path)):
        try:
            path.resolve().relative_to(manager.run_dir.resolve())
        except ValueError as error:
            raise COnlyHarborDriverError(f"reconciled manager {label} is outside this run's manager directory") from error
    original_ledger_snapshot = _object(original.get("ledger_snapshot"), field="reconciliation.original.ledger_snapshot")
    if Path(_text(original_ledger_snapshot.get("path"), field="reconciliation.original.ledger_snapshot.path")).resolve() == manager.ledger_path.resolve():
        raise COnlyHarborDriverError("reconciled manager ledger snapshot must be separate from the active ledger")
    live_ledger = _object(original.get("live_ledger"), field="reconciliation.original.live_ledger")
    if Path(_text(live_ledger.get("path"), field="reconciliation.original.live_ledger.path")).resolve() != manager.ledger_path.resolve():
        raise COnlyHarborDriverError("reconciled manager ledger is not the active run ledger")
    if live_ledger.get("mutable") is not True:
        raise COnlyHarborDriverError("reconciled manager live ledger is not explicitly mutable")
    preserved_entry = _object(original.get("preserved_ledger_entry"), field="reconciliation.original.preserved_ledger_entry")
    preserved_entry_hash = _text(
        original.get("preserved_ledger_entry_sha256"),
        field="reconciliation.original.preserved_ledger_entry_sha256",
    )
    if _hash_json(preserved_entry) != preserved_entry_hash:
        raise COnlyHarborDriverError("reconciled manager preserved ledger entry hash changed")
    ledger = _read_json_file(manager.ledger_path, field="reconciled manager active ledger")
    ledger_calls = ledger.get("calls")
    if not isinstance(ledger_calls, list):
        raise COnlyHarborDriverError("reconciled manager active ledger has no calls list")
    matching_ledger = [item for item in ledger_calls if isinstance(item, dict) and item.get("call_id") == call_id]
    if (
        len(matching_ledger) != 1
        or canonical_json(matching_ledger[0]) != canonical_json(preserved_entry)
        or matching_ledger[0].get("purpose") != purpose
        or matching_ledger[0].get("status") != "tokenizer_mismatch"
        or matching_ledger[0].get("response_path") != str(response_path)
    ):
        raise COnlyHarborDriverError("reconciled manager active ledger no longer matches the preserved call")
    request_record = _read_json_file(request_path, field="reconciled manager request")
    response_record = _read_json_file(response_path, field="reconciled manager response")
    request_payload = _object(request_record.get("request"), field="reconciled manager request.request")
    saved_messages = request_payload.get("messages")
    if not isinstance(saved_messages, list) or _hash_json(saved_messages) != reusable.get("messages_sha256") or _hash_json(saved_messages) != _hash_json(messages):
        raise COnlyHarborDriverError("reconciled manager messages no longer match the audited phase")
    saved_options = {key: deepcopy(value) for key, value in request_payload.items() if key != "messages"}
    expected_options = _object(_object(reconciliation.get("request"), field="reconciliation.request").get("effective_request_options"), field="reconciliation.request.effective_request_options")
    if canonical_json(saved_options) != canonical_json(expected_options):
        raise COnlyHarborDriverError("reconciled manager effective request options changed")
    if request_record.get("purpose") != purpose or request_record.get("call_metadata", {}).get("task_id") != task_id:
        raise COnlyHarborDriverError("reconciled manager request identity changed")
    if response_record.get("http_status") != 200 or response_record.get("classification") != "tokenizer_prompt_count_mismatch":
        raise COnlyHarborDriverError("reconciled manager response is not the preserved HTTP200 tokenizer mismatch")
    parsed_response = _object(response_record.get("parsed_response"), field="reconciled manager response.parsed_response")
    choices = parsed_response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise COnlyHarborDriverError("reconciled manager response must contain exactly one choice")
    message = _object(_object(choices[0], field="reconciled manager response.choice").get("message"), field="reconciled manager response.message")
    content = message.get("content")
    if not isinstance(content, str):
        raise COnlyHarborDriverError("reconciled manager response has no textual JSON content")
    try:
        model_json = json.loads(content)
    except json.JSONDecodeError as error:
        raise COnlyHarborDriverError(f"reconciled manager response JSON is malformed: {error}") from error
    model_json = _object(model_json, field="reconciled manager response JSON")
    corrected = _object(reconciliation.get("corrected_preflight"), field="reconciliation.corrected_preflight")
    usage = _object(response_record.get("usage"), field="reconciled manager response.usage")
    observed = usage.get("prompt_tokens")
    if corrected.get("matches") is not True or corrected.get("estimated_input_tokens") != observed or corrected.get("chat_usage_prompt_tokens") != observed:
        raise COnlyHarborDriverError("reconciled manager corrected preflight does not match saved chat usage")
    expected_journal = Path(_text(resume.get("reconciliation_journal_path"), field="reconciliation.resume.reconciliation_journal_path"))
    run_dir = executor.journal_root.parent.parent
    expected_root = run_dir / "manager-journals-reconciliation"
    try:
        expected_journal.relative_to(expected_root)
    except ValueError as error:
        raise COnlyHarborDriverError("reconciled manager journal path escapes the run reconciliation namespace") from error
    if expected_journal != expected_root / task_id / sha256_text(trial_id)[:20] / f"{phase}.json":
        raise COnlyHarborDriverError("reconciled manager journal path is not the deterministic phase path")
    if expected_journal.exists():
        raise COnlyHarborDriverError(f"reconciled manager journal already exists; manual reconciliation required: {expected_journal}")
    manifest_ref = {"path": str(reconciliation_path), "sha256": sha256_file(reconciliation_path)}
    journal_value = {
        "schema_version": 1,
        "kind": "r012_reconciled_manager_call_journal",
        "trial_id": trial_id,
        "phase": phase,
        "purpose": purpose,
        "status": "reused_saved_manager_response",
        "created_at_utc": utc_now(),
        "original_call_id": call_id,
        "reconciliation_manifest": manifest_ref,
        "request": {"path": str(request_path), "sha256": request_ref.get("sha256")},
        "response": {"path": str(response_path), "sha256": response_ref.get("sha256")},
        "messages_sha256": _hash_json(messages),
        "corrected_preflight": deepcopy(corrected),
        "no_chat_completion_repeated": True,
    }
    write_json(expected_journal, journal_value)
    # Normal ManagerClient response references carry their call ID.  The audit
    # manifest's generic immutable file refs intentionally do not, so bind the
    # reused response explicitly before it enters event/extraction evidence.
    reusable_response_ref = deepcopy(response_ref)
    reusable_response_ref["call_id"] = call_id
    journal_ref = {"path": str(expected_journal), "sha256": sha256_file(expected_journal), "response": reusable_response_ref}
    return {
        "call_id": call_id,
        "response": parsed_response,
        "json": model_json,
        "preflight": deepcopy(corrected),
        "reconciled_from": manifest_ref,
    }, journal_ref, None


def _current_trace_ref(
    context: dict[str, Any],
    trace: dict[str, Any],
    trajectory_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind the current trace to its durable trajectory bytes.

    A fresh Harbor run creates ``trajectory-live.json`` immediately after
    importing the official packet.  A manager-only continuation must reuse
    the exact trajectory reference saved in the completed trial stage; it
    cannot require that the original importer run again or silently copy the
    trace to a new path.  Both paths are checked through the same immutable
    hash boundary.
    """
    path = (
        Path(str(trajectory_ref["path"]))
        if isinstance(trajectory_ref, dict) and trajectory_ref.get("path")
        else context["artifact_root"] / "trajectory-live.json"
    )
    ref = _ref(path)
    if not ref.get("exists"):
        raise COnlyHarborDriverError("current live trajectory reference is missing")
    if isinstance(trajectory_ref, dict):
        stated_hash = trajectory_ref.get("sha256")
        if not isinstance(stated_hash, str) or stated_hash != ref.get("sha256"):
            raise COnlyHarborDriverError("current live trajectory reference hash changed")
        expected_ref = {
            "round_id": int(str(context["trial_id"]).split(":", 1)[0].removeprefix("r")),
            "task_id": str(context["task_id"]),
            "trial_id": str(context["trial_id"]),
            "session_id": str(context["session_id"]),
        }
        for field, expected in expected_ref.items():
            if trajectory_ref.get(field) != expected:
                raise COnlyHarborDriverError(f"current live trajectory reference differs at {field}")
    source = _object(trace.get("source"), field="live trajectory.source")
    if canonical_instance_id(str(source.get("canonical_instance_id", ""))) != canonical_instance_id(str(context["task_id"])):
        raise COnlyHarborDriverError("current live trajectory source is not the frozen task")
    binding = trace.get("r015_binding")
    if not isinstance(binding, dict):
        raise COnlyHarborDriverError("current live trajectory has no C-only binding")
    expected_round = int(str(context["trial_id"]).split(":", 1)[0].removeprefix("r"))
    if binding.get("round_id") != expected_round or canonical_instance_id(str(binding.get("task_id", ""))) != canonical_instance_id(str(context["task_id"])) or binding.get("trial_id") != str(context["trial_id"]) or binding.get("session_id") != str(context["session_id"]):
        raise COnlyHarborDriverError("current live trajectory C-only binding differs from the frozen assignment")
    return {
        "round_id": expected_round,
        "task_id": str(context["task_id"]),
        "trial_id": str(context["trial_id"]),
        "session_id": str(context["session_id"]),
        "complete": True,
        "path": ref["path"],
        "sha256": ref["sha256"],
    }


def _pool_traces(context: dict[str, Any], current_trace: dict[str, Any], current_ref: dict[str, Any], input_value: dict[str, Any]) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    round_id = current_ref["round_id"]
    material = _object(input_value.get("round_material"), field="input.round_material")
    raw_pool = material.get("trajectory_pool", [])
    if not isinstance(raw_pool, list):
        raise COnlyHarborDriverError("round material trajectory_pool must be a list")
    result: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    seen: set[str] = set()

    for index, raw in enumerate(raw_pool):
        item = _object(raw, field=f"round_material.trajectory_pool[{index}]")
        task_id = canonical_instance_id(_text(item.get("task_id"), field="trajectory_pool.task_id"))
        if task_id in seen or task_id == context["task_id"]:
            raise COnlyHarborDriverError("round material has duplicate or current-task trajectory pool material")
        if item.get("round_id") != round_id or item.get("source") != "current_round_c_only":
            raise COnlyHarborDriverError("round material trajectory is outside the active C-only round")
        path = Path(_text(item.get("path"), field="trajectory_pool.path"))
        stated_hash = _text(item.get("sha256"), field="trajectory_pool.sha256")
        if not path.is_file() or sha256_file(path) != stated_hash:
            raise COnlyHarborDriverError("round material trajectory path/hash is not an immutable registered file")
        trace = _read_json_file(path, field="registered current-round trajectory")
        source = _object(trace.get("source"), field="registered trajectory.source")
        if canonical_instance_id(str(source.get("canonical_instance_id", ""))) != task_id:
            raise COnlyHarborDriverError("registered trajectory source differs from its pool task")
        binding = _object(trace.get("r015_binding"), field="registered trajectory.r015_binding")
        if binding.get("round_id") != round_id or binding.get("task_id") != task_id:
            raise COnlyHarborDriverError("registered trajectory binding differs from its current-round task")
        if binding.get("trial_id") != _text(item.get("trial_id"), field="trajectory_pool.trial_id"):
            raise COnlyHarborDriverError("registered trajectory binding differs from its pool trial")
        if binding.get("session_id") != _text(item.get("session_id"), field="trajectory_pool.session_id"):
            raise COnlyHarborDriverError("registered trajectory binding differs from its pool session")
        result.append(
            (
                task_id,
                trace,
                {
                    "round_id": round_id,
                    "task_id": task_id,
                    "trial_id": binding["trial_id"],
                    "session_id": binding["session_id"],
                    "complete": True,
                    "path": str(path),
                    "sha256": sha256_file(path),
                },
            )
        )
        seen.add(task_id)
    result.append((str(context["task_id"]), current_trace, current_ref))
    return result






def _add_provenance(skill: dict[str, Any], source_ids: list[str]) -> dict[str, Any]:
    candidate = validate_skill_candidate(skill)
    candidate["provenance"] = {
        "source_instance_ids": sorted({canonical_instance_id(item) for item in source_ids}),
        "source_instance_ids_raw": sorted(set(source_ids)),
        "parent_skill_ids": list(candidate.get("provenance", {}).get("parent_skill_ids", [])) if isinstance(candidate.get("provenance"), dict) else [],
    }
    return candidate


def _build_extraction(
    *,
    context: dict[str, Any],
    input_value: dict[str, Any],
    trace: dict[str, Any],
    current_ref: dict[str, Any],
    executor: R012EvolutionMaintenanceExecutor,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    traces = _pool_traces(context, trace, current_ref, input_value)
    event_service, event_runner = _event_graph_runtime(
        context=context, input_value=input_value, trace=trace,
        current_ref=current_ref, executor=executor,
    )
    event_initial = event_service.initial_state()
    event_state = event_runner.invoke(identity=event_initial["identity"], initial=event_initial)
    if event_state.get("stage") == "stopped":
        raise COnlyHarborDriverError(
            f"Event Graph stopped with a blocked stage: {event_state.get('outcomes')}"
        )
    graph_candidates = event_state.get("event_candidates", [])
    by_id = {item["candidate_id"]: item for item in graph_candidates}
    event_candidates = []
    published_fingerprints: set[str] = set()
    event_attempts = []
    for ordinal, result in enumerate(event_state.get("event_results", []), start=1):
        status = result["status"]
        candidate = by_id[result["candidate_id"]] if status == "generated" else None
        duplicate = candidate is not None and candidate["candidate_fingerprint"] in published_fingerprints
        if candidate is not None and not duplicate:
            published_fingerprints.add(candidate["candidate_fingerprint"])
            event_candidates.append(candidate)
        event_attempts.append({
            "attempt_no": ordinal,
            "outcome": "duplicate" if duplicate else
                       "generated" if status == "generated" else
                       "skip" if status == "skip" else "invalid",
            "candidate": deepcopy(candidate["skill"]) if candidate is not None else None,
            "raw_response": {"event_graph": deepcopy(result),
                             "publication_classification": "duplicate" if duplicate else status},
        })
    event_evidence = {"kind": "r015_event_graph_v3",
                      "thread_id": event_state["thread_id"],
                      "identity": event_state["identity"],
                      "directory": str(event_runner.directory),
                      "outcomes": event_state.get("outcomes", {}),
                      "event_results": deepcopy(event_state.get("event_results", [])),
                      "verifier_status": event_state["verifier_status"]}
    return _finish_extraction(
        context=context, input_value=input_value, trace=trace,
        current_ref=current_ref, executor=executor, traces=traces,
        event_attempts=event_attempts, event_candidates=event_candidates,
        event_evidence=event_evidence,
    )


def _finish_extraction(
    *, context: dict[str, Any], input_value: dict[str, Any], trace: dict[str, Any],
    current_ref: dict[str, Any], executor: R012EvolutionMaintenanceExecutor,
    traces: list[tuple[str, dict[str, Any], dict[str, Any]]],
    event_attempts: list[dict[str, Any]], event_candidates: list[dict[str, Any]],
    event_evidence: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    service, runner = _task_graph_runtime(
        context=context, input_value=input_value, trace=trace,
        current_ref=current_ref, traces=traces, executor=executor,
    )
    initial = service.initial_state()
    task_state = runner.invoke(identity=initial["identity"], initial=initial)
    if task_state.get("stage") == "stopped":
        raise COnlyHarborDriverError(
            f"Task Graph stopped with a blocked stage: {task_state.get('outcomes')}"
        )
    task_candidates = [task_state["merge_candidate"]] if task_state.get("merge_candidate") else []
    descriptions = task_state.get("description_records", [])
    task_candidate_records = task_state.get("task_candidate_records", [])
    task_graph_record = {
        "kind": "r015_task_graph_v1", "thread_id": task_state["thread_id"],
        "identity": task_state["identity"], "directory": str(runner.directory),
        "frozen_bank_state_sha256": task_state["frozen_bank_hash"],
        "verifier_status": task_state["verifier_status"],
        "outcomes": task_state.get("outcomes", {}),
    }
    candidates = event_candidates + task_candidates
    extraction = {
        "condition": "C-only",
        "round_id": current_ref["round_id"],
        "task_id": context["task_id"],
        "decision": "extract" if candidates else "skip",
        "reason": None if candidates else "no valid event/task candidate survived current-round evidence validation",
        "candidates": candidates,
        "trajectory_ref": current_ref,
        "description_records": descriptions,
        "task_candidate_records": task_candidate_records,
        "evidence": {
            "kind": "r015_c_only_extraction_evidence",
            "source_trajectory": current_ref,
            "event": event_evidence,
            "description": task_state.get("outcomes", {}).get("d01", {}),
            "task_candidate": task_state.get("outcomes", {}).get("generate", {}),
            "task": {"status": task_state.get("outcomes", {}).get("merge", {}).get("status", "no_merge"),
                     "group_id": task_state.get("pairing", {}).get("group_id") if isinstance(task_state.get("pairing"), dict) else None,
                     "graph": task_graph_record},
            "current_round_source_task_ids": [task_id for task_id, _trace, _ref_value in traces],
            "historical_baseline_used": False,
        },
    }
    return extraction, event_attempts


def _task_graph_runtime(
    *, context: dict[str, Any], input_value: dict[str, Any], trace: dict[str, Any],
    current_ref: dict[str, Any], traces: list[tuple[str, dict[str, Any], dict[str, Any]]],
    executor: R012EvolutionMaintenanceExecutor,
) -> tuple[TaskGraphStages, TaskGraphRunner]:
    manager = executor.manager
    if not isinstance(manager, ManagerClient):
        raise COnlyHarborDriverError("Task Graph needs the configured manager's model/tokenizer identity")
    frozen_bank = SkillBank.from_dict(
        _object(_object(context.get("assignment"), field="assignment").get("frozen_bank"), field="assignment.frozen_bank")
    )
    chat = TaskChatBoundary(
        root=Path(manager.run_dir), base_url=manager.profile.base_url,
        model=manager.profile.model, token_counter=manager.exact_token_counter,
        context_tokens=524288,
        output_tokens=65536, safety_tokens=4096,
    )
    artifact_root = Path(context["artifact_root"])
    service = TaskGraphStages(
        context=context, input_value=input_value, trace=trace,
        current_ref=current_ref, traces=traces, frozen_bank=frozen_bank,
        chat=chat, artifact_root=artifact_root, prompt_root=ROOT / "prompts",
        encoder=executor.encoder,
        tokenizer_identity=_tokenizer_request_identity(manager.exact_token_counter),
        require_durable_extraction=os.environ.get("CODESKILL_EXTRACT_HANDOFF") == "1",
    )
    return service, TaskGraphRunner(
        directory=artifact_root / "task-graph", service=service,
        store_path=Path(input_value["state"]["path"]).parent / "task-graph-store.sqlite",
    )


def _event_graph_runtime(
    *, context: dict[str, Any], input_value: dict[str, Any],
    trace: dict[str, Any], current_ref: dict[str, Any],
    executor: R012EvolutionMaintenanceExecutor,
) -> tuple[EventGraphStages, EventGraphRunner]:
    manager = executor.manager
    if not isinstance(manager, ManagerClient):
        raise COnlyHarborDriverError("Event Graph needs the configured manager model/tokenizer identity")
    frozen_bank = SkillBank.from_dict(_object(
        _object(context.get("assignment"), field="assignment").get("frozen_bank"),
        field="assignment.frozen_bank"))
    chat = TaskChatBoundary(
        root=Path(manager.run_dir), base_url=manager.profile.base_url,
        model=manager.profile.model, token_counter=manager.exact_token_counter,
        context_tokens=524288,
        output_tokens=65536, safety_tokens=4096,
    )
    artifact_root = Path(context["artifact_root"])
    service = EventGraphStages(
        context=context, input_value=input_value,
        trace=trace, current_ref=current_ref,
        traces=[(str(context["task_id"]), trace, current_ref)],
        frozen_bank=frozen_bank, chat=chat, artifact_root=artifact_root,
        prompt_root=ROOT / "prompts", encoder=executor.encoder,
        tokenizer_identity=_tokenizer_request_identity(manager.exact_token_counter),
    )
    return service, EventGraphRunner(
        directory=artifact_root / "event-graph", service=service,
        store_path=Path(input_value["state"]["path"]).parent / "task-graph-store.sqlite",
    )


def _fig9_operation(
    *,
    context: dict[str, Any],
    executor: R012EvolutionMaintenanceExecutor,
    bank: SkillBank,
    candidate: dict[str, Any],
    source_instance_ids: list[str],
    operation_id: str,
    phase: str,
    purpose_prefix: str,
    extra_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    original_candidate = deepcopy(candidate)
    original_fingerprint = _hash_json(original_candidate)
    encoder = _ensure_encoder(context, executor)
    retrieved, retrieval = same_granularity_top5(bank, candidate, encoder)
    prompt_candidate = {
        key: candidate[key]
        for key in ("title", "granularity", "when_to_apply", "rules", "benchmark", "code_examples")
        if key in candidate
    }
    messages = maintenance_from_skills_messages(
        prompt_candidate,
        retrieved,
        paper_prompt=_prompt_path("custom/r015_fig09_maintenance_preserve_code_examples.md").read_text(encoding="utf-8"),
    )
    call, journal, invalid = _manager_call(
        executor,
        trial_id=str(context["trial_id"]),
        phase=phase,
        purpose=f"{purpose_prefix}:{context['trial_id']}:{operation_id}",
        messages=messages,
        metadata={"condition": "C-only", "task_id": context["task_id"], "operation_id": operation_id, "retrieval": retrieval},
    )
    if call is None:
        # A Fig.9 choice is mandatory for an extracted/evolved candidate.  An
        # invalid model response is retained by ManagerClient but cannot be
        # safely converted into add/merge/drop, so stop the current task.
        _finish_manager_journal(
            executor,
            journal,
            status="fig9_output_rejected",
            value={"error": invalid},
        )
        raise COnlyHarborDriverError(
            f"Fig.9 manager output was invalid for {operation_id}; raw response is preserved: {invalid}"
        )
    response_ref = journal.get("response")
    try:
        checked = validate_maintenance_from_skills(
            call.get("json"),
            candidate=candidate,
            retrieved_skill_ids={str(item["skill_id"]) for item in retrieved},
            retrieved_skills=retrieved,
        )
    except (ValueError, TypeError) as error:
        _finish_manager_journal(executor, journal, status="fig9_output_rejected", value={"call_id": call.get("call_id"), "error_type": type(error).__name__, "error": str(error), "model_output": deepcopy(call.get("json"))})
        raise COnlyHarborDriverError(f"Fig.9 output failed its validator for {operation_id}: {error}") from error
    candidate_for_operation = deepcopy(checked.get("skill", candidate))
    candidate_for_operation["provenance"] = deepcopy(candidate.get("provenance", {}))
    merge_target_id = checked.get("merge_target_skill_id")
    merge_target = None
    if checked.get("action") == "merge":
        if not isinstance(merge_target_id, str) or not merge_target_id:
            raise COnlyHarborDriverError(f"Fig.9 merge for {operation_id} has no validated target")
        try:
            merge_target = deepcopy(bank._find_active(merge_target_id))
        except BankError as error:
            raise COnlyHarborDriverError(f"Fig.9 merge target disappeared for {operation_id}: {merge_target_id}") from error
        candidate_provenance = _object(candidate_for_operation.get("provenance", {}), field="Fig.9 candidate.provenance")
        target_provenance = _object(merge_target.get("provenance", {}), field="Fig.9 merge target.provenance")
        source_ids = sorted(
            {
                canonical_instance_id(str(item))
                for item in [
                    *candidate_provenance.get("source_instance_ids", []),
                    *target_provenance.get("source_instance_ids", []),
                    *source_instance_ids,
                ]
            }
        )
        raw_source_ids = sorted(
            {
                str(item)
                for item in [
                    *candidate_provenance.get("source_instance_ids_raw", candidate_provenance.get("source_instance_ids", [])),
                    *target_provenance.get("source_instance_ids_raw", target_provenance.get("source_instance_ids", [])),
                    *source_instance_ids,
                ]
            }
        )
        parent_ids = sorted(
            {
                str(item)
                for item in [
                    *candidate_provenance.get("parent_skill_ids", []),
                    *target_provenance.get("parent_skill_ids", []),
                    str(merge_target["skill_id"]),
                ]
            }
        )
        candidate_for_operation["provenance"] = {
            "source_instance_ids": source_ids,
            "source_instance_ids_raw": raw_source_ids,
            "parent_skill_ids": parent_ids,
        }
    final_fingerprint = _hash_json(candidate_for_operation)
    evidence = {
        "kind": "r015_c_only_fig9_manager_evidence",
        "retrieval": retrieval,
        "manager_call_id": call.get("call_id"),
        "decision_reason": checked.get("reason"),
        "decision_references": deepcopy(checked["evidence"]),
        **(deepcopy(extra_evidence) if isinstance(extra_evidence, dict) else {}),
        # These fields are recomputed from the validated response and the
        # exact objects entering/leaving Fig.9.  Keep them after optional
        # context so a caller cannot override an invariant through
        # ``extra_evidence``.
        "manager_response_sha256": response_ref["sha256"],
        "manager_response_path": response_ref["path"],
        "fig9_response_sha256": response_ref["sha256"],
        "fig9_response_path": response_ref["path"],
        "original_candidate_fingerprint": original_fingerprint,
        "merged_candidate_fingerprint": final_fingerprint,
        "merge_target_skill_fingerprint": _hash_json(merge_target) if merge_target is not None else None,
        "merge_target_skill": merge_target,
    }
    if isinstance(extra_evidence, dict) and "fig8_manager_response_sha256" in extra_evidence:
        evidence["ancestor_union"] = deepcopy(candidate_for_operation["provenance"])
    try:
        applied = bank.apply(
            operation_id=operation_id,
            decision=str(checked["action"]),
            candidate=candidate_for_operation,
            source_instance_ids=source_instance_ids,
            merge_target_id=merge_target_id,
            evidence=evidence,
        )
    except (BankError, ValueError) as error:
        _finish_manager_journal(executor, journal, status="fig9_bank_operation_rejected", value={"call_id": call.get("call_id"), "error_type": type(error).__name__, "error": str(error), "validated": checked})
        raise COnlyHarborDriverError(f"Fig.9 bank operation was rejected for {operation_id}: {error}") from error
    _finish_manager_journal(executor, journal, status="fig9_applied_to_driver_staged_bank", value={"call_id": call.get("call_id"), "validated": checked, "operation": applied})
    return {
        "operation_id": operation_id,
        "original_candidate": original_candidate,
        "original_candidate_fingerprint": original_fingerprint,
        "candidate": candidate_for_operation,
        "decision": checked["action"],
        "merge_target_id": checked.get("merge_target_skill_id"),
        "source_instance_ids": list(source_instance_ids),
        "evidence": evidence,
        "manager_response": response_ref,
        "applied_preview": applied,
    }


def _supplied_maintenance(
    *,
    context: dict[str, Any],
    executor: R012EvolutionMaintenanceExecutor,
    bank: SkillBank,
    trajectory: dict[str, Any],
    attempts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Perform one Fig.8 and, when evolved, one Fig.9 per supplied skill."""
    trial_id = str(context["trial_id"])
    supplied = supplied_skills_for_evolution(attempts, trial_id=trial_id)
    if not supplied:
        return [], []
    decisions: list[dict[str, Any]] = []
    operations: list[dict[str, Any]] = []
    staged = bank
    for ordinal, supplied_item in enumerate(supplied, start=1):
        skill = _object(supplied_item.get("skill"), field="supplied skill")
        skill_id = _text(skill.get("skill_id"), field="supplied skill.skill_id")
        version = skill.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise COnlyHarborDriverError("supplied skill version is invalid")
        evolution_call, evolution_journal, invalid = _manager_call(
            executor,
            trial_id=trial_id,
            phase=f"evolution-{ordinal:03d}",
            purpose=f"r015_c_only_fig8:{trial_id}:{skill_id}:{version}",
            trajectory_context=context, source_traces=[trajectory],
            messages_builder=lambda values: evolution_messages(
                supplied=[supplied_item], trajectory_evidence=values[0],
                paper_prompt=_prompt_path("custom/r015_fig08_evolution_preserve_code_examples.md").read_text(encoding="utf-8"),
            ),
            messages=evolution_messages(
                supplied=[supplied_item],
                trajectory_evidence=trajectory,
                paper_prompt=_prompt_path("custom/r015_fig08_evolution_preserve_code_examples.md").read_text(encoding="utf-8"),
            ),
            metadata={"condition": "C-only", "task_id": context["task_id"], "supplied_skill_id": skill_id, "supplied_skill_version": version},
        )
        if evolution_call is None:
            _finish_manager_journal(
                executor,
                evolution_journal,
                status="fig8_output_rejected",
                value={"error": invalid},
            )
            raise COnlyHarborDriverError(f"Fig.8 manager output was invalid for supplied {skill_id}:{version}; raw response is preserved: {invalid}")
        evolution_ref = evolution_journal.get("response")
        try:
            evolved = validate_evolution_output(
                evolution_call.get("json"),
                supplied=[supplied_item],
                trajectory_evidence=trajectory,
                visible_step_ids_by_source=evolution_call.get("visible_step_ids_by_source"),
            )
        except (R012ExecutionError, ValueError, TypeError) as error:
            _finish_manager_journal(executor, evolution_journal, status="fig8_output_rejected", value={"call_id": evolution_call.get("call_id"), "error_type": type(error).__name__, "error": str(error), "model_output": deepcopy(evolution_call.get("json"))})
            raise COnlyHarborDriverError(f"Fig.8 output failed its validator for supplied {skill_id}:{version}: {error}") from error
        decision = {
            "skill_id": skill_id,
            "version": version,
            "action": evolved["action"],
            "reason": evolved["reason"],
            "manager_response_sha256": evolution_ref["sha256"],
            "manager_response_path": evolution_ref["path"],
            "manager_call_id": evolution_call.get("call_id"),
            "fig8_response_sha256": evolution_ref["sha256"],
            "historical_thinking_policy": _historical_thinking_policy_identity(context),
        }
        decisions.append(decision)
        if evolved["action"] == "skip":
            _finish_manager_journal(executor, evolution_journal, status="fig8_skip", value={"call_id": evolution_call.get("call_id"), "validated": evolved})
            continue
        base = _object(evolved.get("base"), field="Fig.8 base")
        base_skill = _object(base.get("skill"), field="Fig.8 base.skill")
        candidate = deepcopy(evolved["skill"])
        base_provenance = base_skill.get("provenance") if isinstance(base_skill.get("provenance"), dict) else {}
        base_sources = base_provenance.get("source_instance_ids", []) if isinstance(base_provenance.get("source_instance_ids", []), list) else []
        base_raw_sources = base_provenance.get("source_instance_ids_raw", base_sources) if isinstance(base_provenance.get("source_instance_ids_raw", base_sources), list) else base_sources
        base_parents = base_provenance.get("parent_skill_ids", []) if isinstance(base_provenance.get("parent_skill_ids", []), list) else []
        candidate["provenance"] = {
            "source_instance_ids": sorted({canonical_instance_id(str(item)) for item in [*base_sources, str(context["task_id"])]}),
            "source_instance_ids_raw": sorted(set(str(item) for item in [*base_raw_sources, str(context["task_id"])])),
            "parent_skill_ids": sorted(set(str(item) for item in [*base_parents, str(base_skill["skill_id"])])),
        }
        _finish_manager_journal(executor, evolution_journal, status="fig8_evolve_validated", value={"call_id": evolution_call.get("call_id"), "validated": evolved, "candidate": candidate})
        operation_id = "r015-c-only-maintenance-" + sha256_text(canonical_json({"trial_id": trial_id, "skill_id": skill_id, "version": version, "evolution_call_id": evolution_call.get("call_id")}))[:20]
        operation = _fig9_operation(
            context=context,
            executor=executor,
            bank=staged,
            candidate=candidate,
            source_instance_ids=[str(context["task_id"])],
            operation_id=operation_id,
            phase=f"fig9-maintenance-{ordinal:03d}",
            purpose_prefix="r015_c_only_fig9_after_fig8",
            extra_evidence={
                "fig8_manager_response_sha256": evolution_ref["sha256"],
                "fig8_manager_response_path": evolution_ref["path"],
                "fig8_call_id": evolution_call.get("call_id"),
                "fig8_candidate_fingerprint": _hash_json(candidate),
                "fig8_candidate_provenance": deepcopy(candidate["provenance"]),
                "supplied_skill_id": skill_id,
                "supplied_skill_version": version,
                "ancestor_skill_id": str(base_skill["skill_id"]),
                "injection_evidence": deepcopy(supplied_item.get("injection_evidence", [])),
            },
        )
        operation["evidence"]["ancestor_union"] = deepcopy(operation["candidate"]["provenance"])
        operation.update({"source_kind": "maintenance", "supplied_skill_id": skill_id, "supplied_skill_version": version})
        operations.append(operation)
    return decisions, operations


def _extraction_publication(
    *,
    context: dict[str, Any],
    executor: R012EvolutionMaintenanceExecutor,
    frozen_bank: SkillBank,
    extraction: dict[str, Any],
    supplied_attempts: list[dict[str, Any]],
    trace: dict[str, Any],
    input_value: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run Fig.9 once for every extracted candidate, then C-supplied Fig.8/9."""
    staged = SkillBank.from_dict(frozen_bank.to_dict())
    operations: list[dict[str, Any]] = []
    candidates = extraction.get("candidates", [])
    if not isinstance(candidates, list):
        raise COnlyHarborDriverError("extraction candidates must be a list")
    graph_record = extraction.get("evidence", {}).get("task", {}).get("graph")
    if graph_record is not None and (not isinstance(graph_record, dict) or graph_record.get("kind") != "r015_task_graph_v1"):
        raise COnlyHarborDriverError("Task Graph extraction marker is invalid")
    if input_value is not None and graph_record is None:
        raise COnlyHarborDriverError("official Task publication requires a persisted Task Graph receipt")
    if graph_record is not None and input_value is None:
        raise COnlyHarborDriverError("Task Graph publication needs the immutable driver input")
    event_candidates = [item for item in candidates if item.get("skill", {}).get("granularity") == "event"] if graph_record else candidates
    event_graph_record = extraction.get("evidence", {}).get("event")
    event_service = None
    event_runner = None
    event_state = None
    if isinstance(event_graph_record, dict) and event_graph_record.get("kind") == "r015_event_graph_v3":
        if input_value is None:
            raise COnlyHarborDriverError("Event Graph publication needs the immutable driver input")
        event_service, event_runner = _event_graph_runtime(
            context=context, input_value=input_value, trace=trace,
            current_ref=_object(extraction.get("trajectory_ref"), field="extraction.trajectory_ref"),
            executor=executor,
        )
        initial = event_service.initial_state()
        event_state = event_runner.invoke(identity=initial["identity"], initial=initial)
        if event_state.get("thread_id") != event_graph_record.get("thread_id") or \
                str(event_runner.directory) != event_graph_record.get("directory"):
            raise COnlyHarborDriverError("Event Graph publication source/method differs")
    for ordinal, raw in enumerate(event_candidates, start=1):
        item = _object(raw, field=f"extraction.candidates[{ordinal - 1}]")
        candidate = _object(item.get("skill"), field=f"extraction.candidates[{ordinal - 1}].skill")
        candidate_id = _text(item.get("candidate_id"), field=f"extraction.candidates[{ordinal - 1}].candidate_id")
        field = f"extraction.candidates[{ordinal - 1}]"
        expected_policy = _historical_thinking_policy_identity(context)
        raw_derivation = item.get("raw")
        if isinstance(raw_derivation, dict) and raw_derivation.get("kind") == "event_graph_candidate_v3":
            if event_service is None or event_state is None:
                raise COnlyHarborDriverError("Event Graph candidate has no matching completed graph")
            event_runner.verify_candidate(item, event_state)
        elif not isinstance(raw_derivation, dict):
            if expected_policy["policy"] != "keep":
                raise COnlyHarborDriverError(
                    f"{field} has no request-bound derivation provenance for exclude"
                )
        else:
            response_ref = _object(raw_derivation.get("response"), field=f"{field}.raw.response")
            manager_call_id = _text(raw_derivation.get("manager_call_id"), field=f"{field}.raw.manager_call_id")
            if candidate.get("granularity") == "event":
                expected_traces = [trace]
                journal_statuses = {"event_generated"}
            else:
                pairing = _object(item.get("pairing"), field=f"{field}.pairing")
                trajectory_refs = pairing.get("trajectory_refs")
                if not isinstance(trajectory_refs, list) or not trajectory_refs:
                    raise COnlyHarborDriverError(f"{field}.pairing has no trajectory references")
                expected_traces = [
                    _read_immutable_json_ref(
                        _object(reference, field=f"{field}.pairing.trajectory_refs[{index}]"),
                        field=f"{field}.pairing.trajectory_refs[{index}]",
                    )[0]
                    for index, reference in enumerate(trajectory_refs)
                ]
                journal_statuses = {"task_extraction_generated"}
            binding = _validate_manager_derivation_binding(
                raw_derivation.get("derivation"),
                field=f"{field}.raw.derivation",
                wrapper_policy=raw_derivation.get("historical_thinking_policy"),
                expected_policy=expected_policy,
                expected_traces=expected_traces,
                response_ref=response_ref,
                expected_call_id=manager_call_id,
                allowed_journal_statuses=journal_statuses,
            )
            if binding.get("binding_kind") != "legacy_keep_response_binding":
                model_output = _object(raw_derivation.get("model_output"), field=f"{field}.raw.model_output")
                persisted_model_output = _model_output_from_response_ref(
                    response_ref,
                    field=f"{field}.raw.response",
                    expected_call_id=manager_call_id,
                )
                if canonical_json(model_output) != canonical_json(persisted_model_output):
                    raise COnlyHarborDriverError(
                        f"{field}.raw.model_output differs from its saved manager response"
                    )
        provenance = _object(candidate.get("provenance"), field=f"extraction.candidates[{ordinal - 1}].skill.provenance")
        provenance_sources = provenance.get("source_instance_ids")
        if not isinstance(provenance_sources, list) or not provenance_sources:
            raise COnlyHarborDriverError("extraction candidate has no source provenance")
        source_instance_ids = [canonical_instance_id(str(source)) for source in provenance_sources]
        if candidate.get("granularity") == "event":
            source_instance_ids = [str(context["task_id"])]
        operation_id = "r015-c-only-extraction-" + sha256_text(canonical_json({"trial_id": context["trial_id"], "candidate_id": candidate_id, "ordinal": ordinal}))[:20]
        operation = _fig9_operation(
            context=context,
            executor=executor,
            bank=staged,
            candidate=candidate,
            source_instance_ids=source_instance_ids,
            operation_id=operation_id,
            phase=f"fig9-extraction-{ordinal:03d}",
            purpose_prefix="r015_c_only_fig9_after_extraction",
            extra_evidence={
                "extraction_candidate_id": candidate_id,
                "extraction_candidate_fingerprint": _hash_json(candidate),
                "extraction_source": deepcopy(item.get("raw")),
            },
        )
        operation.update({"source_kind": "extraction", "candidate_id": candidate_id})
        operations.append(operation)
    if graph_record is not None:
        current_ref = _object(extraction.get("trajectory_ref"), field="extraction.trajectory_ref")
        service, runner = _task_graph_runtime(
            context=context, input_value=input_value, trace=trace,
            current_ref=current_ref,
            traces=_pool_traces(context, trace, current_ref, input_value),
            executor=executor,
        )
        initial = service.initial_state()
        if initial["thread_id"] != graph_record.get("thread_id") or str(runner.directory) != graph_record.get("directory"):
            raise COnlyHarborDriverError("Task Graph handoff differs from extraction identity")
        event_receipt = {
            "thread_id": initial["thread_id"],
            "H0": frozen_bank.snapshot()["state_sha256"],
            "HE": staged.snapshot()["state_sha256"],
            "event_operations": deepcopy(operations),
        }
        if os.environ.get("CODESKILL_EXTRACT_HANDOFF") == "1":
            ack_path = Path(context["artifact_root"]).parent / "extraction-ack.json"
            state_path = Path(input_value["state"]["path"])
            event_receipt["extraction_ref"] = {
                "durable_state_path": str(state_path),
                "durable_state_sha256": sha256_file(state_path),
                "ack_path": str(ack_path), "ack_sha256": sha256_file(ack_path),
            }
        task_state = runner.invoke(identity=initial["identity"], receipt=event_receipt)
        if task_state.get("stage") == "stopped":
            raise COnlyHarborDriverError(f"Task Graph Fig.9 stopped: {task_state.get('outcomes')}")
        task_receipt = _object(task_state.get("task_receipt"), field="Task Graph staged receipt")
        task_operations = task_receipt.get("task_operations")
        if not isinstance(task_operations, list):
            raise COnlyHarborDriverError("Task Graph has no ordered Task operation list")
        staged = SkillBank.from_dict(service._read(task_receipt["task_bank"]))
        if staged.snapshot()["state_sha256"] != task_receipt.get("HT"):
            raise COnlyHarborDriverError("Task Graph staged bank hash differs")
        operations.extend(task_operations)
    trace_evidence = {
        "proxy_attempt_records": supplied_attempts,
        "trajectory_evidence": trace,
        "classification": "completed",
    }
    try:
        decisions, maintenance_operations = _supplied_maintenance(
            context=context,
            executor=executor,
            bank=staged,
            trajectory=trace,
            attempts=supplied_attempts,
        )
    except (R012ExecutionError, ValueError, TypeError) as error:
        raise COnlyHarborDriverError(f"C-only Fig.8/Fig.9 supplied-skill maintenance failed: {error}") from error
    operations.extend(maintenance_operations)
    # Keep this check at the driver boundary as well as in the coordinator:
    # every actual supplied identity has one and only one Fig.8 decision, and
    # every evolve decision has the corresponding Fig.9 operation.
    supplied = supplied_skills_for_evolution(supplied_attempts, trial_id=str(context["trial_id"]))
    decision_ids = {(item["skill_id"], item["version"]) for item in decisions}
    supplied_ids = {(item["skill"]["skill_id"], item["skill"]["version"]) for item in supplied}
    if decision_ids != supplied_ids:
        raise COnlyHarborDriverError("driver Fig.8 decision identities do not cover exactly the supplied skills")
    evolve_ids = {(item["skill_id"], item["version"]) for item in decisions if item.get("action") == "evolve"}
    operation_ids = {(item.get("supplied_skill_id"), item.get("supplied_skill_version")) for item in operations if item.get("source_kind") == "maintenance"}
    if operation_ids != evolve_ids:
        raise COnlyHarborDriverError("driver Fig.8 evolve/Fig.9 operation identities are not one-to-one")
    return decisions, operations


def _run_trial(input_value: dict[str, Any], input_path: Path, output_path: Path) -> dict[str, Any]:
    if output_path.exists():
        raise COnlyHarborDriverError(f"driver output already exists; durable outer runner must reuse it: {output_path}")
    paths, metadata, task_path = _task_root_and_service(input_value)
    context = _build_trial_context(input_value, output_path=output_path, paths=paths, metadata=metadata)
    context["input_path"] = input_path
    context["assignment"] = deepcopy(input_value["assignment"])
    context["driver_config"] = _object(input_value["config"].get("driver", {}), field="input.config.driver")
    launch_intent = {
        "schema_version": 1,
        "kind": "r015_c_only_official_harbor_launch_intent",
        "created_at_utc": utc_now(),
        "condition": "C-only",
        "round_id": int(input_value["assignment"]["round_id"]),
        "task_id": str(input_value["assignment"]["task_id"]),
        "trial_id": str(input_value["assignment"]["trial_id"]),
        "session_id": context["session_id"],
        "frozen_bank_state_sha256": str(input_value["assignment"]["frozen_bank_state_sha256"]),
        "state": deepcopy(input_value["state"]),
        "input": {"path": str(input_path), "sha256": sha256_file(input_path)},
        "task": deepcopy(metadata),
        "public_adapter": {
            "import_path": "codeskill_rebuild.harbor_openclaw_adapter:CODESKILLHarborOpenClaw",
            "harbor_version": str(context["target"].get("harbor_version", "")),
            "openclaw_package": f"openclaw@{context['target'].get('openclaw_version', '')}",
            "source_mount": "forbidden",
        },
        "sidecar_config": _ref(context["sidecar_config"]),
        "openclaw_config": _ref(context["host_config"]),
        "job_config": _ref(context["job_config"]),
        "native_database_seed": _ref(context["database"]),
        "baseline_used": False,
    }
    write_json(context["artifact_root"] / "launch-intent.json", launch_intent)
    _sidecar_check(context)
    _harbor_config_check(context)
    process = _run_official_harbor(context)
    imported = _import_official_evidence(context, process)
    return _finish_imported_trial(
        input_value, input_path, output_path, context, process, metadata, imported
    )


def _finish_imported_trial(
    input_value: dict[str, Any],
    input_path: Path,
    output_path: Path,
    context: dict[str, Any],
    process: dict[str, Any],
    metadata: dict[str, Any],
    imported: dict[str, Any],
    *,
    recovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply the same learning and publication rules to live and recovered trials."""
    assignment = input_value["assignment"]
    raw_evidence = imported["raw_evidence"]
    trace = imported.get("trace")
    attempts = imported.get("proxy_attempt_records", [])
    recovery_evidence = (
        {"recovery_binding": deepcopy(context["recovery_binding"])}
        if recovery is not None else {}
    )
    raw_evidence.update(recovery_evidence)
    supplied_records = supplied_skills_for_evolution(attempts, trial_id=str(context["trial_id"])) if attempts else []
    trial = {
        "condition": "C-only",
        "round_id": int(assignment["round_id"]),
        "task_id": str(assignment["task_id"]),
        "trial_id": str(assignment["trial_id"]),
        "outcome": imported["outcome"],
        "trajectory": imported.get("trajectory"),
        "supplied_skills": [
            {
                "skill_id": item["skill"]["skill_id"],
                "version": item["skill"]["version"],
                "source": "current_round",
                "round_id": int(assignment["round_id"]),
                "injection_evidence": deepcopy(item.get("injection_evidence", [])),
            }
            for item in supplied_records
        ],
        "raw_evidence": raw_evidence,
    }
    phase_base = {
        "trial": trial,
        "official_process": process,
        "task_metadata": metadata,
        **recovery_evidence,
    }

    def save_phase(phase: str, **payload: Any) -> Path:
        return _write_phase_stage(
            input_path=input_path,
            stage_path=recovery["stage_paths"][phase] if recovery is not None else None,
            context=context,
            phase=phase,
            payload={**phase_base, **payload},
        )

    # Persist the trace before any manager call; neither path can replay Harbor.
    save_phase("trial", trace=deepcopy(trace), proxy_attempt_records=deepcopy(attempts))
    learnable = trial["outcome"] == "completed" and isinstance(trace, dict) and trial["trajectory"] is not None
    identity = {key: trial[key] for key in ("condition", "round_id", "task_id")}
    if learnable:
        manager, executor, manager_context = _manager_context(context, output_path=output_path)
        current_ref = _current_trace_ref(
            context, trace, trajectory_ref=_object(trial["trajectory"], field="trial.trajectory")
        )
        extraction, event_attempts = _build_extraction(
            context=context,
            input_value=input_value,
            trace=trace,
            current_ref=current_ref,
            executor=executor,
        )
    else:
        event_attempts = []
        extraction = {
            **identity,
            "decision": "skip",
            "reason": "official Harbor trial did not produce a complete importable trajectory",
            "candidates": [],
            "trajectory_ref": None,
            "description_records": [],
            "evidence": {"kind": "r015_c_only_infra_trial_extraction", "official_trial": True, "raw_evidence": raw_evidence},
        }
    extraction_stage = save_phase("extraction", event_attempts=event_attempts, extraction=extraction)
    _wait_extraction_ack(
        input_value=input_value, input_path=input_path, output_path=output_path,
        assignment=assignment, extraction_stage=extraction_stage,
        extraction=extraction,
    )
    if learnable:
        frozen_bank = SkillBank.from_dict(_object(assignment.get("frozen_bank"), field="assignment.frozen_bank"))
        manager_decisions, operations = _extraction_publication(
            context=context,
            executor=executor,
            frozen_bank=frozen_bank,
            extraction=extraction,
            supplied_attempts=attempts,
            trace=trace,
            input_value=input_value,
        )
        publication = {
            **identity,
            "operations": operations,
            "manager_decisions": manager_decisions,
            "evidence": {
                "kind": "r015_c_only_publication_evidence",
                "manager_ledger": _snapshot_manager_ledger(context, manager_context),
                "manager_calls_before_trial": manager_context["manager_ledger_before_calls"],
                "historical_baseline_used": False,
                **recovery_evidence,
            },
        }
    else:
        publication = {
            **identity,
            "operations": [],
            "manager_decisions": [],
            "evidence": {"kind": "r015_c_only_infra_trial_publication", "official_trial": True, "raw_evidence": raw_evidence},
        }
    save_phase("publication", event_attempts=event_attempts, extraction=extraction, publication=publication)
    output = {
        "schema_version": 1,
        "kind": "r015_c_only_trial_driver_output",
        **identity,
        **phase_base,
        "trace": deepcopy(trace) if learnable else None,
        "proxy_attempt_records": deepcopy(attempts) if learnable else [],
        "event_attempts": event_attempts,
        "extraction": extraction,
        "publication": publication,
        "historical_baseline_used": False,
        "evidence_mode": "official_live",
    }
    if recovery is not None:
        output["recovery_completion"] = _write_recovery_completion(
            recovery, input_path=input_path, output_path=output_path
        )
    write_json(output_path, output)
    return output


def _verify_recovery_refs(
    value: Any,
    *,
    field: str,
    frozen_state_ref: tuple[Path, str] | None = None,
) -> None:
    """Verify recovery refs, allowing only the input-bound launch state hash.

    The coordinator updates the mutable state file when it saves a recovery
    assignment.  A trial packet still carries the state digest captured by its
    immutable driver input.  That one exact path/hash pair remains admissible
    during manager-only recovery; every other stale or relabelled ref must
    still match the bytes on disk.
    """
    if isinstance(value, dict):
        if "path" in value and "sha256" in value and isinstance(value.get("path"), str) and value.get("exists") is not False:
            path = Path(value["path"])
            stated = value.get("sha256")
            frozen_match = (
                frozen_state_ref is not None
                and path.resolve() == frozen_state_ref[0].resolve()
                and stated == frozen_state_ref[1]
            )
            if not path.is_file() or (sha256_file(path) != stated and not frozen_match):
                raise COnlyHarborDriverError(f"{field} evidence ref is missing or changed: {path}")
        for key, child in value.items():
            _verify_recovery_refs(child, field=f"{field}.{key}", frozen_state_ref=frozen_state_ref)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _verify_recovery_refs(child, field=f"{field}[{index}]", frozen_state_ref=frozen_state_ref)


def _continue_from_completed_trial(
    input_value: dict[str, Any],
    input_path: Path,
    output_path: Path,
    reconciliation_manifest_path: Path | None = None,
    *,
    extraction_builder: Callable[..., tuple[dict[str, Any], list[dict[str, Any]]]] | None = None,
) -> dict[str, Any]:
    """Continue manager phases from a proven completed Harbor trial stage.

    This narrow recovery path is intentionally separate from normal launch:
    it accepts only an immutable completed trial stage, no later stage/output,
    and no existing manager journal for the trial.  The last condition is
    fail-closed because an existing prepared or finished manager call could
    otherwise be paid twice or left semantically ambiguous.
    """
    if reconciliation_manifest_path is not None and extraction_builder is None:
        raise COnlyHarborDriverError(
            "pre-Graph manager reconciliation requires scripts/legacy/run_r015_c_only_harbor_driver.py"
        )
    if extraction_builder is not None and reconciliation_manifest_path is None:
        raise COnlyHarborDriverError("historical extraction requires an explicit reconciliation manifest")
    if output_path.exists():
        raise COnlyHarborDriverError(f"continuation output already exists; use durable recovery instead: {output_path}")
    for phase in ("extraction", "publication"):
        if input_path.with_name(f"driver-stage-{phase}.json").exists():
            raise COnlyHarborDriverError(f"trial continuation cannot coexist with a durable {phase} stage")
    stage_path = input_path.with_name("driver-stage-trial.json")
    if not stage_path.is_file():
        raise COnlyHarborDriverError(f"completed trial stage is missing: {stage_path}")
    stage = _read_json_file(stage_path, field="driver-stage-trial")
    assignment = _object(input_value.get("assignment"), field="input.assignment")
    for field in ("condition", "round_id", "task_id", "trial_id"):
        expected = "C-only" if field == "condition" else assignment[field]
        if stage.get(field) != expected:
            raise COnlyHarborDriverError(f"trial continuation stage is bound to a different {field}")
    if stage.get("input_path") != str(input_path) or stage.get("input_sha256") != sha256_file(input_path):
        raise COnlyHarborDriverError("trial continuation input binding changed")
    payload = _object(stage.get("payload"), field="driver-stage-trial.payload")
    if stage.get("payload_sha256") != _hash_json(payload):
        raise COnlyHarborDriverError("trial continuation stage payload hash changed")
    input_state = _object(input_value.get("state"), field="input.state")
    frozen_state_ref = (
        Path(_text(input_state.get("path"), field="input.state.path")).resolve(),
        _text(input_state.get("sha256"), field="input.state.sha256"),
    )
    _verify_recovery_refs(
        payload,
        field="driver-stage-trial.payload",
        frozen_state_ref=frozen_state_ref,
    )
    trial = _object(payload.get("trial"), field="driver-stage-trial.payload.trial")
    if trial.get("condition") != "C-only" or trial.get("round_id") != assignment["round_id"] or trial.get("task_id") != assignment["task_id"] or trial.get("trial_id") != assignment["trial_id"]:
        raise COnlyHarborDriverError("trial continuation stage trial identity differs from the frozen assignment")
    if trial.get("outcome") != "completed":
        raise COnlyHarborDriverError("only a completed official Harbor trial can enter manager-phase continuation")
    trace = _object(payload.get("trace"), field="driver-stage-trial.payload.trace")
    source = _object(trace.get("source"), field="driver-stage-trial.payload.trace.source")
    if source.get("canonical_instance_id") != assignment["task_id"]:
        raise COnlyHarborDriverError("trial continuation trace source differs from the frozen task")
    attempts = payload.get("proxy_attempt_records")
    if not isinstance(attempts, list):
        raise COnlyHarborDriverError("trial continuation stage has no complete sidecar attempt list")
    for index, attempt in enumerate(attempts):
        if not isinstance(attempt, dict) or attempt.get("trial_id") != assignment["trial_id"]:
            raise COnlyHarborDriverError(f"trial continuation sidecar attempt {index} belongs to another trial")
    process = _object(payload.get("official_process"), field="driver-stage-trial.payload.official_process")
    if process.get("official_trial_boundary_started") is not True or process.get("classification") not in {"completed", "harbor_nonzero"}:
        raise COnlyHarborDriverError("trial continuation stage does not prove an official Harbor trial boundary")

    reconciliation: dict[str, Any] | None = None
    if reconciliation_manifest_path is not None:
        reconciliation_manifest_path = Path(reconciliation_manifest_path).resolve()
        try:
            reconciliation = load_reconciliation_manifest(reconciliation_manifest_path)
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            raise COnlyHarborDriverError(f"manager reconciliation manifest is not valid: {error}") from error
        manifest_trial = _object(reconciliation.get("trial"), field="reconciliation.trial")
        for field, expected in (
            ("round_id", assignment.get("round_id")),
            ("task_id", assignment.get("task_id")),
            ("trial_id", assignment.get("trial_id")),
            ("session_id", stage.get("session_id")),
        ):
            if manifest_trial.get(field) != expected:
                raise COnlyHarborDriverError(f"manager reconciliation differs from the trial at {field}")
        if reconciliation.get("phase") != "event-001":
            raise COnlyHarborDriverError("only the first event extraction call can be explicitly reconciled")
        stage_ref = reconciliation.get("driver_stage")
        if not isinstance(stage_ref, dict) or stage_ref.get("path") != str(stage_path) or stage_ref.get("sha256") != sha256_file(stage_path):
            raise COnlyHarborDriverError("manager reconciliation is not bound to this completed trial stage")
        original_trajectory = _object(_object(reconciliation.get("original"), field="reconciliation.original").get("trajectory"), field="reconciliation.original.trajectory")
        saved_trajectory = _object(trial.get("trajectory"), field="driver-stage-trial.payload.trial.trajectory")
        if original_trajectory.get("path") != saved_trajectory.get("path") or original_trajectory.get("sha256") != saved_trajectory.get("sha256"):
            raise COnlyHarborDriverError("manager reconciliation is not bound to the completed trial trajectory")
        original_session = reconciliation.get("original", {}).get("session")
        if isinstance(original_session, dict):
            if original_session.get("session_id") != stage.get("session_id"):
                raise COnlyHarborDriverError("manager reconciliation session ID differs from the completed trial")
            session_path = Path(_text(original_session.get("path"), field="reconciliation.original.session.path"))
            if not session_path.is_file() or sha256_file(session_path) != original_session.get("sha256"):
                raise COnlyHarborDriverError("manager reconciliation session bytes changed")

    # A continuation is safe only before any manager call has been prepared.
    # If a process died after preparing a call, the operator must reconcile
    # that call from its journal rather than silently creating a duplicate.
    journal_root = output_path.parents[2] / "manager-journals" / str(assignment["task_id"])
    trial_journal_dir = journal_root / sha256_text(str(assignment["trial_id"]))[:20]
    journal_files = sorted(path for path in trial_journal_dir.glob("*.json") if path.is_file()) if trial_journal_dir.is_dir() else []
    if reconciliation is None:
        if journal_files:
            raise COnlyHarborDriverError(
                "trial continuation found existing manager journals; paid manager state is ambiguous and requires reconciliation"
            )
    else:
        original = _object(reconciliation.get("original"), field="reconciliation.original")
        original_journal = _object(original.get("journal"), field="reconciliation.original.journal")
        expected_journal = trial_journal_dir / "event-001.json"
        if journal_files != [expected_journal]:
            raise COnlyHarborDriverError("manager reconciliation requires exactly the preserved event-001 journal and no other original journal")
        if original_journal.get("path") != str(expected_journal) or original_journal.get("sha256") != sha256_file(expected_journal):
            raise COnlyHarborDriverError("manager reconciliation original journal reference changed")

    paths, metadata, _task_path_value = _task_root_and_service(input_value)
    stage_metadata = _object(payload.get("task_metadata"), field="driver-stage-trial.payload.task_metadata")
    for key in ("task_name", "task_path", "task_toml", "public_environment"):
        if canonical_json(stage_metadata.get(key)) != canonical_json(metadata.get(key)):
            raise COnlyHarborDriverError(f"trial continuation public task metadata changed at {key}")
    context = _build_trial_context(
        input_value,
        output_path=output_path,
        paths=paths,
        metadata=metadata,
        check_only=True,
    )
    context["input_path"] = input_path
    context["assignment"] = deepcopy(assignment)
    context["driver_config"] = _object(input_value["config"].get("driver", {}), field="input.config.driver")
    context["manager_reconciliation_path"] = reconciliation_manifest_path
    manager, executor, manager_context = _manager_context(context, output_path=output_path)
    current_ref = _current_trace_ref(
        context,
        trace,
        trajectory_ref=_object(trial.get("trajectory"), field="driver-stage-trial.payload.trial.trajectory"),
    )
    extraction, event_attempts = (extraction_builder or _build_extraction)(
        context=context,
        input_value=input_value,
        trace=trace,
        current_ref=current_ref,
        executor=executor,
        **({"reconciliation": reconciliation,
            "reconciliation_path": reconciliation_manifest_path}
           if extraction_builder is not None else {}),
    )
    extraction_stage = _write_phase_stage(
        input_path=input_path,
        context=context,
        phase="extraction",
        payload={
            "trial": trial,
            "event_attempts": event_attempts,
            "extraction": extraction,
            "official_process": process,
            "task_metadata": metadata,
            "manager_reconciliation": {
                "path": str(reconciliation_manifest_path),
                "sha256": sha256_file(reconciliation_manifest_path),
            }
            if reconciliation_manifest_path is not None
            else None,
        },
    )
    _wait_extraction_ack(
        input_value=input_value, input_path=input_path, output_path=output_path,
        assignment=assignment, extraction_stage=extraction_stage,
        extraction=extraction,
    )
    frozen_bank = SkillBank.from_dict(_object(assignment.get("frozen_bank"), field="assignment.frozen_bank"))
    manager_decisions, operations = _extraction_publication(
        context=context,
        executor=executor,
        frozen_bank=frozen_bank,
        extraction=extraction,
        supplied_attempts=attempts,
        trace=trace,
        input_value=input_value,
    )
    manager_ledger_snapshot = _snapshot_manager_ledger(context, manager_context)
    publication = {
        "condition": "C-only",
        "round_id": int(assignment["round_id"]),
        "task_id": str(assignment["task_id"]),
        "operations": operations,
        "manager_decisions": manager_decisions,
        "evidence": {
            "kind": "r015_c_only_publication_evidence",
            "manager_ledger": manager_ledger_snapshot,
            "manager_calls_before_trial": manager_context["manager_ledger_before_calls"],
            "continuation_from": "completed_trial_stage_with_reconciled_manager_call" if reconciliation is not None else "completed_trial_stage",
            "manager_reconciliation": {
                "path": str(reconciliation_manifest_path),
                "sha256": sha256_file(reconciliation_manifest_path),
            }
            if reconciliation_manifest_path is not None
            else None,
            "historical_baseline_used": False,
        },
    }
    _write_phase_stage(
        input_path=input_path,
        context=context,
        phase="publication",
        payload={
            "trial": trial,
            "event_attempts": event_attempts,
            "extraction": extraction,
            "publication": publication,
            "official_process": process,
            "task_metadata": metadata,
        },
    )
    output = {
        "schema_version": 1,
        "kind": "r015_c_only_trial_driver_output",
        "condition": "C-only",
        "round_id": trial["round_id"],
        "task_id": trial["task_id"],
        "trial": trial,
        "trace": deepcopy(trace),
        "proxy_attempt_records": deepcopy(attempts),
        "event_attempts": event_attempts,
        "extraction": extraction,
        "publication": publication,
        "official_process": process,
        "task_metadata": metadata,
        "historical_baseline_used": False,
        "evidence_mode": "official_live",
        "manager_reconciliation": {
            "path": str(reconciliation_manifest_path),
            "sha256": sha256_file(reconciliation_manifest_path),
        }
        if reconciliation_manifest_path is not None
        else None,
    }
    write_json(output_path, output)
    return output


def _continue_from_completed_extraction(
    input_value: dict[str, Any], input_path: Path, output_path: Path,
) -> dict[str, Any]:
    """Publish one already acknowledged extraction without repeating paid earlier phases."""
    if output_path.exists() or input_path.with_name("driver-stage-publication.json").exists():
        raise COnlyHarborDriverError("extraction continuation already has publication output")
    assignment = _object(input_value.get("assignment"), field="input.assignment")
    original_process = _read_json_file(input_path.with_name("driver-process.json"),
                                       field="original failed driver process")
    if original_process.get("status") != "failed" or original_process.get("input_sha256") != sha256_file(input_path):
        raise COnlyHarborDriverError("extraction continuation requires the exact failed original process")
    stages = {}
    frozen_state = _object(input_value.get("state"), field="input.state")
    frozen_state_ref = (
        Path(_text(frozen_state.get("path"), field="input.state.path")).resolve(),
        _text(frozen_state.get("sha256"), field="input.state.sha256"),
    )
    for phase in ("trial", "extraction"):
        path = input_path.with_name(f"driver-stage-{phase}.json")
        stage = _read_json_file(path, field=f"driver-stage-{phase}")
        for field, expected in (
            ("kind", "r015_c_only_driver_stage"), ("status", "complete"),
            ("phase", phase), ("condition", "C-only"),
            ("round_id", assignment["round_id"]), ("task_id", assignment["task_id"]),
            ("trial_id", assignment["trial_id"]), ("input_path", str(input_path)),
            ("input_sha256", sha256_file(input_path)),
        ):
            if stage.get(field) != expected:
                raise COnlyHarborDriverError(f"{phase} continuation stage differs at {field}")
        payload = _object(stage.get("payload"), field=f"driver-stage-{phase}.payload")
        if stage.get("payload_sha256") != _hash_json(payload):
            raise COnlyHarborDriverError(f"{phase} continuation payload hash changed")
        _verify_recovery_refs(payload, field=f"driver-stage-{phase}.payload",
                              frozen_state_ref=frozen_state_ref)
        stages[phase] = (stage, payload)
    trial_stage, trial_payload = stages["trial"]
    extraction_stage, extraction_payload = stages["extraction"]
    if trial_stage.get("session_id") != extraction_stage.get("session_id"):
        raise COnlyHarborDriverError("extraction continuation session differs")
    for field in ("trial", "official_process", "task_metadata"):
        if canonical_json(trial_payload.get(field)) != canonical_json(extraction_payload.get(field)):
            raise COnlyHarborDriverError(f"extraction continuation changed {field}")
    trial = _object(trial_payload.get("trial"), field="trial stage trial")
    if trial.get("outcome") != "completed" or trial.get("trial_id") != assignment["trial_id"]:
        raise COnlyHarborDriverError("extraction continuation requires the completed assigned trial")
    trace = _object(trial_payload.get("trace"), field="trial stage trace")
    attempts = trial_payload.get("proxy_attempt_records")
    event_attempts = extraction_payload.get("event_attempts")
    if not isinstance(attempts, list) or not isinstance(event_attempts, list):
        raise COnlyHarborDriverError("extraction continuation attempts are incomplete")
    extraction = _object(extraction_payload.get("extraction"), field="extraction stage extraction")
    if extraction.get("task_id") != assignment["task_id"] or extraction.get("round_id") != assignment["round_id"]:
        raise COnlyHarborDriverError("extraction continuation identity differs")
    handoff_path = output_path.with_name("extraction-awaiting-ack.json")
    ack_path = output_path.with_name("extraction-ack.json")
    handoff = _read_json_file(handoff_path, field="extraction handoff")
    ack = _read_json_file(ack_path, field="extraction acknowledgement")
    extraction_path = input_path.with_name("driver-stage-extraction.json")
    if handoff.get("kind") != "r015_task_extraction_handoff_v1" or \
            ack.get("kind") != "r015_task_extraction_ack_v1" or \
            handoff.get("input_sha256") != sha256_file(input_path) or \
            handoff.get("extraction_stage_path") != str(extraction_path) or \
            handoff.get("extraction_stage_sha256") != sha256_file(extraction_path) or \
            ack.get("handoff_sha256") != sha256_file(handoff_path):
        raise COnlyHarborDriverError("extraction continuation handoff binding differs")
    state_path = Path(_text(frozen_state.get("path"), field="input.state.path"))
    durable = _read_json_file(state_path, field="durable extraction state")
    saved_assignment = durable["rounds"][str(assignment["round_id"])]["assignments"][assignment["task_id"]]
    saved_extraction = _object(saved_assignment.get("extraction"), field="durable extraction")
    source_candidates = [(item.get("candidate_id"), item.get("candidate_fingerprint"))
                         for item in extraction.get("candidates", [])]
    saved_candidates = [(item.get("candidate_id"), item.get("candidate_fingerprint"))
                        for item in saved_extraction.get("candidates", [])]
    if not isinstance(ack.get("durable_state_sha256"), str) or len(ack["durable_state_sha256"]) != 64 or \
            any(char not in "0123456789abcdef" for char in ack["durable_state_sha256"]) or \
            canonical_json(saved_extraction.get("evidence")) != canonical_json(extraction.get("evidence")) or \
            canonical_json(saved_extraction.get("trajectory_ref")) != canonical_json(extraction.get("trajectory_ref")) or \
            source_candidates != saved_candidates:
        raise COnlyHarborDriverError("extraction was not durably acknowledged")
    journal_root = output_path.parents[2] / "manager-journals" / str(assignment["task_id"])
    journal_dir = journal_root / sha256_text(str(assignment["trial_id"]))[:20]
    if journal_dir.is_dir() and any(journal_dir.iterdir()):
        raise COnlyHarborDriverError("publication manager journal already exists; reconcile before retry")
    paths, metadata, _ = _task_root_and_service(input_value)
    stage_metadata = _object(trial_payload.get("task_metadata"), field="trial stage task metadata")
    for field in ("task_name", "task_path", "task_toml", "public_environment"):
        if canonical_json(metadata.get(field)) != canonical_json(stage_metadata.get(field)):
            raise COnlyHarborDriverError(f"extraction continuation task metadata changed at {field}")
    context = _build_trial_context(input_value, output_path=output_path, paths=paths,
                                   metadata=metadata, check_only=True)
    context["input_path"] = input_path
    context["assignment"] = deepcopy(assignment)
    context["driver_config"] = _object(input_value["config"].get("driver", {}), field="input.config.driver")
    _manager, executor, manager_context = _manager_context(context, output_path=output_path)
    frozen_bank = SkillBank.from_dict(_object(assignment.get("frozen_bank"), field="assignment.frozen_bank"))
    decisions, operations = _extraction_publication(
        context=context, executor=executor, frozen_bank=frozen_bank,
        extraction=extraction, supplied_attempts=attempts, trace=trace,
        input_value=input_value,
    )
    identity = {field: trial[field] for field in ("condition", "round_id", "task_id")}
    publication = {**identity, "operations": operations, "manager_decisions": decisions,
        "evidence": {"kind": "r015_c_only_publication_evidence",
                     "manager_ledger": _snapshot_manager_ledger(context, manager_context),
                     "manager_calls_before_trial": manager_context["manager_ledger_before_calls"],
                     "continuation_from": "completed_extraction_stage",
                     "historical_baseline_used": False}}
    _write_phase_stage(input_path=input_path, context=context, phase="publication",
        payload={"trial": trial, "official_process": trial_payload["official_process"],
                 "task_metadata": stage_metadata, "event_attempts": event_attempts,
                 "extraction": extraction, "publication": publication})
    output = {"schema_version": 1, "kind": "r015_c_only_trial_driver_output", **identity,
        "trial": trial, "official_process": trial_payload["official_process"],
        "task_metadata": stage_metadata, "trace": trace, "proxy_attempt_records": attempts,
        "event_attempts": event_attempts, "extraction": extraction,
        "publication": publication, "historical_baseline_used": False,
        "evidence_mode": "official_live"}
    write_json(output_path, output)
    return output


def _recovery_ref_path(value: Any, *, field: str, require_file: bool = True) -> Path:
    ref = _object(value, field=field)
    path = Path(_text(ref.get("path"), field=f"{field}.path")).resolve()
    if require_file:
        stated = _text(ref.get("sha256"), field=f"{field}.sha256")
        if not path.is_file() or sha256_file(path) != stated:
            raise COnlyHarborDriverError(f"{field} is missing or changed: {path}")
    return path


def _recovery_manifest_binding(
    manifest_path: Path,
    input_value: dict[str, Any],
    input_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Validate one original-bound recovery manifest before any import.

    The outer coordinator has already written a fresh recovery process intent
    by the time this function runs.  Only that new process path may exist in
    the recovery namespace; the original failed process, input, import-failure
    record, and Harbor result files must remain immutable and independently
    hashed.
    """

    try:
        manifest = load_harbor_recovery_manifest(manifest_path)
    except (HarborRecoveryError, OSError, ValueError, KeyError) as error:
        raise COnlyHarborDriverError(f"Harbor recovery manifest is invalid: {error}") from error
    assignment = _object(input_value.get("assignment"), field="input.assignment")
    input_ref = _object(manifest.get("input"), field="recovery.input")
    if Path(_text(input_ref.get("path"), field="recovery.input.path")).resolve() != input_path.resolve() or input_ref.get("sha256") != sha256_file(input_path):
        raise COnlyHarborDriverError("Harbor recovery manifest is not bound to the exact original driver input")
    state_input = _object(input_value.get("state"), field="input.state")
    state_manifest = _object(manifest.get("state"), field="recovery.state")
    if Path(_text(state_manifest.get("path"), field="recovery.state.path")).resolve() != Path(_text(state_input.get("path"), field="input.state.path")).resolve() or state_manifest.get("sha256") != state_input.get("sha256"):
        raise COnlyHarborDriverError("Harbor recovery state binding differs from the original input")
    manifest_assignment = _object(manifest.get("assignment"), field="recovery.assignment")
    for field in ("condition", "round_id", "task_id", "trial_id", "frozen_bank_state_sha256"):
        if manifest_assignment.get(field) != assignment.get(field):
            raise COnlyHarborDriverError(f"Harbor recovery assignment differs at {field}")
    if manifest_assignment.get("session_id") != _session_id(str(assignment["trial_id"])):
        raise COnlyHarborDriverError("Harbor recovery session identity is not the deterministic frozen assignment session")
    original = _object(manifest.get("original"), field="recovery.original")
    original_process_path = _recovery_ref_path(original.get("driver_process"), field="recovery.original.driver_process")
    original_process = _read_json_file(original_process_path, field="original failed driver process")
    for field, expected in (
        ("status", "failed"),
        ("condition", "C-only"),
        ("round_id", assignment["round_id"]),
        ("task_id", assignment["task_id"]),
        ("trial_id", assignment["trial_id"]),
        ("input_path", str(input_path)),
        ("input_sha256", sha256_file(input_path)),
    ):
        if original_process.get(field) != expected:
            raise COnlyHarborDriverError(f"original failed driver process differs at {field}")
    original_output = Path(_text(original_process.get("output_path"), field="original failed driver process.output_path")).resolve()
    if original.get("output", {}).get("path") != str(original_output) or original_output.exists():
        raise COnlyHarborDriverError("original failed driver output is not the preserved missing-output boundary")
    original_stage_values = _object(original.get("stages"), field="recovery.original.stages")
    for phase in _DRIVER_PHASES:
        stage_value = _object(original_stage_values.get(phase), field=f"recovery.original.stages.{phase}")
        stage_path = Path(_text(stage_value.get("path"), field=f"recovery.original.stages.{phase}.path")).resolve()
        if stage_path.exists() or stage_value.get("exists") is not False:
            raise COnlyHarborDriverError(f"original {phase} stage is not the preserved missing-stage boundary")
    import_failure_path = _recovery_ref_path(original.get("import_failure"), field="recovery.original.import_failure")
    import_failure = _read_json_file(import_failure_path, field="original Harbor import failure")
    for field, expected in (
        ("kind", "r015_c_only_official_import_failure"),
        ("task_id", assignment["task_id"]),
        ("trial_id", assignment["trial_id"]),
    ):
        if import_failure.get(field) != expected:
            raise COnlyHarborDriverError(f"original Harbor import failure differs at {field}")
    official_process_path = _recovery_ref_path(original.get("official_harbor_process"), field="recovery.original.official_harbor_process")
    official_process = _read_json_file(official_process_path, field="original official Harbor process")
    # The raw process contains transport fields. Config refs are added by
    # _process_evidence in the import-failure packet, not in harbor-process.json.
    process_evidence = _object(import_failure.get("process"), field="import failure.process")
    for key, value in official_process.items():
        if canonical_json(process_evidence.get(key)) != canonical_json(value):
            raise COnlyHarborDriverError(f"import failure differs from the original Harbor process at {key}")
    if official_process.get("official_trial_boundary_started") is not True or official_process.get("classification") not in {"completed", "harbor_nonzero"}:
        raise COnlyHarborDriverError("original official Harbor process does not prove a terminal started task boundary")
    trial_dir = Path(_text(original.get("official_trial_dir"), field="recovery.original.official_trial_dir")).resolve()
    sidecar_dir = Path(_text(original.get("sidecar_dir"), field="recovery.original.sidecar_dir")).resolve()
    if not trial_dir.is_dir() or not sidecar_dir.is_dir():
        raise COnlyHarborDriverError("original Harbor trial or sidecar directory is missing")
    for field in ("result", "config", "instruction", "reward"):
        ref_path = _recovery_ref_path(original.get(field), field=f"recovery.original.{field}")
        expected = trial_dir / {"result": "result.json", "config": "config.json", "instruction": "agent/instruction.txt", "reward": "verifier/reward.txt"}[field]
        if ref_path != expected.resolve():
            raise COnlyHarborDriverError(f"recovery.original.{field} is outside the exact Harbor trial directory")
    session_ref = original.get("session")
    sqlite_ref = original.get("sqlite")
    if session_ref is not None:
        _recovery_ref_path(session_ref, field="recovery.original.session")
    if sqlite_ref is not None:
        _recovery_ref_path(sqlite_ref, field="recovery.original.sqlite")
    attempts = original.get("sidecar_attempts")
    if not isinstance(attempts, list) or not attempts:
        raise COnlyHarborDriverError("recovery manifest has no original sidecar attempt refs")
    expected_attempts: dict[Path, str] = {}
    for index, ref in enumerate(attempts):
        path = _recovery_ref_path(ref, field=f"recovery.original.sidecar_attempts[{index}]")
        try:
            path.relative_to((sidecar_dir / "upstream_requests").resolve())
        except ValueError as error:
            raise COnlyHarborDriverError("recovery sidecar attempt escapes the original sidecar directory") from error
        expected_attempts[path] = _object(ref, field=f"recovery.original.sidecar_attempts[{index}]")["sha256"]
    actual_attempts = {path.resolve(): sha256_file(path) for path in sorted((sidecar_dir / "upstream_requests").glob("attempt-*.json")) if path.is_file()}
    if actual_attempts != expected_attempts:
        raise COnlyHarborDriverError("original sidecar attempt set or hashes changed after recovery manifest creation")
    recovery = _object(manifest.get("recovery"), field="recovery.recovery")
    recovery_root = Path(_text(recovery.get("root"), field="recovery.recovery.root")).resolve()
    output_ref = _object(recovery.get("output"), field="recovery.recovery.output")
    process_ref = _object(recovery.get("process"), field="recovery.recovery.process")
    intent_ref = _object(recovery.get("intent"), field="recovery.recovery.intent")
    completion_ref = _object(recovery.get("completion"), field="recovery.recovery.completion")
    if Path(_text(output_ref.get("path"), field="recovery.recovery.output.path")).resolve() != output_path.resolve():
        raise COnlyHarborDriverError("recovery output path differs from the manifest")
    if not output_path.is_relative_to(recovery_root):
        raise COnlyHarborDriverError("recovery output escapes the recovery namespace")
    recovery_process_path = Path(_text(process_ref.get("path"), field="recovery.recovery.process.path")).resolve()
    recovery_intent_path = Path(_text(intent_ref.get("path"), field="recovery.recovery.intent.path")).resolve()
    recovery_completion_path = Path(_text(completion_ref.get("path"), field="recovery.recovery.completion.path")).resolve()
    if (
        recovery_process_path != recovery_root / "driver-recovery-process.json"
        or recovery_intent_path != recovery_root / "recovery-intent.json"
        or recovery_completion_path != recovery_root / "recovery-complete.json"
    ):
        raise COnlyHarborDriverError("recovery process/intent paths are not the fixed namespace paths")
    if output_path.exists():
        raise COnlyHarborDriverError("recovery output already exists; automatic recovery replay is disabled")
    if recovery_intent_path.exists():
        raise COnlyHarborDriverError("recovery intent already exists; an earlier recovery may be in flight or ambiguous")
    if recovery_completion_path.exists():
        raise COnlyHarborDriverError("recovery completion already exists; an earlier recovery may be in flight or ambiguous")
    stage_paths: dict[str, Path] = {}
    stage_values = _object(recovery.get("stages"), field="recovery.recovery.stages")
    for phase in _DRIVER_PHASES:
        stage_value = _object(stage_values.get(phase), field=f"recovery.recovery.stages.{phase}")
        stage_path = Path(_text(stage_value.get("path"), field=f"recovery.recovery.stages.{phase}.path")).resolve()
        expected = recovery_root / f"driver-stage-{phase}.json"
        if stage_path != expected or stage_path.exists():
            raise COnlyHarborDriverError(f"recovery {phase} stage is not a fresh fixed path")
        stage_paths[phase] = stage_path
    manager_root = Path(_text(recovery.get("manager_root"), field="recovery.recovery.manager_root")).resolve()
    return {
        "manifest": manifest,
        "manifest_path": Path(manifest_path).resolve(),
        "original_process_path": original_process_path,
        "original_process": original_process,
        "import_failure_path": import_failure_path,
        "import_failure": import_failure,
        "official_process_path": official_process_path,
        "official_process": official_process,
        "process_evidence": process_evidence,
        "trial_dir": trial_dir,
        "sidecar_dir": sidecar_dir,
        "recovery_root": recovery_root,
        "recovery_process_path": recovery_process_path,
        "recovery_intent_path": recovery_intent_path,
        "recovery_completion_path": recovery_completion_path,
        "stage_paths": stage_paths,
        "manager_root": manager_root,
    }


def _write_recovery_completion(
    binding: dict[str, Any],
    *,
    input_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Seal the recovery stages with a separate immutable completion record.

    ``recovery-intent.json`` is the launch intent and is deliberately never
    rewritten after the first byte is persisted.  Rewriting it after the
    output/stages were produced would invalidate every embedded hash.  This
    second record therefore carries completion state and fixed path pointers;
    the coordinator still requires the recovery process and output hashes
    before applying anything.  The record contains no output/stage digest, so
    it cannot form a circular reference with the output that points to it.
    """

    path = binding["recovery_completion_path"]
    if path.exists():
        raise COnlyHarborDriverError(f"recovery completion already exists: {path}")
    value = {
        "schema_version": 1,
        "kind": "r015_c_only_harbor_recovery_completion",
        "status": "succeeded",
        "completed_at_utc": utc_now(),
        "condition": "C-only",
        "round_id": int(input_path.parent.parent.name.removeprefix("round-"))
        if input_path.parent.parent.name.startswith("round-")
        else None,
        "task_id": input_path.parent.name,
        "input": {"path": str(input_path.resolve()), "sha256": sha256_file(input_path)},
        "manifest": {"path": str(binding["manifest_path"]), "sha256": sha256_file(binding["manifest_path"])},
        "recovery_intent": {"path": str(binding["recovery_intent_path"]), "sha256": sha256_file(binding["recovery_intent_path"])},
        "output_path": str(output_path.resolve()),
        "stage_paths": {phase: str(path.resolve()) for phase, path in binding["stage_paths"].items()},
        "harbor_rerun": False,
        "sidecar_rerun": False,
        "model_calls_before_recovery": 0,
    }
    write_json(path, value)
    return _ref(path)


def _recover_failed_harbor_trial(
    input_value: dict[str, Any],
    input_path: Path,
    output_path: Path,
    recovery_manifest_path: Path,
) -> dict[str, Any]:
    """Import an existing Harbor result after an importer-only failure.

    No Harbor command is built here.  The original failed driver process and
    its missing output remain untouched; all derived packet/stage/output files
    are written in the manifest's fresh recovery namespace and retain exact
    refs to the original input and raw trial.
    """

    binding = _recovery_manifest_binding(recovery_manifest_path, input_value, input_path, output_path)
    recovery_root = binding["recovery_root"]
    if not recovery_root.is_dir():
        raise COnlyHarborDriverError(f"recovery namespace directory is missing: {recovery_root}")
    intent = {
        "schema_version": 1,
        "kind": "r015_c_only_harbor_recovery_intent",
        "status": "running",
        "condition": "C-only",
        "round_id": input_value["assignment"]["round_id"],
        "task_id": input_value["assignment"]["task_id"],
        "trial_id": input_value["assignment"]["trial_id"],
        "input": {"path": str(input_path.resolve()), "sha256": sha256_file(input_path)},
        "manifest": {"path": str(binding["manifest_path"]), "sha256": sha256_file(binding["manifest_path"])},
        "original_driver_process": {"path": str(binding["original_process_path"]), "sha256": sha256_file(binding["original_process_path"])},
        "original_import_failure": {"path": str(binding["import_failure_path"]), "sha256": sha256_file(binding["import_failure_path"])},
        "official_harbor_process": {"path": str(binding["official_process_path"]), "sha256": sha256_file(binding["official_process_path"])},
        "completion_path": str(binding["recovery_completion_path"]),
        "immutable_launch_intent": True,
        "harbor_rerun": False,
        "sidecar_rerun": False,
        "model_calls_before_recovery": 0,
        "created_at_utc": utc_now(),
    }
    write_json(binding["recovery_intent_path"], intent)
    paths, metadata, _task_path = _task_root_and_service(input_value)
    context = _build_trial_context(
        input_value,
        output_path=output_path,
        paths=paths,
        metadata=metadata,
        check_only=True,
    )
    artifact_root = output_path.parent / "official-harbor"
    artifact_root.mkdir(parents=True, exist_ok=False)
    context["artifact_root"] = artifact_root
    context["input_path"] = input_path
    context["assignment"] = deepcopy(input_value["assignment"])
    context["driver_config"] = _object(input_value["config"].get("driver", {}), field="input.config.driver")
    context["sidecar_dir"] = binding["sidecar_dir"]
    context["harbor_trial_dir"] = binding["trial_dir"]
    context["jobs_dir"] = binding["trial_dir"].parent.parent
    context["trusted_manager_root"] = binding["manager_root"]
    # ``_process_evidence`` adds the task/sidecar/OpenClaw/database refs to
    # every recovered packet.  The check-only context initially points at a
    # new, unwritten recovery artifact directory; bind those fields back to
    # the exact existing files recorded by the terminal official process so
    # recovery cannot emit ``exists=false`` placeholders or relabel a fresh
    # config as the historical Harbor invocation.
    for context_key, process_key in (
        ("job_config", "task_config"),
        ("sidecar_config", "sidecar_config"),
        ("host_config", "openclaw_host_config"),
        ("database", "native_database_seed"),
    ):
        ref = _object(binding["process_evidence"].get(process_key), field=f"import failure.process.{process_key}")
        candidate = _recovery_ref_path(ref, field=f"import failure.process.{process_key}")
        if sha256_file(candidate) != ref.get("sha256"):
            raise COnlyHarborDriverError(f"original Harbor {process_key} ref changed during recovery: {candidate}")
        context[context_key] = candidate
    context["recovery_binding"] = {
        "manifest": {"path": str(binding["manifest_path"]), "sha256": sha256_file(binding["manifest_path"])},
        "original_driver_process": {"path": str(binding["original_process_path"]), "sha256": sha256_file(binding["original_process_path"])},
        "original_import_failure": {"path": str(binding["import_failure_path"]), "sha256": sha256_file(binding["import_failure_path"])},
        "official_harbor_process": {"path": str(binding["official_process_path"]), "sha256": sha256_file(binding["official_process_path"])},
        "recovery_intent": {"path": str(binding["recovery_intent_path"]), "sha256": sha256_file(binding["recovery_intent_path"])},
        "harbor_rerun": False,
        "sidecar_rerun": False,
    }
    imported = _import_official_evidence(context, binding["official_process"])
    return _finish_imported_trial(
        input_value, input_path, output_path, context,
        binding["official_process"], metadata, imported, recovery=binding,
    )


def _check_config(input_value: dict[str, Any], input_path: Path, output_path: Path) -> dict[str, Any]:
    paths, metadata, _task_path_value = _task_root_and_service(input_value)
    context = _build_trial_context(
        input_value,
        output_path=output_path,
        paths=paths,
        metadata=metadata,
        check_only=False,
    )
    context["input_path"] = input_path
    check = _sidecar_check(context)
    harbor_check = _harbor_config_check(context)
    target = _object(context["target"], field="prepared_target")
    provider = _object(context["openclaw_config"]["models"]["providers"]["codeskill-r012"], field="rendered provider")
    model = _object(provider["models"][0], field="rendered provider model")
    params = _object(context["openclaw_config"]["agents"]["defaults"]["models"][f"codeskill-r012/{target['model_id']}"]["params"], field="rendered model params")
    return {
        "status": "valid",
        "kind": "r015_c_only_official_harbor_driver_check",
        "input": {"path": str(input_path), "sha256": sha256_file(input_path)},
        "official_adapter": "codeskill_rebuild.harbor_openclaw_adapter:CODESKILLHarborOpenClaw",
        "harbor_executable": context["harbor"],
        "task_metadata": metadata,
        "sidecar_check": check,
        "harbor_job_config_check": harbor_check,
        "effective_public_openclaw_config": {
            "provider_base_url": provider.get("baseUrl"),
            "provider_timeout_seconds": provider.get("timeoutSeconds"),
            "model_id": model.get("id"),
            "reasoning": model.get("reasoning"),
            "context_tokens": model.get("contextTokens"),
            "max_output_tokens": model.get("maxTokens"),
            "proxy_max_input_tokens": context["sidecar_value"]["overlay"].get("maxInputTokens"),
            "proxy_max_forwarded_requests": None,
            "thinking_default": context["openclaw_config"]["agents"]["defaults"].get("thinkingDefault"),
            "params": params,
        },
        "no_model_or_harbor_trial_started": True,
        "source_policy": "official Harbor package plus public CODESKILL plugin/sidecar; no OpenClaw source mount or patch",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument(
        "--continue-from-trial",
        action="store_true",
        help="continue manager phases from an immutable completed trial stage without rerunning Harbor",
    )
    parser.add_argument("--continue-from-extraction", action="store_true",
                        help="publish one durable acknowledged extraction without repeating Harbor or extraction")
    parser.add_argument(
        "--reconciliation-manifest",
        type=Path,
        help="explicitly reuse one audited paid manager response while continuing from a completed trial stage",
    )
    parser.add_argument(
        "--recover-from-harbor-manifest",
        type=Path,
        help="import one already completed Harbor artifact set through a fresh recovery namespace without relaunching Harbor",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    input_path = args.input.resolve()
    output_path = args.output.resolve()
    input_value = _load_input(input_path)
    if args.continue_from_extraction and (args.check_config or args.continue_from_trial or
            args.reconciliation_manifest is not None or args.recover_from_harbor_manifest is not None):
        raise COnlyHarborDriverError("--continue-from-extraction cannot be combined with another mode")
    if args.check_config and args.continue_from_trial:
        raise COnlyHarborDriverError("--check-config and --continue-from-trial are mutually exclusive")
    if args.check_config and args.recover_from_harbor_manifest is not None:
        raise COnlyHarborDriverError("--check-config and --recover-from-harbor-manifest are mutually exclusive")
    if args.continue_from_trial and args.recover_from_harbor_manifest is not None:
        raise COnlyHarborDriverError("--continue-from-trial and --recover-from-harbor-manifest are mutually exclusive")
    if args.reconciliation_manifest is not None and not args.continue_from_trial:
        raise COnlyHarborDriverError("--reconciliation-manifest requires --continue-from-trial")
    if args.reconciliation_manifest is not None and args.recover_from_harbor_manifest is not None:
        raise COnlyHarborDriverError("--reconciliation-manifest and --recover-from-harbor-manifest are mutually exclusive")
    if args.check_config:
        output_value = _check_config(input_value, input_path, output_path)
    elif args.continue_from_trial:
        output_value = _continue_from_completed_trial(
            input_value,
            input_path,
            output_path,
            reconciliation_manifest_path=args.reconciliation_manifest,
        )
    elif args.continue_from_extraction:
        output_value = _continue_from_completed_extraction(input_value, input_path, output_path)
    elif args.recover_from_harbor_manifest is not None:
        output_value = _recover_failed_harbor_trial(
            input_value,
            input_path,
            output_path,
            args.recover_from_harbor_manifest,
        )
    else:
        output_value = _run_trial(input_value, input_path, output_path)
    if not output_path.is_file():
        write_json(output_path, output_value)
    print(json.dumps({"status": "valid" if args.check_config else "completed", "output": str(output_path), "output_sha256": sha256_file(output_path)}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (COnlyHarborDriverError, COnlyProtocolError, R012ExecutionError, OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise SystemExit(f"{type(error).__name__}: {error}") from error
