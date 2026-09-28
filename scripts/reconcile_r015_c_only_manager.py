#!/usr/bin/env python3
"""Audit one saved manager call and prepare an explicit no-Harbor resume.

This command is intentionally model-free.  It re-counts the exact saved chat
request through SGLang's public ``/tokenize`` endpoint, validates the saved JSON
response and its live trajectory binding, and writes a separate reconciliation
manifest.  It never calls ``/chat/completions`` and never changes the original
manager request, response, journal, ledger, or Harbor artifacts.

The resulting manifest is consumed only by the explicit reconciled-manager
continuation path in the official C-only driver after a parent review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from codeskill_rebuild.manager import ServerMessageTokenCounter  # noqa: E402
from codeskill_rebuild.manager_reconciliation import (  # noqa: E402
    ManagerReconciliationError,
    audit_saved_manager_call,
)
from codeskill_rebuild.types import canonical_json, read_json, sha256_file, write_json  # noqa: E402


def _object(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManagerReconciliationError(f"{field} must be an object")
    return value


def _validate_stage(
    stage_path: Path,
    trace_path: Path,
    *,
    round_id: int,
    task_id: str,
    trial_id: str,
    session_id: str,
) -> None:
    try:
        stage = _object(read_json(stage_path), field="driver-stage-trial")
        trace = _object(read_json(trace_path), field="live trajectory")
    except (OSError, ValueError) as error:
        raise ManagerReconciliationError(f"cannot read trial binding files: {error}") from error
    if stage.get("status") != "complete" or stage.get("phase") != "trial":
        raise ManagerReconciliationError("driver-stage-trial is not a completed trial stage")
    for field, expected in (
        ("condition", "C-only"),
        ("round_id", round_id),
        ("task_id", task_id),
        ("trial_id", trial_id),
        ("session_id", session_id),
    ):
        if stage.get(field) != expected:
            raise ManagerReconciliationError(f"driver-stage-trial differs at {field}")
    payload = _object(stage.get("payload"), field="driver-stage-trial.payload")
    if stage.get("payload_sha256") != hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest():
        raise ManagerReconciliationError("driver-stage-trial payload hash changed")
    trial = _object(payload.get("trial"), field="driver-stage-trial.payload.trial")
    if trial.get("outcome") != "completed" or trial.get("trial_id") != trial_id:
        raise ManagerReconciliationError("driver-stage-trial does not contain the completed target trial")
    source = _object(trace.get("source"), field="live trajectory.source")
    binding = _object(trace.get("r015_binding"), field="live trajectory.r015_binding")
    if source.get("canonical_instance_id") != task_id:
        raise ManagerReconciliationError("live trajectory source differs from expected task")
    for field, expected in (
        ("round_id", round_id),
        ("task_id", task_id),
        ("trial_id", trial_id),
        ("session_id", session_id),
    ):
        if binding.get(field) != expected:
            raise ManagerReconciliationError(f"live trajectory binding differs at {field}")
    trajectory = _object(trial.get("trajectory"), field="driver-stage-trial.payload.trial.trajectory")
    if trajectory.get("path") != str(trace_path) or trajectory.get("sha256") != sha256_file(trace_path):
        raise ManagerReconciliationError("driver-stage trajectory reference differs from the saved trace")
    process = _object(payload.get("official_process"), field="driver-stage-trial.payload.official_process")
    if process.get("official_trial_boundary_started") is not True:
        raise ManagerReconciliationError("driver-stage-trial does not prove the official Harbor boundary")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--driver-stage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tokenizer-exchange", type=Path, required=True)
    parser.add_argument(
        "--ledger-snapshot",
        type=Path,
        help="separate immutable copy of the live ledger captured during this audit (defaults beside --output)",
    )
    parser.add_argument("--run-dir", type=Path, required=True, help="formal attempt directory that owns the task artifact and recovery journal")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--call-id", required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--purpose", required=True)
    parser.add_argument("--trial-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--round-id", type=int, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--timeout", type=int, default=30)
    return parser


def main() -> None:
    args = _parser().parse_args()
    for path in (args.request, args.response, args.journal, args.ledger, args.trace, args.driver_stage):
        if not path.is_file():
            raise ManagerReconciliationError(f"required source file is missing: {path}")
    _validate_stage(
        args.driver_stage,
        args.trace,
        round_id=args.round_id,
        task_id=args.task_id,
        trial_id=args.trial_id,
        session_id=args.session_id,
    )
    tokenizer = ServerMessageTokenCounter(args.base_url, timeout_seconds=args.timeout)
    manifest = audit_saved_manager_call(
        request_path=args.request,
        response_path=args.response,
        journal_path=args.journal,
        ledger_path=args.ledger,
        trace_path=args.trace,
        expected={
            "call_id": args.call_id,
            "phase": args.phase,
            "purpose": args.purpose,
            "round_id": args.round_id,
            "task_id": args.task_id,
            "trial_id": args.trial_id,
            "session_id": args.session_id,
        },
        tokenizer=tokenizer,
        output_path=args.output,
        tokenizer_exchange_path=args.tokenizer_exchange,
        driver_stage_ref={"path": str(args.driver_stage), "sha256": sha256_file(args.driver_stage)},
        reconciliation_run_dir=args.run_dir,
        ledger_snapshot_path=args.ledger_snapshot,
    )
    print(json.dumps({"status": manifest["status"], "output": str(args.output), "output_sha256": sha256_file(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (ManagerReconciliationError, OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise SystemExit(f"{type(error).__name__}: {error}") from error
