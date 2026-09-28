"""Fail-closed manifests for recovering one completed Harbor artifact set.

The normal C-only driver writes its input, process record, and phase stages in
one directory.  If importing a completed Harbor trial fails after the Harbor
boundary, that directory may contain an immutable *failed* driver process but
no trial stage.  This module describes a separate recovery namespace that is
bound to the original input and raw Harbor files.  It never copies or edits
the original process, input, import-failure record, or Harbor artifacts.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .types import canonical_json, read_json, sha256_file, sha256_text, utc_now, write_json


class HarborRecoveryError(RuntimeError):
    """A Harbor recovery manifest is missing, contradictory, or stale."""


KIND = "r015_c_only_harbor_recovery_manifest"
STATUS = "ready_for_explicit_harbor_artifact_recovery"


def _object(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HarborRecoveryError(f"{field} must be an object")
    return value


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HarborRecoveryError(f"{field} must be a nonempty string")
    return value.strip()


def _absolute(value: Any, *, field: str) -> Path:
    path = Path(_text(value, field=field))
    if not path.is_absolute():
        raise HarborRecoveryError(f"{field} must be an absolute path")
    return path.resolve()


def file_ref(path: Path, *, field: str, require_exists: bool = True) -> dict[str, Any]:
    path = Path(path).resolve()
    if require_exists and not path.is_file():
        raise HarborRecoveryError(f"{field} is not an existing file: {path}")
    value: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
    if path.is_file():
        value.update({"sha256": sha256_file(path), "size_bytes": path.stat().st_size})
    return value


def _verify_ref(value: Any, *, field: str, require_exists: bool = True) -> Path:
    ref = _object(value, field=field)
    path = _absolute(ref.get("path"), field=f"{field}.path")
    exists = ref.get("exists", True)
    if exists is not True:
        if require_exists:
            raise HarborRecoveryError(f"{field} must identify an existing file: {path}")
        return path
    stated = _text(ref.get("sha256"), field=f"{field}.sha256")
    if not path.is_file():
        raise HarborRecoveryError(f"{field} file is missing: {path}")
    actual = sha256_file(path)
    if actual != stated:
        raise HarborRecoveryError(f"{field} hash changed: {path}")
    return path


def _ref_value(path: Path) -> dict[str, Any]:
    return file_ref(path, field=str(path))


def _read_object(path: Path, *, field: str) -> dict[str, Any]:
    try:
        return _object(read_json(path), field=field)
    except (OSError, ValueError) as error:
        raise HarborRecoveryError(f"cannot read {field}: {path}: {error}") from error


def _empty_recovery_path(path: Path, *, field: str) -> None:
    path = Path(path).resolve()
    if path.exists():
        raise HarborRecoveryError(f"{field} already exists; recovery namespace must be fresh: {path}")


def _ensure_child(path: Path, parent: Path, *, field: str) -> None:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError as error:
        raise HarborRecoveryError(f"{field} must be inside the original execution namespace: {path}") from error


def create_harbor_recovery_manifest(
    *,
    input_path: Path,
    original_driver_process_path: Path,
    import_failure_path: Path,
    official_harbor_process_path: Path,
    trial_dir: Path,
    sidecar_dir: Path,
    recovery_root: Path,
    manager_root: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Create one immutable, original-bound Harbor artifact recovery manifest.

    This operation is model-free.  It validates the failed outer process and
    the existing official result/config/reward/session/sidecar files, then
    writes only the new manifest.  The recovery driver later creates the
    separately named process, stage, and output files under ``recovery_root``.
    """

    input_path = Path(input_path).resolve()
    original_driver_process_path = Path(original_driver_process_path).resolve()
    import_failure_path = Path(import_failure_path).resolve()
    official_harbor_process_path = Path(official_harbor_process_path).resolve()
    trial_dir = Path(trial_dir).resolve()
    sidecar_dir = Path(sidecar_dir).resolve()
    recovery_root = Path(recovery_root).resolve()
    manager_root = Path(manager_root).resolve()
    output_path = Path(output_path).resolve()
    if not input_path.is_file():
        raise HarborRecoveryError(f"original driver input is missing: {input_path}")
    input_value = _read_object(input_path, field="original driver input")
    assignment = _object(input_value.get("assignment"), field="input.assignment")
    state = _object(input_value.get("state"), field="input.state")
    for field in ("condition", "round_id", "task_id", "trial_id", "frozen_bank_state_sha256"):
        if field not in assignment:
            raise HarborRecoveryError(f"input.assignment.{field} is required")
    state_path = _absolute(state.get("path"), field="input.state.path")
    state_hash = _text(state.get("sha256"), field="input.state.sha256")
    if not state_path.is_file():
        raise HarborRecoveryError(f"original bound state is missing: {state_path}")
    original_process = _read_object(original_driver_process_path, field="original driver process")
    if original_process.get("status") != "failed" or original_process.get("returncode") == 0:
        raise HarborRecoveryError("original driver process must be a terminal failed process")
    for field, expected in (
        ("condition", "C-only"),
        ("round_id", assignment.get("round_id")),
        ("task_id", assignment.get("task_id")),
        ("trial_id", assignment.get("trial_id")),
        ("input_path", str(input_path)),
        ("input_sha256", sha256_file(input_path)),
    ):
        if original_process.get(field) != expected:
            raise HarborRecoveryError(f"original driver process differs from input at {field}")
    original_output = Path(_text(original_process.get("output_path"), field="original driver process.output_path")).resolve()
    if original_output.is_file():
        raise HarborRecoveryError(f"original driver output exists; recovery cannot relabel it: {original_output}")
    original_stage_paths = {
        phase: input_path.with_name(f"driver-stage-{phase}.json")
        for phase in ("trial", "extraction", "publication")
    }
    for phase, stage_path in original_stage_paths.items():
        if stage_path.exists():
            raise HarborRecoveryError(f"original {phase} stage exists; recovery requires the missing-stage boundary: {stage_path}")
    import_failure = _read_object(import_failure_path, field="official import failure")
    if import_failure.get("kind") != "r015_c_only_official_import_failure":
        raise HarborRecoveryError("official import failure has an unexpected kind")
    for field, expected in (
        ("task_id", assignment.get("task_id")),
        ("trial_id", assignment.get("trial_id")),
    ):
        if import_failure.get(field) != expected:
            raise HarborRecoveryError(f"official import failure differs from input at {field}")
    official_process = _read_object(official_harbor_process_path, field="official Harbor process")
    if official_process.get("official_trial_boundary_started") is not True:
        raise HarborRecoveryError("official Harbor process does not prove that the task boundary started")
    if official_process.get("classification") not in {"completed", "harbor_nonzero"}:
        raise HarborRecoveryError("official Harbor process is not a terminal task boundary")
    if not trial_dir.is_dir():
        raise HarborRecoveryError(f"official Harbor trial directory is missing: {trial_dir}")
    if not sidecar_dir.is_dir():
        raise HarborRecoveryError(f"original sidecar evidence directory is missing: {sidecar_dir}")
    required_files = {
        "result": trial_dir / "result.json",
        "config": trial_dir / "config.json",
        "instruction": trial_dir / "agent" / "instruction.txt",
        "reward": trial_dir / "verifier" / "reward.txt",
    }
    session = trial_dir / "agent" / "openclaw.session.jsonl"
    sqlite = trial_dir / "agent" / "codeskill-openclaw-state" / "openclaw-agent.sqlite"
    if not session.is_file() and not sqlite.is_file():
        raise HarborRecoveryError("official Harbor trial has neither the raw session JSONL nor SQLite session source")
    attempt_dir = sidecar_dir / "upstream_requests"
    if not attempt_dir.is_dir():
        raise HarborRecoveryError(f"original sidecar attempt directory is missing: {attempt_dir}")
    attempt_refs = [
        _ref_value(path)
        for path in sorted(attempt_dir.glob("attempt-*.json"))
        if path.is_file()
    ]
    if not attempt_refs:
        raise HarborRecoveryError("original sidecar has no durable attempt records")
    recovery_root_parent = input_path.parent
    _ensure_child(recovery_root, recovery_root_parent, field="recovery.root")
    if recovery_root == input_path.parent:
        raise HarborRecoveryError("recovery.root must be a separate child namespace")
    if output_path.exists():
        raise HarborRecoveryError(f"recovery manifest output already exists: {output_path}")
    for path, field in (
        (recovery_root, "recovery.root"),
        (recovery_root / "driver-output.json", "recovery.output"),
        (recovery_root / "driver-recovery-process.json", "recovery.process"),
        (recovery_root / "recovery-intent.json", "recovery.intent"),
        (recovery_root / "recovery-complete.json", "recovery.completion"),
        (recovery_root / "driver-stage-trial.json", "recovery.trial_stage"),
        (recovery_root / "driver-stage-extraction.json", "recovery.extraction_stage"),
        (recovery_root / "driver-stage-publication.json", "recovery.publication_stage"),
    ):
        _empty_recovery_path(path, field=field)
    recovery_root.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": 1,
        "kind": KIND,
        "status": STATUS,
        "created_at_utc": utc_now(),
        "condition": "C-only",
        "assignment": {
            "condition": assignment.get("condition"),
            "round_id": assignment.get("round_id"),
            "task_id": assignment.get("task_id"),
            "trial_id": assignment.get("trial_id"),
            "session_id": "r015-" + sha256_text(str(assignment.get("trial_id")))[:28],
            "frozen_bank_state_sha256": assignment.get("frozen_bank_state_sha256"),
        },
        "input": _ref_value(input_path),
        "state": {"path": str(state_path), "sha256": state_hash},
        "original": {
            "driver_process": _ref_value(original_driver_process_path),
            "import_failure": _ref_value(import_failure_path),
            "official_harbor_process": _ref_value(official_harbor_process_path),
            "official_trial_dir": str(trial_dir),
            "sidecar_dir": str(sidecar_dir),
            "result": _ref_value(required_files["result"]),
            "config": _ref_value(required_files["config"]),
            "instruction": _ref_value(required_files["instruction"]),
            "reward": _ref_value(required_files["reward"]),
            "session": _ref_value(session) if session.is_file() else None,
            "sqlite": _ref_value(sqlite) if sqlite.is_file() else None,
            "sidecar_attempts": attempt_refs,
            "output": {"path": str(original_output), "exists": False},
            "stages": {phase: {"path": str(path), "exists": False} for phase, path in original_stage_paths.items()},
        },
        "recovery": {
            "root": str(recovery_root),
            "manager_root": str(manager_root),
            "output": {"path": str(recovery_root / "driver-output.json"), "exists": False},
            "process": {"path": str(recovery_root / "driver-recovery-process.json"), "exists": False},
            "intent": {"path": str(recovery_root / "recovery-intent.json"), "exists": False},
            # The launch intent is immutable.  A separate completion record is
            # created only after all phase stages have been written, so the
            # output/stage payloads never point at a hash that is later
            # changed by finalisation.
            "completion": {"path": str(recovery_root / "recovery-complete.json"), "exists": False},
            "stages": {
                phase: {"path": str(recovery_root / f"driver-stage-{phase}.json"), "exists": False}
                for phase in ("trial", "extraction", "publication")
            },
        },
        "safety": {
            "harbor_rerun": False,
            "sidecar_rerun": False,
            "model_calls_before_recovery": 0,
            "original_failure_immutable": True,
            "historical_baseline_used": False,
            "recovery_uses_original_input": True,
        },
    }
    # The manifest is written last.  If writing fails, the empty recovery
    # directory remains a harmless operator-visible namespace rather than a
    # partially populated trial.
    write_json(output_path, manifest)
    return manifest


def load_harbor_recovery_manifest(path: Path) -> dict[str, Any]:
    """Load and structurally validate a recovery manifest without side effects."""

    path = Path(path).resolve()
    try:
        value = _read_object(path, field="Harbor recovery manifest")
    except HarborRecoveryError:
        raise
    if value.get("schema_version") != 1 or value.get("kind") != KIND or value.get("status") != STATUS:
        raise HarborRecoveryError("Harbor recovery manifest is not resume-ready")
    assignment = _object(value.get("assignment"), field="recovery.assignment")
    if assignment.get("condition") != "C-only":
        raise HarborRecoveryError("Harbor recovery assignment is not C-only")
    for field in ("round_id", "task_id", "trial_id", "frozen_bank_state_sha256"):
        if field not in assignment:
            raise HarborRecoveryError(f"recovery.assignment.{field} is required")
    input_ref = _object(value.get("input"), field="recovery.input")
    input_path = _verify_ref(input_ref, field="recovery.input")
    state = _object(value.get("state"), field="recovery.state")
    state_path = _absolute(state.get("path"), field="recovery.state.path")
    _text(state.get("sha256"), field="recovery.state.sha256")
    original = _object(value.get("original"), field="recovery.original")
    for field in ("driver_process", "import_failure", "official_harbor_process", "result", "config", "instruction", "reward"):
        _verify_ref(original.get(field), field=f"recovery.original.{field}")
    trial_dir = _absolute(original.get("official_trial_dir"), field="recovery.original.official_trial_dir")
    sidecar_dir = _absolute(original.get("sidecar_dir"), field="recovery.original.sidecar_dir")
    if not trial_dir.is_dir() or not sidecar_dir.is_dir():
        raise HarborRecoveryError("recovery original trial/sidecar directory is missing")
    for field in ("session", "sqlite"):
        ref = original.get(field)
        if ref is not None:
            _verify_ref(ref, field=f"recovery.original.{field}")
    attempts = original.get("sidecar_attempts")
    if not isinstance(attempts, list) or not attempts:
        raise HarborRecoveryError("recovery.original.sidecar_attempts must be a nonempty list")
    for index, ref in enumerate(attempts):
        _verify_ref(ref, field=f"recovery.original.sidecar_attempts[{index}]")
    recovery = _object(value.get("recovery"), field="recovery.recovery")
    root = _absolute(recovery.get("root"), field="recovery.recovery.root")
    manager_root = _absolute(recovery.get("manager_root"), field="recovery.recovery.manager_root")
    _ensure_child(root, input_path.parent, field="recovery.recovery.root")
    if root == input_path.parent:
        raise HarborRecoveryError("recovery root must be separate from the original task directory")
    for field in ("output", "process", "intent", "completion"):
        ref = _object(recovery.get(field), field=f"recovery.recovery.{field}")
        expected = root / {
            "output": "driver-output.json",
            "process": "driver-recovery-process.json",
            "intent": "recovery-intent.json",
            "completion": "recovery-complete.json",
        }[field]
        actual = _absolute(ref.get("path"), field=f"recovery.recovery.{field}.path")
        if actual != expected:
            raise HarborRecoveryError(
                f"recovery.recovery.{field}.path is not the fixed recovery namespace path: {actual}"
            )
    stages = _object(recovery.get("stages"), field="recovery.recovery.stages")
    for phase in ("trial", "extraction", "publication"):
        ref = _object(stages.get(phase), field=f"recovery.recovery.stages.{phase}")
        _absolute(ref.get("path"), field=f"recovery.recovery.stages.{phase}.path")
    safety = _object(value.get("safety"), field="recovery.safety")
    if safety.get("harbor_rerun") is not False or safety.get("sidecar_rerun") is not False:
        raise HarborRecoveryError("Harbor recovery manifest permits a forbidden rerun")
    if safety.get("recovery_uses_original_input") is not True:
        raise HarborRecoveryError("Harbor recovery manifest does not bind to the original input")
    # Keep structural load strict even before the driver performs semantic
    # assignment/result checks.  This also rejects an accidental manifest
    # pointed at a different input path.
    if input_path != Path(_text(value["input"]["path"], field="recovery.input.path")).resolve():
        raise HarborRecoveryError("recovery input path normalization changed")
    return deepcopy(value)


__all__ = [
    "HarborRecoveryError",
    "KIND",
    "STATUS",
    "create_harbor_recovery_manifest",
    "load_harbor_recovery_manifest",
    "file_ref",
]
