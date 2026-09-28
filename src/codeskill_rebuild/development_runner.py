"""Fail-closed release wiring for the bounded R012 development runner.

The runner deliberately separates two time boundaries:

* all A/B/C assignments for instance ``i`` are frozen before any starts;
* only after all of them have durable finish evidence may Arm A's extracted
  candidates update B/C and Arm C's actually supplied skills enter Fig.8/9.

Consequently a skill learned from Arm A of ``i`` can first be retrieved by an
assignment frozen for ``i + 1``.  This is the key leakage boundary that a
generic shell launcher cannot safely provide.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable

from .arm_banks import exact_skill_schema, group_exact_candidates
from .bank import SkillBank
from .r012_execution import (
    R012EvolutionMaintenanceExecutor,
    R012ExecutionError,
    profile_sha256,
    validate_execution_profile,
    validate_selection_manifest,
)
from .trial_schedule import InstanceBankFreeze, TrialScheduleError
from .types import canonical_instance_id, canonical_json, sha256_text


class DevelopmentRunnerError(RuntimeError):
    """The bounded development runner lacks required evidence or policy input."""


@dataclass(frozen=True)
class DevelopmentArms:
    """The three named arms used by the approved development integration."""

    baseline: str = "A"
    extraction: str = "B"
    full_lifecycle: str = "C"

    def all(self) -> tuple[str, str, str]:
        return (self.baseline, self.extraction, self.full_lifecycle)


def _candidate_records(value: Any) -> list[dict[str, Any]]:
    """Read the one explicit, already-produced Arm A extraction payload."""
    if isinstance(value, dict):
        value = value.get("candidate_records", value.get("records", value.get("candidates")))
    if not isinstance(value, list):
        raise DevelopmentRunnerError("Arm A shared extraction needs a candidate_records list")
    if not all(isinstance(item, dict) for item in value):
        raise DevelopmentRunnerError("Arm A candidate_records must contain objects only")
    return deepcopy(value)


def validate_arm_a_candidate_records(value: Any, *, instance_id: str) -> list[dict[str, Any]]:
    """Require every released candidate to be grounded in completed Arm A.

    A candidate may carry multiple raw aliases, but after canonicalization each
    source must be exactly the instance whose Arm A trace was just finished.
    This rejects historical-source substitution, a B/C self-update, and an
    accidental held-out trace before any bank mutation is staged.
    """
    canonical = canonical_instance_id(instance_id)
    records = _candidate_records(value)
    checked: list[dict[str, Any]] = []
    for ordinal, record in enumerate(records, start=1):
        try:
            skill = exact_skill_schema(record.get("skill"))
        except Exception as error:
            raise DevelopmentRunnerError(f"Arm A candidate {ordinal} has an invalid skill schema: {error}") from error
        source_ids = record.get("source_instance_ids")
        raw_ids = record.get("source_instance_ids_raw", source_ids)
        if not isinstance(source_ids, list) or not source_ids or not all(isinstance(item, str) and item for item in source_ids):
            raise DevelopmentRunnerError(f"Arm A candidate {ordinal} needs nonempty source_instance_ids")
        if not isinstance(raw_ids, list) or not raw_ids or not all(isinstance(item, str) and item for item in raw_ids):
            raise DevelopmentRunnerError(f"Arm A candidate {ordinal} needs nonempty source_instance_ids_raw")
        canonical_sources = {canonical_instance_id(item) for item in source_ids}
        if canonical_sources != {canonical}:
            raise DevelopmentRunnerError(
                f"Arm A candidate {ordinal} must cite only completed instance {canonical}, got {sorted(canonical_sources)}"
            )
        candidate_record = record.get("candidate_record")
        if candidate_record is not None and not isinstance(candidate_record, dict):
            raise DevelopmentRunnerError(f"Arm A candidate {ordinal} candidate_record must be an object when present")
        checked.append(
            {
                "skill": skill,
                "source_instance_ids": sorted(canonical_sources),
                "source_instance_ids_raw": sorted(set(raw_ids)),
                "candidate_record": deepcopy(candidate_record) if isinstance(candidate_record, dict) else {"ordinal": ordinal},
            }
        )
    return checked


def _direct_ingest_arm_b_candidates(
    bank: SkillBank,
    *,
    candidate_records: Any,
    instance_id: str,
) -> dict[str, Any]:
    """Deterministically ingest Arm A candidates into Arm B only.

    This is R010's Extraction Only arm: it never asks the maintenance model.
    Exact-content duplicates preserve their new provenance through a
    deterministic bank merge, which is explicitly recorded as ingestion and
    never represented as a Fig.9 decision.
    """
    checked = validate_arm_a_candidate_records(candidate_records, instance_id=instance_id)
    grouped = group_exact_candidates(checked)
    evidence_groups: list[dict[str, Any]] = []
    for group in grouped:
        evidence_groups.append(
            {
                "exact_schema_sha256": group["exact_schema_sha256"],
                "source_instance_ids": deepcopy(group["source_instance_ids"]),
                "source_instance_ids_raw": deepcopy(group["source_instance_ids_raw"]),
                "candidate_records": deepcopy(group["candidate_records"]),
            }
        )
    operations: list[dict[str, Any]] = []
    for ordinal, group in enumerate(grouped, start=1):
        candidate = exact_skill_schema(group["skill"])
        if candidate["benchmark"] != bank.benchmark:
            raise DevelopmentRunnerError(
                f"Arm A candidate benchmark {candidate['benchmark']!r} differs from B bank {bank.benchmark!r}"
            )
        candidate["provenance"] = {
            "source_instance_ids": deepcopy(group["source_instance_ids"]),
            "source_instance_ids_raw": deepcopy(group["source_instance_ids_raw"]),
            "parent_skill_ids": [],
        }
        identical = [
            skill for skill in bank.skills
            if skill.get("status") == "active" and canonical_json(exact_skill_schema(skill)) == canonical_json(exact_skill_schema(candidate))
        ]
        if len(identical) > 1:
            raise DevelopmentRunnerError("B bank has multiple active exact-content duplicates; deterministic ingestion is ambiguous")
        decision = "merge" if identical else "add"
        operation = bank.apply(
            operation_id=f"r015-arm-b-direct-{canonical_instance_id(instance_id)}-{ordinal:03d}-{group['exact_schema_sha256'][:16]}",
            decision=decision,
            candidate=candidate,
            source_instance_ids=group["source_instance_ids"],
            merge_target_id=identical[0]["skill_id"] if identical else None,
            evidence={
                "kind": "r015_arm_b_deterministic_extraction_only_ingestion",
                "source_arm": "A",
                "source_instance_id": canonical_instance_id(instance_id),
                "exact_schema_sha256": group["exact_schema_sha256"],
                "candidate_records": deepcopy(group["candidate_records"]),
                "source_instance_ids_raw": deepcopy(group["source_instance_ids_raw"]),
                "exact_duplicate_of": identical[0]["skill_id"] if identical else None,
            },
        )
        operations.append(operation)
    return {
        "kind": "r015_arm_b_direct_extraction_release",
        "source_arm": "A",
        "source_instance_id": canonical_instance_id(instance_id),
        "candidate_content_groups": evidence_groups,
        "target_arm": "B",
        "operations": operations,
    }


def _maintain_arm_c_candidates(
    bank: SkillBank,
    *,
    candidate_records: Any,
    instance_id: str,
    c_trial_id: str,
    evolution_executor: R012EvolutionMaintenanceExecutor,
) -> dict[str, Any]:
    """Apply every Arm A candidate to C through a distinct real Fig.9 call."""
    checked = validate_arm_a_candidate_records(candidate_records, instance_id=instance_id)
    grouped = group_exact_candidates(checked)
    operations: list[dict[str, Any]] = []
    for ordinal, group in enumerate(grouped, start=1):
        candidate = exact_skill_schema(group["skill"])
        if candidate["benchmark"] != bank.benchmark:
            raise DevelopmentRunnerError(
                f"Arm A candidate benchmark {candidate['benchmark']!r} differs from C bank {bank.benchmark!r}"
            )
        candidate["provenance"] = {
            "source_instance_ids": deepcopy(group["source_instance_ids"]),
            "source_instance_ids_raw": deepcopy(group["source_instance_ids_raw"]),
            "parent_skill_ids": [],
        }
        operations.append(
            evolution_executor.apply_extracted_candidate_maintenance(
                trial_id=c_trial_id,
                bank=bank,
                candidate=candidate,
                candidate_ordinal=ordinal,
                candidate_evidence={
                    "kind": "r015_arm_a_candidate_for_c_fig9",
                    "source_arm": "A",
                    "source_instance_id": canonical_instance_id(instance_id),
                    "exact_schema_sha256": group["exact_schema_sha256"],
                    "candidate_records": deepcopy(group["candidate_records"]),
                    "source_instance_ids_raw": deepcopy(group["source_instance_ids_raw"]),
                },
            )
        )
    return {
        "kind": "r015_arm_c_fig9_candidate_release",
        "source_arm": "A",
        "source_instance_id": canonical_instance_id(instance_id),
        "candidate_content_groups": [
            {
                "exact_schema_sha256": group["exact_schema_sha256"],
                "source_instance_ids": deepcopy(group["source_instance_ids"]),
                "source_instance_ids_raw": deepcopy(group["source_instance_ids_raw"]),
                "candidate_records": deepcopy(group["candidate_records"]),
            }
            for group in grouped
        ],
        "target_arm": "C",
        "operations": operations,
    }


def _assignment_id(instance_id: str, arm: str, repeat: str) -> str:
    return f"{canonical_instance_id(instance_id)}:{arm}:{repeat}"


def release_development_instance(
    coordinator: InstanceBankFreeze,
    *,
    instance_id: str,
    repeat_id: str,
    profile: dict[str, Any],
    selection_manifest: dict[str, Any],
    arm_a_candidate_records: Any,
    evolution_executor: R012EvolutionMaintenanceExecutor,
    arms: DevelopmentArms = DevelopmentArms(),
) -> dict[str, Any]:
    """Release one completed A/B/C development instance as a single commit.

    The caller must have finished all assignments first.  The current frozen
    snapshots are never re-read or changed.  A candidate learned from A is
    staged into the independent live B/C banks, then the C executor can evolve
    only skills actually supplied during C.  Both changes become visible only
    after the transaction succeeds, and are therefore available to a later
    ``freeze`` for the next instance.
    """
    configured = validate_execution_profile(profile)
    canonical = canonical_instance_id(instance_id)
    if tuple(coordinator.repeat_ids) != (repeat_id,):
        raise DevelopmentRunnerError("development release requires exactly the frozen single development repeat")
    if set(coordinator.arm_banks) != set(arms.all()):
        raise DevelopmentRunnerError("development release requires exactly frozen A/B/C banks")
    group = coordinator.instances.get(canonical)
    if not isinstance(group, dict):
        raise DevelopmentRunnerError("development instance was not frozen")
    expected_trials = [_assignment_id(canonical, arm, repeat_id) for arm in arms.all()]
    selection = validate_selection_manifest(
        selection_manifest,
        instance_id=canonical,
        trial_ids=expected_trials,
        expected_profile_sha256=profile_sha256(configured),
    )
    full_arms = configured["evolution"]["full_lifecycle_arms"]
    if full_arms != [arms.full_lifecycle]:
        raise DevelopmentRunnerError("development profile must name C as its only full lifecycle arm")
    for arm in (arms.baseline, arms.extraction):
        trial_id = _assignment_id(canonical, arm, repeat_id)
        if selection["selections"][trial_id]["action"] != "skip":
            raise DevelopmentRunnerError(f"development arm {arm} must have an explicit non-evolution skip")
    c_trial = _assignment_id(canonical, arms.full_lifecycle, repeat_id)
    if selection["selections"][c_trial]["action"] != "evaluate_all_supplied":
        raise DevelopmentRunnerError("development C arm must evaluate all actually supplied skills")

    def apply_transaction(staged_banks: dict[str, SkillBank], finished_group: dict[str, Any]) -> dict[str, Any]:
        arm_b_ingestion = _direct_ingest_arm_b_candidates(
            staged_banks[arms.extraction],
            candidate_records=arm_a_candidate_records,
            instance_id=canonical,
        )
        assignment = finished_group["assignments"].get(c_trial)
        if not isinstance(assignment, dict):
            raise DevelopmentRunnerError("frozen C assignment disappeared before release")
        arm_c_maintenance = _maintain_arm_c_candidates(
            staged_banks[arms.full_lifecycle],
            candidate_records=arm_a_candidate_records,
            instance_id=canonical,
            c_trial_id=c_trial,
            evolution_executor=evolution_executor,
        )
        c_update = evolution_executor.apply_update(c_trial, staged_banks[arms.full_lifecycle], assignment)
        return {
            "kind": "r015_development_instance_release",
            "instance_id": canonical,
            "release_order": selection["release_order"],
            "arm_b_direct_extraction_ingestion": arm_b_ingestion,
            "arm_c_new_candidate_fig9_maintenance": arm_c_maintenance,
            "c_supplied_only_evolution_maintenance": c_update,
            "next_instance_bank_snapshots": {
                arm: staged_banks[arm].snapshot()["state_sha256"] for arm in arms.all()
            },
        }

    try:
        result = coordinator.release_instance_transaction(
            canonical,
            ordered_trial_ids=selection["release_order"],
            apply_transaction=apply_transaction,
        )
    except TrialScheduleError as error:
        cause = error.__cause__
        if isinstance(cause, DevelopmentRunnerError):
            raise cause
        raise DevelopmentRunnerError(str(error)) from error
    if not isinstance(result, dict):
        raise DevelopmentRunnerError("development release produced invalid transaction evidence")
    return result
