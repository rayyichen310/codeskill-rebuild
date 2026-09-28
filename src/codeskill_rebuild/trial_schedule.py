"""R012 arm/repeat bank-freeze gate for one evaluation instance."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .bank import SkillBank
from .types import canonical_instance_id


class TrialScheduleError(RuntimeError):
    pass


@dataclass
class InstanceBankFreeze:
    """Freeze each arm's bank before an instance, then release updates once."""

    arm_banks: dict[str, SkillBank]
    repeat_ids: tuple[str, ...]
    instances: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.arm_banks or not all(isinstance(arm, str) and arm and isinstance(bank, SkillBank) for arm, bank in self.arm_banks.items()):
            raise TrialScheduleError("arm_banks must map nonempty arm names to SkillBank instances")
        if not self.repeat_ids or len(set(self.repeat_ids)) != len(self.repeat_ids) or not all(isinstance(item, str) and item for item in self.repeat_ids):
            raise TrialScheduleError("repeat_ids must be unique nonempty strings")
        # Separate copies make a mistakenly shared mutable bank impossible at
        # the coordinator boundary, even when two arms begin byte-identical.
        self.arm_banks = {arm: SkillBank.from_dict(bank.to_dict()) for arm, bank in self.arm_banks.items()}

    def freeze(self, instance_id: str) -> list[dict[str, Any]]:
        canonical = canonical_instance_id(instance_id)
        if canonical in self.instances:
            raise TrialScheduleError(f"instance {canonical} is already frozen")
        pending = [name for name, group in self.instances.items() if not group.get("released")]
        if pending:
            raise TrialScheduleError(
                "cannot freeze a later instance while an earlier instance still has unreleased or blocked updates: "
                + ", ".join(sorted(pending))
            )
        assignments: dict[str, dict[str, Any]] = {}
        for arm in sorted(self.arm_banks):
            snapshot = self.arm_banks[arm].snapshot()
            for repeat_id in self.repeat_ids:
                trial_id = f"{canonical}:{arm}:{repeat_id}"
                assignments[trial_id] = {
                    "trial_id": trial_id,
                    "instance_id": canonical,
                    "arm": arm,
                    "repeat_id": repeat_id,
                    "frozen_bank": deepcopy(snapshot),
                    "status": "pending",
                }
        self.instances[canonical] = {"assignments": assignments, "released": False, "release_state": "pending"}
        return [deepcopy(assignments[key]) for key in sorted(assignments)]

    def trial_bank(self, trial_id: str) -> SkillBank:
        assignment = self._assignment(trial_id)
        return SkillBank.from_dict(assignment["frozen_bank"])

    def finish(self, trial_id: str, *, result_evidence: dict[str, Any]) -> None:
        assignment = self._assignment(trial_id)
        if assignment["status"] != "pending":
            raise TrialScheduleError(f"trial {trial_id} was already finished")
        assignment["status"] = "finished"
        assignment["result_evidence"] = deepcopy(result_evidence)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the authoritative, all-arm coordinator state atomically.

        A release updates every arm in memory first.  Persisting this single
        object with :func:`write_json` gives the runner one crash boundary,
        rather than publishing one mutable bank file at a time.
        """
        return {
            "schema_version": 1,
            "kind": "r012_instance_bank_freeze",
            "arm_banks": {arm: bank.to_dict() for arm, bank in sorted(self.arm_banks.items())},
            "repeat_ids": list(self.repeat_ids),
            "instances": deepcopy(self.instances),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InstanceBankFreeze":
        if not isinstance(value, dict) or value.get("kind") != "r012_instance_bank_freeze":
            raise TrialScheduleError("invalid R012 instance bank-freeze state")
        raw_banks = value.get("arm_banks")
        raw_repeats = value.get("repeat_ids")
        raw_instances = value.get("instances")
        if not isinstance(raw_banks, dict) or not isinstance(raw_repeats, list) or not isinstance(raw_instances, dict):
            raise TrialScheduleError("incomplete R012 instance bank-freeze state")
        if not all(isinstance(arm, str) and isinstance(payload, dict) for arm, payload in raw_banks.items()):
            raise TrialScheduleError("R012 instance bank-freeze state has invalid arm banks")
        restored = cls(
            {arm: SkillBank.from_dict(payload) for arm, payload in raw_banks.items()},
            repeat_ids=tuple(raw_repeats),
        )
        restored.instances = deepcopy(raw_instances)
        for instance_id, group in restored.instances.items():
            if not isinstance(instance_id, str) or not isinstance(group, dict):
                raise TrialScheduleError("R012 instance bank-freeze state has invalid instance records")
            assignments = group.get("assignments")
            if not isinstance(assignments, dict) or not isinstance(group.get("release_state"), str):
                raise TrialScheduleError("R012 instance bank-freeze state has invalid assignments")
        return restored

    def release_updates(
        self,
        instance_id: str,
        *,
        ordered_trial_ids: Iterable[str],
        apply_update: Callable[[str, SkillBank, dict[str, Any]], Any],
    ) -> list[dict[str, Any]]:
        """Apply updates only after every arm/repeat has finished.

        The caller must provide an explicit order because maintenance ordering
        is an experimental setting, never an implicit cross-seed side effect.
        """
        canonical = canonical_instance_id(instance_id)
        group = self.instances.get(canonical)
        if not isinstance(group, dict):
            raise TrialScheduleError(f"instance {canonical} was not frozen")
        if group["release_state"] != "pending":
            raise TrialScheduleError(
                f"updates for instance {canonical} are {group['release_state']}; automatic replay is forbidden"
            )
        assignments = group["assignments"]
        if any(value["status"] != "finished" for value in assignments.values()):
            raise TrialScheduleError("cannot release updates before every arm and repeat of the instance finishes")
        order = list(ordered_trial_ids)
        if set(order) != set(assignments) or len(order) != len(assignments):
            raise TrialScheduleError("ordered_trial_ids must contain every frozen trial exactly once")
        staged_banks = {arm: SkillBank.from_dict(bank.to_dict()) for arm, bank in self.arm_banks.items()}
        results: list[dict[str, Any]] = []
        try:
            for trial_id in order:
                assignment = assignments[trial_id]
                result = apply_update(trial_id, staged_banks[assignment["arm"]], deepcopy(assignment))
                results.append({"trial_id": trial_id, "arm": assignment["arm"], "update_result": deepcopy(result)})
        except BaseException as error:
            # The callback may have issued a manager call before failing.  Do
            # not replay those calls or expose a partly staged bank.  A human
            # must reconcile the durable per-trial evidence before any new
            # release plan is allowed.
            group["release_state"] = "blocked_after_callback_error"
            group["completed_release_evidence"] = deepcopy(results)
            group["release_error"] = {"error_type": type(error).__name__, "error": str(error)}
            raise TrialScheduleError(
                "release callback failed after staged updates; automatic replay is forbidden"
            ) from error
        self.arm_banks = staged_banks
        group["released"] = True
        group["release_state"] = "released"
        group["release_order"] = order
        group["released_updates"] = deepcopy(results)
        return results

    def release_instance_transaction(
        self,
        instance_id: str,
        *,
        ordered_trial_ids: Iterable[str],
        apply_transaction: Callable[[dict[str, SkillBank], dict[str, Any]], Any],
    ) -> Any:
        """Publish one staged all-arm release after every assignment finishes.

        This supports the development-only cross-arm action in which a
        completed Arm A trajectory produces shared candidates for *later*
        instances' B/C banks.  The callback never sees live banks, so no
        same-instance trial can observe its outputs.  A failure leaves every
        live bank unchanged and blocks automatic replay, just as a per-trial
        manager callback does in :meth:`release_updates`.
        """
        canonical = canonical_instance_id(instance_id)
        group = self.instances.get(canonical)
        if not isinstance(group, dict):
            raise TrialScheduleError(f"instance {canonical} was not frozen")
        if group["release_state"] != "pending":
            raise TrialScheduleError(
                f"updates for instance {canonical} are {group['release_state']}; automatic replay is forbidden"
            )
        assignments = group["assignments"]
        order = list(ordered_trial_ids)
        if set(order) != set(assignments) or len(order) != len(assignments):
            raise TrialScheduleError("ordered_trial_ids must contain every frozen trial exactly once")
        if any(assignments[trial_id]["status"] != "finished" for trial_id in order):
            raise TrialScheduleError("cannot release updates before every arm and repeat of the instance finishes")
        staged_banks = {arm: SkillBank.from_dict(bank.to_dict()) for arm, bank in self.arm_banks.items()}
        try:
            result = apply_transaction(staged_banks, deepcopy(group))
        except BaseException as error:
            group["release_state"] = "blocked_after_transaction_error"
            group["release_error"] = {"error_type": type(error).__name__, "error": str(error)}
            raise TrialScheduleError(
                "release transaction failed; automatic replay is forbidden"
            ) from error
        self.arm_banks = staged_banks
        group["released"] = True
        group["release_state"] = "released"
        group["release_order"] = order
        group["released_updates"] = deepcopy(result)
        return deepcopy(result)

    def _assignment(self, trial_id: str) -> dict[str, Any]:
        for group in self.instances.values():
            assignment = group.get("assignments", {}).get(trial_id)
            if isinstance(assignment, dict):
                return assignment
        raise TrialScheduleError(f"unknown frozen trial {trial_id}")
