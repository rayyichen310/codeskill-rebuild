"""Run the durable local R012 freeze/finish/release lifecycle.

This runner is intentionally split into three explicit commands.  It never
launches OpenClaw or a solver.  ``release`` can call the manager only with
``--execute-manager`` and an explicit profile plus selection manifest.  A
Full Lifecycle trial with a durably supplied skill requires that path; a
non-evolution arm or durable no-supplied evidence does not.  An interrupted
call leaves a pre-call journal and blocks automatic replay.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
from typing import Any

from codeskill_rebuild.bank import SkillBank
from codeskill_rebuild.manager import ManagerClient, ManagerProfile, ServerMessageTokenCounter, update_development_ledger_limit
from codeskill_rebuild.r012_execution import (
    R012EvolutionMaintenanceExecutor,
    R012ExecutionError,
    inspect_full_lifecycle_evidence,
    profile_sha256,
    validate_execution_profile,
    validate_selection_manifest,
)
from codeskill_rebuild.r012_runtime import R012Runtime
from codeskill_rebuild.retrieval import MiniLMEncoder
from codeskill_rebuild.trial_schedule import InstanceBankFreeze
from codeskill_rebuild.types import (
    canonical_instance_id,
    contract_from_files,
    read_json,
    sha256_file,
    utc_now,
    write_contract_snapshot,
    write_json,
)


def _arm_bank(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("--arm-bank needs ARM=PATH")
    arm, raw_path = value.split("=", 1)
    if not arm or not raw_path:
        raise argparse.ArgumentTypeError("--arm-bank needs a nonempty ARM and PATH")
    return arm, Path(raw_path)


def _state(state_path: Path) -> dict[str, Any]:
    value = read_json(state_path)
    if not isinstance(value, dict) or value.get("kind") != "r012_instance_lifecycle_state":
        raise R012ExecutionError("invalid R012 lifecycle state")
    profile = validate_execution_profile(value.get("profile"))
    if value.get("profile_sha256") != profile_sha256(profile):
        raise R012ExecutionError("R012 lifecycle state profile hash does not match profile content")
    if not isinstance(value.get("coordinator"), dict):
        raise R012ExecutionError("R012 lifecycle state lacks coordinator")
    return value


def _save_state(path: Path, value: dict[str, Any]) -> None:
    output = deepcopy(value)
    output["updated_at_utc"] = utc_now()
    write_json(path, output)


def _evidence_directory(state_path: Path, trial_id: str) -> Path:
    from codeskill_rebuild.types import sha256_text

    return state_path.parent / f"{state_path.stem}-evidence" / "trials" / sha256_text(trial_id)[:20]


def _copy_json(path: Path, destination: Path) -> dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise R012ExecutionError(f"evidence input must be a JSON object: {path}")
    write_json(destination, value)
    return {"path": str(destination), "sha256": sha256_file(destination), "value": value}


def _read_json_object(path: Path) -> dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise R012ExecutionError(f"evidence input must be a JSON object: {path}")
    return value


def _copy_json_value(value: dict[str, Any], *, source: Path, destination: Path) -> dict[str, Any]:
    write_json(destination, value)
    return {"path": str(destination), "sha256": sha256_file(destination), "source_path": str(source), "source_sha256": sha256_file(source), "value": value}


def freeze(args: argparse.Namespace) -> None:
    profile = validate_execution_profile(read_json(args.profile))
    profile_hash = profile_sha256(profile)
    if args.state.exists():
        if args.arm_bank:
            raise R012ExecutionError("existing lifecycle state already owns its live arm banks; do not replace them")
        state = _state(args.state)
        if state["profile_sha256"] != profile_hash:
            raise R012ExecutionError("cannot advance lifecycle state under a different R012 profile")
        coordinator = InstanceBankFreeze.from_dict(state["coordinator"])
        if tuple(args.repeat) != coordinator.repeat_ids:
            raise R012ExecutionError("cannot advance lifecycle state with a different repeat list")
        if contract_from_files(args.spec, args.decisions) != state["contract"]:
            raise R012ExecutionError("cannot advance lifecycle state after the frozen contract changed")
    else:
        if not args.arm_bank:
            raise R012ExecutionError("first freeze needs one --arm-bank per arm")
        banks: dict[str, SkillBank] = {}
        for arm, path in args.arm_bank:
            if arm in banks:
                raise R012ExecutionError(f"duplicate arm {arm}")
            banks[arm] = SkillBank.load(path)
        missing_lifecycle_arms = set(profile["evolution"]["full_lifecycle_arms"]) - set(banks)
        if missing_lifecycle_arms:
            raise R012ExecutionError(
                "execution profile names lifecycle arms absent from --arm-bank: " + ", ".join(sorted(missing_lifecycle_arms))
            )
        coordinator = InstanceBankFreeze(banks, repeat_ids=tuple(args.repeat))
        state = {
            "schema_version": 1,
            "kind": "r012_instance_lifecycle_state",
            "created_at_utc": utc_now(),
            "profile": profile,
            "profile_sha256": profile_hash,
            "profile_source": {"path": str(args.profile), "sha256": sha256_file(args.profile)},
            "contract": contract_from_files(args.spec, args.decisions),
        }
        state["contract_snapshot"] = write_contract_snapshot(args.state.parent / f"{args.state.stem}-contract", args.spec, args.decisions, state["contract"])
    assignments = coordinator.freeze(args.instance_id)
    state["coordinator"] = coordinator.to_dict()
    state["last_action"] = {
        "action": "freeze",
        "instance_id": canonical_instance_id(args.instance_id),
        "assignments": assignments,
    }
    _save_state(args.state, state)


def finish(args: argparse.Namespace) -> None:
    state = _state(args.state)
    coordinator = InstanceBankFreeze.from_dict(state["coordinator"])
    assignment = coordinator._assignment(args.trial_id)
    if assignment.get("status") != "pending":
        raise R012ExecutionError(f"{args.trial_id}: finished evidence already exists; refusing to overwrite it")
    directory = _evidence_directory(args.state, args.trial_id)
    if directory.exists():
        raise R012ExecutionError(f"{args.trial_id}: existing evidence directory requires manual reconciliation")
    trial_result_value = _read_json_object(args.result_evidence)
    stated_trial_id = trial_result_value.get("trial_id")
    if stated_trial_id is not None and stated_trial_id != args.trial_id:
        raise R012ExecutionError(f"{args.trial_id}: result evidence belongs to {stated_trial_id}")
    attempt_values = [(path, _read_json_object(path)) for path in args.proxy_attempt]
    for index, (_path, value) in enumerate(attempt_values, start=1):
        if value.get("trial_id") != args.trial_id:
            raise R012ExecutionError(f"{args.trial_id}: proxy attempt {index} belongs to a different trial")
    trajectory_value = _read_json_object(args.trajectory_evidence) if args.trajectory_evidence else None
    if trajectory_value is not None:
        source = trajectory_value.get("source")
        expected_instance = assignment.get("instance_id")
        if not isinstance(source, dict) or not isinstance(source.get("canonical_instance_id"), str):
            raise R012ExecutionError(f"{args.trial_id}: trajectory evidence needs source.canonical_instance_id")
        if canonical_instance_id(source["canonical_instance_id"]) != expected_instance:
            raise R012ExecutionError(f"{args.trial_id}: trajectory source differs from its frozen instance")
    # Every input is validated before any copy.  This prevents a rejected
    # second finish or cross-trial artifact from replacing durable evidence.
    trial_result = _copy_json_value(trial_result_value, source=args.result_evidence, destination=directory / "trial-result.json")
    attempts = [
        _copy_json_value(value, source=path, destination=directory / "proxy-attempts" / f"attempt-{index:04d}.json")
        for index, (path, value) in enumerate(attempt_values, start=1)
    ]
    trajectory = (
        _copy_json_value(trajectory_value, source=args.trajectory_evidence, destination=directory / "trajectory-evidence.json")
        if trajectory_value is not None and args.trajectory_evidence is not None
        else None
    )
    evidence = {
        "kind": "r012_finished_trial_evidence",
        "trial_result": {key: value for key, value in trial_result.items() if key != "value"},
        "trial_result_value": trial_result["value"],
        "proxy_attempt_record_refs": [{key: value for key, value in item.items() if key != "value"} for item in attempts],
        "proxy_attempt_records": [item["value"] for item in attempts],
        "trajectory_evidence_ref": {key: value for key, value in trajectory.items() if key != "value"} if trajectory else None,
        "trajectory_evidence": trajectory["value"] if trajectory else None,
    }
    coordinator.finish(args.trial_id, result_evidence=evidence)
    state["coordinator"] = coordinator.to_dict()
    state["last_action"] = {"action": "finish", "trial_id": args.trial_id, "assignment": assignment}
    _save_state(args.state, state)


def release(args: argparse.Namespace) -> None:
    state = _state(args.state)
    coordinator = InstanceBankFreeze.from_dict(state["coordinator"])
    instance_id = canonical_instance_id(args.instance_id)
    group = coordinator.instances.get(instance_id)
    if not isinstance(group, dict):
        raise R012ExecutionError("requested instance is not frozen in this lifecycle state")
    selections_source = read_json(args.selection_manifest)
    selection = validate_selection_manifest(
        selections_source,
        instance_id=instance_id,
        trial_ids=group["assignments"],
        expected_profile_sha256=state["profile_sha256"],
    )
    selections = selection["selections"]
    order = selection["release_order"]
    full_arms = set(state["profile"]["evolution"]["full_lifecycle_arms"])
    manager_required_trials: list[str] = []
    for trial_id in order:
        assignment = group["assignments"][trial_id]
        action = selections[trial_id]["action"]
        if assignment["arm"] not in full_arms:
            if action != "skip":
                raise R012ExecutionError(f"{trial_id}: a non-lifecycle arm must use the explicit skip action")
            continue
        if action != "evaluate_all_supplied":
            raise R012ExecutionError(
                f"{trial_id}: a Full Lifecycle trial must evaluate_all_supplied; manual evolution skip is not a release option"
            )
        evidence = inspect_full_lifecycle_evidence(
            trial_id=trial_id,
            instance_id=instance_id,
            result_evidence=assignment.get("result_evidence"),
        )
        if evidence["supplied"]:
            manager_required_trials.append(trial_id)
    manager_needed = bool(manager_required_trials)
    if manager_needed and not args.execute_manager:
        raise R012ExecutionError(
            "a Full Lifecycle trial has actually supplied skill evidence and requires explicit --execute-manager; no manager call was made"
        )
    manager: ManagerClient | None = None
    encoder: MiniLMEncoder | None = None
    evolution_prompt: str | None = None
    maintenance_prompt: str | None = None
    if manager_needed:
        required = (args.config, args.ledger, args.evolution_prompt, args.maintenance_prompt, args.minilm_revision)
        if any(item is None for item in required):
            raise R012ExecutionError("manager release requires config, ledger, both prompts, and an explicit MiniLM revision")
        if not args.activate_unlimited_development_ledger:
            raise R012ExecutionError(
                "manager release requires --activate-unlimited-development-ledger; no finite ledger is silently changed"
            )
        service = read_json(args.config)["services"]["deepseek_flash"]
        profile = ManagerProfile(base_url=service["base_url"], model=service["model_id"], max_total_calls=None)
        ledger = update_development_ledger_limit(
            args.ledger,
            new_limit=None,
            reason="R014 explicit unlimited-development-ledger activation for R012 evolution/maintenance",
            contract=state["contract"],
        )
        write_json(
            args.state.parent / f"{args.state.stem}-manager-ledger-activation.json",
            {
                "kind": "r014_unlimited_development_ledger_activation",
                "ledger_path": str(args.ledger),
                "ledger_limit": ledger["limit"],
                "calls_preserved": len(ledger["calls"]),
                "limit_history": ledger.get("limit_history", []),
            },
        )
        manager = ManagerClient(
            profile,
            args.state.parent / f"{args.state.stem}-manager-calls",
            state["contract"],
            args.ledger,
            exact_token_counter=ServerMessageTokenCounter(profile.base_url),
        )
        encoder = MiniLMEncoder(revision=args.minilm_revision)
        encoder.load()
        evolution_prompt = args.evolution_prompt.read_text(encoding="utf-8")
        maintenance_prompt = args.maintenance_prompt.read_text(encoding="utf-8")
    executor = R012EvolutionMaintenanceExecutor(
        manager=manager,
        encoder=encoder,
        journal_root=args.state.parent / f"{args.state.stem}-journals",
        instance_id=instance_id,
        profile=state["profile"],
        selections=selections,
        evolution_prompt=evolution_prompt,
        maintenance_prompt=maintenance_prompt,
    )
    try:
        released = R012Runtime(coordinator).release_instance_updates(
            instance_id,
            ordered_trial_ids=order,
            apply_update=executor.apply_update,
        )
    except BaseException:
        # If a callback has made a pre-call journal or received a manager
        # result, this exact state records a block before the command returns.
        # If the process crashes before this write, the journal itself still
        # blocks replay on the next invocation.
        state["coordinator"] = coordinator.to_dict()
        state["last_action"] = {"action": "release", "instance_id": instance_id, "status": "blocked_or_failed"}
        _save_state(args.state, state)
        raise
    state["coordinator"] = coordinator.to_dict()
    state["last_action"] = {
        "action": "release",
        "instance_id": instance_id,
        "status": "released",
        "selection_manifest": {"path": str(args.selection_manifest), "sha256": sha256_file(args.selection_manifest)},
        "updates": released,
    }
    _save_state(args.state, state)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    actions = value.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state", type=Path, required=True)
    freeze_parser = actions.add_parser("freeze", parents=[common])
    freeze_parser.add_argument("--profile", type=Path, required=True)
    freeze_parser.add_argument("--instance-id", required=True)
    freeze_parser.add_argument("--repeat", action="append", required=True)
    freeze_parser.add_argument("--arm-bank", type=_arm_bank, action="append", default=[])
    freeze_parser.add_argument("--spec", type=Path, required=True)
    freeze_parser.add_argument("--decisions", type=Path, required=True)
    freeze_parser.set_defaults(func=freeze)
    finish_parser = actions.add_parser("finish", parents=[common])
    finish_parser.add_argument("--trial-id", required=True)
    finish_parser.add_argument("--result-evidence", type=Path, required=True)
    finish_parser.add_argument("--proxy-attempt", type=Path, action="append", default=[])
    finish_parser.add_argument("--trajectory-evidence", type=Path)
    finish_parser.set_defaults(func=finish)
    release_parser = actions.add_parser("release", parents=[common])
    release_parser.add_argument("--instance-id", required=True)
    release_parser.add_argument("--selection-manifest", type=Path, required=True)
    release_parser.add_argument("--execute-manager", action="store_true")
    release_parser.add_argument("--activate-unlimited-development-ledger", action="store_true")
    release_parser.add_argument("--config", type=Path)
    release_parser.add_argument("--ledger", type=Path)
    release_parser.add_argument("--evolution-prompt", type=Path)
    release_parser.add_argument("--maintenance-prompt", type=Path)
    release_parser.add_argument("--minilm-revision")
    release_parser.set_defaults(func=release)
    return value


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
