#!/usr/bin/env python3
"""Run exactly four ordered first-round C-only trials as a diagnostic.

This entrypoint derives a separate four-task config and comparison-only
baseline metadata from immutable originals. It uses the production C-only
coordinator, official Harbor driver, bank freeze, and publication path, then
stops before the protocol's built-in second-round transition.
"""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "src"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from codeskill_rebuild.c_only_protocol import COnlyProtocol, COnlyProtocolError, validate_c_only_config  # noqa: E402
from codeskill_rebuild.bank import SkillBank  # noqa: E402
from codeskill_rebuild.types import read_json, sha256_file, write_json  # noqa: E402
from scripts import run_r015_c_only as coordinator  # noqa: E402


TASK_IDS = ("build-pmars", "fix-git", "git-leak-recovery",
            "schemelike-metacircular-eval")
CONFIG_NAME = "diagnostic-config.json"
BASELINE_NAME = "diagnostic-baseline-metadata.json"
PROVENANCE_NAME = "diagnostic-provenance.json"
STATE_NAME = "state.json"


def _write_once(path: Path, value: dict[str, Any]) -> None:
    if path.exists():
        if read_json(path) != value:
            raise COnlyProtocolError(f"diagnostic artifact changed: {path}")
        return
    write_json(path, value)


def prepare(*, source_config: Path, source_baseline: Path, run_dir: Path,
            runtime_approval_reference: str) -> dict[str, Any]:
    source_config = source_config.resolve()
    source_baseline = source_baseline.resolve()
    run_dir = run_dir.resolve()
    if not runtime_approval_reference.strip():
        raise COnlyProtocolError("diagnostic runtime approval reference is required")
    # This checks the frozen source pair before making a derived metadata copy.
    COnlyProtocol.initialize(source_config, source_baseline)
    original_config = read_json(source_config)
    original_baseline = read_json(source_baseline)
    config_tasks = {item["canonical_instance_id"]: item for item in original_config["tasks"]}
    baseline_tasks = {item["canonical_instance_id"]: item for item in original_baseline["tasks"]}
    if any(task_id not in config_tasks or task_id not in baseline_tasks for task_id in TASK_IDS):
        raise COnlyProtocolError("diagnostic task is absent from its frozen source pair")
    source_order = [item["canonical_instance_id"] for item in original_config["tasks"]
                    if item["canonical_instance_id"] in TASK_IDS]
    if source_order != list(TASK_IDS):
        raise COnlyProtocolError("diagnostic task order differs from the frozen source")
    run_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = run_dir / BASELINE_NAME
    config_path = run_dir / CONFIG_NAME
    baseline = {"schema_version": original_baseline["schema_version"],
        "kind": original_baseline["kind"],
        "source": deepcopy(original_baseline["source"]),
        "tasks": [{"order": index, "task_name": baseline_tasks[task_id]["task_name"],
                   "canonical_instance_id": task_id,
                   "task_digest": baseline_tasks[task_id].get("task_digest")}
                  for index, task_id in enumerate(TASK_IDS, start=1)]}
    baseline["source"]["skills_imported"] = False
    baseline["source"]["trajectories_imported"] = False
    baseline["source"]["solver_input_imported"] = False
    _write_once(baseline_path, baseline)
    config = deepcopy(original_config)
    config["protocol_id"] = f"{original_config['protocol_id']}-r1-four-diagnostic"
    config["status"] = "diagnostic_prepared"
    config["tasks"] = [{**deepcopy(config_tasks[task_id]), "order": index}
                       for index, task_id in enumerate(TASK_IDS, start=1)]
    config["baseline_manifest"] = {**config["baseline_manifest"],
        "path": str(baseline_path), "sha256": sha256_file(baseline_path)}
    config["formal"] = {"status": "diagnostic_only", "planned_trials": 4,
                        "task_count_per_round": 4, "rounds_to_execute": 1}
    config["diagnostic_scope"] = {"kind": "r015_first_round_four_task_diagnostic",
        "task_order": list(TASK_IDS), "max_trials": 4,
        "stop_before_round_2": True,
        "source_config_sha256": sha256_file(source_config),
        "source_baseline_sha256": sha256_file(source_baseline),
        "runtime_approval_reference": runtime_approval_reference.strip(),
        "runtime_approval_scope": "prepared OpenClaw 2026.9.3 and Harbor 0.17.1; historical runtime and image identity differ"}
    validate_c_only_config(config)
    _write_once(config_path, config)
    # Verify the derived pair through the production protocol validator.
    COnlyProtocol.initialize(config_path, baseline_path)
    provenance = {"kind": "r015_four_task_diagnostic_provenance_v1",
        "source_config": {"path": str(source_config), "sha256": sha256_file(source_config)},
        "source_baseline": {"path": str(source_baseline), "sha256": sha256_file(source_baseline)},
        "diagnostic_config": {"path": str(config_path), "sha256": sha256_file(config_path)},
        "diagnostic_baseline": {"path": str(baseline_path), "sha256": sha256_file(baseline_path)},
        "task_order": list(TASK_IDS), "max_trials": 4,
        "runtime_approval_reference": runtime_approval_reference.strip()}
    _write_once(run_dir / PROVENANCE_NAME, provenance)
    return provenance


def _load_scope(run_dir: Path) -> tuple[Path, Path, dict[str, Any]]:
    provenance = read_json(run_dir / PROVENANCE_NAME)
    config_path = run_dir / CONFIG_NAME
    baseline_path = run_dir / BASELINE_NAME
    for field, path in (("diagnostic_config", config_path),
                        ("diagnostic_baseline", baseline_path)):
        if provenance[field] != {"path": str(path), "sha256": sha256_file(path)}:
            raise COnlyProtocolError(f"diagnostic {field} differs from prepared provenance")
    config = read_json(config_path)
    scope = config.get("diagnostic_scope", {})
    if (scope.get("task_order") != list(TASK_IDS) or scope.get("max_trials") != 4 or
            scope.get("stop_before_round_2") is not True or
            [item["canonical_instance_id"] for item in config["tasks"]] != list(TASK_IDS)):
        raise COnlyProtocolError("diagnostic scope is not exactly the approved four tasks")
    return config_path, baseline_path, provenance


def _assert_first_round_stop(protocol: COnlyProtocol) -> dict[str, Any]:
    round_one = protocol.state["rounds"]["1"]
    round_two = protocol.state["rounds"]["2"]
    completed = list(round_one["completed_tasks"])
    if (protocol.current_round_id != 1 or protocol.current_task_id is not None or
            round_one["status"] != "complete" or completed != list(TASK_IDS) or
            round_two["status"] != "not_started" or round_two["completed_tasks"] or
            round_two["trajectory_pool"] or round_two["bank"]["skills"] or
            protocol.state["formal_campaign"] != "active"):
        raise COnlyProtocolError("diagnostic did not stop after exactly four first-round tasks")
    return {"status": "diagnostic_first_round_complete", "completed_tasks": completed,
            "current_round": 1, "round_2_status": round_two["status"],
            "bank_state_sha256": SkillBank.from_dict(round_one["bank"]).snapshot()["state_sha256"]}


def run(*, run_dir: Path, trial_driver: Path, confirm_user_start: bool,
        accept_runtime_deviation: bool,
        allow_test_fixture: bool = False,
        continue_from_extraction: bool = False) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    if not confirm_user_start or not accept_runtime_deviation:
        raise COnlyProtocolError("diagnostic launch requires explicit start and runtime-deviation flags")
    config_path, baseline_path, provenance = _load_scope(run_dir)
    trial_driver = trial_driver.resolve()
    if not trial_driver.is_file():
        raise COnlyProtocolError("diagnostic official trial driver is absent")
    state_path = run_dir / STATE_NAME
    protocol = (COnlyProtocol.load(state_path, config_path, baseline_path)
                if state_path.exists() else
                COnlyProtocol.initialize(config_path, baseline_path, state_path))
    if protocol.current_round_id != 1 or protocol.state["rounds"]["2"]["status"] != "not_started":
        raise COnlyProtocolError("diagnostic cannot enter or resume round 2")
    coordinator._ensure_runtime_parity_gate(protocol,
        accept_runtime_deviation=True, state_path=state_path)
    if protocol.state["formal_campaign"] == "not_started":
        protocol.authorize_start()
        protocol.save(state_path)
    elif protocol.state["formal_campaign"] != "active":
        raise COnlyProtocolError("diagnostic state has an unexpected campaign status")
    with coordinator._execution_lock(run_dir, state_path=state_path):
        coordinator._run_driver(protocol, trial_driver, run_dir, state_path,
                                allow_test_fixture=allow_test_fixture,
                                continue_from_extraction=continue_from_extraction,
                                stop_after_first_round=True)
    stopped = _assert_first_round_stop(protocol)
    completion = {**stopped,
        "kind": "r015_four_task_diagnostic_completion_v1",
        "state": {"path": str(state_path), "sha256": sha256_file(state_path)},
        "provenance": {"path": str(run_dir / PROVENANCE_NAME),
                       "sha256": sha256_file(run_dir / PROVENANCE_NAME)},
        "runtime_approval_reference": provenance["runtime_approval_reference"]}
    _write_once(run_dir / "diagnostic-completion.json", completion)
    return completion


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--source-config", type=Path, required=True)
    prep.add_argument("--source-baseline", type=Path, required=True)
    prep.add_argument("--run-dir", type=Path, required=True)
    prep.add_argument("--runtime-approval-reference", required=True)
    launch = sub.add_parser("run")
    launch.add_argument("--run-dir", type=Path, required=True)
    launch.add_argument("--trial-driver", type=Path,
                        default=ROOT / "scripts" / "run_r015_c_only_harbor_driver.py")
    launch.add_argument("--confirm-user-start", action="store_true")
    launch.add_argument("--accept-runtime-deviation", action="store_true")
    launch.add_argument("--continue-from-extraction", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(source_config=args.source_config,
                         source_baseline=args.source_baseline, run_dir=args.run_dir,
                         runtime_approval_reference=args.runtime_approval_reference)
    else:
        result = run(run_dir=args.run_dir, trial_driver=args.trial_driver,
                     confirm_user_start=args.confirm_user_start,
                     accept_runtime_deviation=args.accept_runtime_deviation,
                     continue_from_extraction=args.continue_from_extraction)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (COnlyProtocolError, OSError, ValueError, KeyError) as error:
        raise SystemExit(f"{type(error).__name__}: {error}") from error
