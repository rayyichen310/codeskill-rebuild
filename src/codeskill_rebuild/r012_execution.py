"""Fail-closed R012 evolution and maintenance execution.

This is deliberately a coordinator, not an autonomous policy.  A caller must
provide an explicit R012 profile and one explicit release action for every
frozen trial.  A Full Lifecycle trial with any actually supplied skill must
reach the manager's evolve-or-skip decision; only a non-evolution arm or
durable evidence that no skill was supplied may avoid that call.  The
coordinator never caps supplied skills or invents a selection rule.  It
records a journal before each manager request, so a crash cannot be retried as
though no model request might have happened.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

from .arm_banks import same_granularity_top5
from .bank import BankError, SkillBank, validate_skill_candidate
from .code_examples import CodeExampleError, apply_code_example_changes
from .evolution import EvolutionEvidenceError, supplied_skills_for_evolution
from .pipeline import (
    internal_skill_to_paper,
    maintenance_messages,
    maintenance_from_skills_messages,
    materialize_lifecycle_code_example,
    paper_skill_to_internal,
    validate_maintenance,
    validate_maintenance_from_skills,
)
from .types import canonical_instance_id, canonical_json, sha256_text, utc_now, write_json


class R012ExecutionError(RuntimeError):
    pass


class ManagerJsonClient(Protocol):
    def call_json(
        self,
        *,
        purpose: str,
        messages: list[dict[str, str]],
        retry_of: str | None = None,
        call_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class SkillEncoder(Protocol):
    def index_skill(self, skill: dict[str, Any]) -> tuple[list[float], dict[str, Any]]: ...


def validate_execution_profile(value: dict[str, Any]) -> dict[str, Any]:
    """Reject omitted R012 selection inputs instead of supplying defaults."""
    if not isinstance(value, dict) or value.get("kind") != "r012_execution_profile":
        raise R012ExecutionError("R012 execution requires kind=r012_execution_profile")
    event = value.get("event_selection")
    evolution = value.get("evolution")
    if not isinstance(event, dict) or not isinstance(evolution, dict):
        raise R012ExecutionError("R012 execution profile needs event_selection and evolution objects")
    for field in ("profile_ref", "selection_rule_ref"):
        if not isinstance(event.get(field), str) or not event[field].strip():
            raise R012ExecutionError(f"R012 event selection profile needs {field}")
    for field in ("max_matching_skills", "skill_token_budget"):
        if not isinstance(event.get(field), int) or event[field] <= 0:
            raise R012ExecutionError(f"R012 event selection profile needs a positive {field}")
    arms = evolution.get("full_lifecycle_arms")
    if not isinstance(arms, list) or not arms or not all(isinstance(item, str) and item for item in arms):
        raise R012ExecutionError("R012 evolution profile needs explicit full_lifecycle_arms")
    if evolution.get("explicit_selection_manifest_required") is not True:
        raise R012ExecutionError("R012 evolution requires an explicit selection manifest")
    if evolution.get("candidate_selection_mode") != "all_actually_supplied":
        raise R012ExecutionError("R012 evolution profile must explicitly require all_actually_supplied candidates")
    # Candidate count/selection is deliberately not inferred here.  A subset
    # policy is not a substitute for Fig.8 and must fail until separately
    # approved; this implementation passes every actually supplied skill.
    return deepcopy(value)


def profile_sha256(profile: dict[str, Any]) -> str:
    return sha256_text(canonical_json(validate_execution_profile(profile)))


def validate_selection_manifest(
    value: dict[str, Any],
    *,
    instance_id: str,
    trial_ids: Iterable[str],
    expected_profile_sha256: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("kind") != "r012_evolution_selection_manifest":
        raise R012ExecutionError("R012 release requires kind=r012_evolution_selection_manifest")
    if canonical_instance_id(str(value.get("instance_id", ""))) != canonical_instance_id(instance_id):
        raise R012ExecutionError("selection manifest instance differs from the frozen instance")
    if value.get("profile_sha256") != expected_profile_sha256:
        raise R012ExecutionError("selection manifest profile hash differs from the frozen execution profile")
    selections = value.get("selections")
    if not isinstance(selections, list):
        raise R012ExecutionError("selection manifest needs a selections list")
    expected = set(trial_ids)
    parsed: dict[str, dict[str, Any]] = {}
    for item in selections:
        if not isinstance(item, dict) or not isinstance(item.get("trial_id"), str):
            raise R012ExecutionError("every selection needs a trial_id")
        trial_id = item["trial_id"]
        if trial_id in parsed or trial_id not in expected:
            raise R012ExecutionError("selection manifest has duplicate or unfrozen trial IDs")
        action = item.get("action")
        if action == "skip":
            if not isinstance(item.get("reason"), str) or not item["reason"].strip():
                raise R012ExecutionError("an explicit evolution skip needs a reason")
            parsed[trial_id] = {"action": "skip", "reason": item["reason"]}
        elif action == "evaluate_all_supplied":
            if not isinstance(item.get("reason"), str) or not item["reason"].strip():
                raise R012ExecutionError("an all-supplied evolution evaluation needs a reason")
            parsed[trial_id] = {
                "action": "evaluate_all_supplied",
                "reason": item["reason"],
            }
        else:
            raise R012ExecutionError("selection action must be evaluate_all_supplied or skip")
    if set(parsed) != expected:
        raise R012ExecutionError("selection manifest must cover every frozen trial exactly once")
    order = value.get("release_order")
    if not isinstance(order, list) or not all(isinstance(item, str) for item in order):
        raise R012ExecutionError("selection manifest needs an explicit release_order")
    if len(order) != len(expected) or set(order) != expected:
        raise R012ExecutionError("release_order must contain every frozen trial exactly once")
    return {"selections": parsed, "release_order": list(order)}


def evolution_messages(
    *,
    supplied: list[dict[str, Any]],
    trajectory_evidence: dict[str, Any],
    paper_prompt: str,
) -> list[dict[str, str]]:
    if not isinstance(trajectory_evidence, dict) or not trajectory_evidence:
        raise R012ExecutionError("evolution needs nonempty trajectory evidence")
    # Figure 8 is paper-facing just like Figure 9.  The runtime bank keeps
    # compact internal labels (``task``/``event``), while the evolution
    # prompt's schema uses ``general``/``event-driven``.  Convert only the
    # nested skill objects and preserve injection evidence unchanged so the
    # manager sees the exact supplied identities and provenance.
    paper_supplied: list[dict[str, Any]] = []
    for item in supplied:
        if not isinstance(item, dict) or not isinstance(item.get("skill"), dict):
            raise R012ExecutionError("evolution supplied evidence lacks a skill object")
        projected = dict(item)
        projected["skill"] = internal_skill_to_paper(item["skill"])
        paper_supplied.append(projected)
    return [
        {"role": "system", "content": paper_prompt},
        {
            "role": "user",
            "content": canonical_json(
                {
                    "provided_skills": paper_supplied,
                    "new_trajectory_evidence": trajectory_evidence,
                }
            ),
        },
    ]


def inspect_full_lifecycle_evidence(
    *,
    trial_id: str,
    instance_id: str,
    result_evidence: dict[str, Any],
) -> dict[str, Any]:
    """Validate copied trial evidence before deciding whether a manager is needed.

    An empty supplied list is a factual result only when durable proxy
    evidence belongs to the trial, or when the caller records an explicit
    pre-request infrastructure failure with exact raw references.  It is
    never inferred from a success, partial failure, or an unclassified empty
    list.  If a skill was actually supplied, a normalized trajectory is
    mandatory and the caller must ask the manager to decide evolve versus
    skip.
    """
    if not isinstance(result_evidence, dict):
        raise R012ExecutionError(f"{trial_id}: finished trial lacks durable result evidence")
    proxy_records = result_evidence.get("proxy_attempt_records")
    if not isinstance(proxy_records, list):
        raise R012ExecutionError(f"{trial_id}: proxy_attempt_records must be a list")
    if not proxy_records:
        # Harbor can fail before the sidecar receives its first request (for
        # example, an agent image setup/runtime incompatibility).  Preserve
        # that boundary as an explicit, reviewable infrastructure result.  A
        # missing list or an unclassified empty list must still fail closed;
        # otherwise a caller could silently turn a partial solver run into a
        # no-skill result and bypass Fig.8.
        infra = result_evidence.get("infra_failure")
        if result_evidence.get("classification") != "infra_failure" or not isinstance(infra, dict):
            raise R012ExecutionError(f"{trial_id}: evolution needs at least one copied proxy attempt")
        if infra.get("trial_id") != trial_id:
            raise R012ExecutionError(f"{trial_id}: infrastructure evidence belongs to a different trial")
        if canonical_instance_id(str(infra.get("instance_id", ""))) != canonical_instance_id(instance_id):
            raise R012ExecutionError(f"{trial_id}: infrastructure evidence differs from its frozen instance")
        if not isinstance(infra.get("error_type"), str) or not infra["error_type"].strip():
            raise R012ExecutionError(f"{trial_id}: infrastructure evidence needs an error_type")
        if not isinstance(infra.get("error"), str) or not infra["error"].strip():
            raise R012ExecutionError(f"{trial_id}: infrastructure evidence needs the exact error")
        if not isinstance(infra.get("raw_evidence"), dict) or not infra["raw_evidence"]:
            raise R012ExecutionError(f"{trial_id}: infrastructure evidence needs raw evidence references")
        trajectory = result_evidence.get("trajectory_evidence")
        if trajectory is not None:
            raise R012ExecutionError(f"{trial_id}: infrastructure evidence cannot claim trajectory evidence")
        return {
            "proxy_attempt_records": [],
            "trajectory_evidence": None,
            "supplied": [],
            "classification": "infra_failure",
            "infra_failure": deepcopy(infra),
        }
    for index, record in enumerate(proxy_records):
        if not isinstance(record, dict) or record.get("trial_id") != trial_id:
            raise R012ExecutionError(f"{trial_id}: proxy attempt {index} belongs to a different trial")
    trajectory = result_evidence.get("trajectory_evidence")
    if trajectory is not None:
        if not isinstance(trajectory, dict):
            raise R012ExecutionError(f"{trial_id}: trajectory evidence must be an object when present")
        source = trajectory.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("canonical_instance_id"), str):
            raise R012ExecutionError(f"{trial_id}: trajectory evidence must be a normalized trace with source.canonical_instance_id")
        if canonical_instance_id(source["canonical_instance_id"]) != canonical_instance_id(instance_id):
            raise R012ExecutionError(f"{trial_id}: trajectory source differs from its frozen instance")
    try:
        supplied = supplied_skills_for_evolution(proxy_records, trial_id=trial_id)
    except EvolutionEvidenceError as error:
        raise R012ExecutionError(f"{trial_id}: supplied-skill evidence is invalid: {error}") from error
    if supplied and not isinstance(trajectory, dict):
        raise R012ExecutionError(f"{trial_id}: an actually supplied skill requires trajectory evidence")
    return {
        "proxy_attempt_records": deepcopy(proxy_records),
        "trajectory_evidence": deepcopy(trajectory) if isinstance(trajectory, dict) else None,
        "supplied": supplied,
    }


def validate_evolution_output(
    value: dict[str, Any],
    *,
    supplied: list[dict[str, Any]],
    trajectory_evidence: dict[str, Any] | None = None,
    visible_step_ids_by_source: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    if visible_step_ids_by_source is not None and (
        not isinstance(visible_step_ids_by_source, dict)
        or not all(
            isinstance(source_id, str)
            and isinstance(step_ids, list)
            and all(isinstance(step_id, str) for step_id in step_ids)
            for source_id, step_ids in visible_step_ids_by_source.items()
        )
    ):
        raise R012ExecutionError("evolution visible source steps must map source IDs to step-ID lists")
    by_identity: dict[tuple[str, int], dict[str, Any]] = {}
    by_id: dict[str, list[dict[str, Any]]] = {}
    for item in supplied:
        skill = item.get("skill") if isinstance(item, dict) else None
        skill_id = skill.get("skill_id") if isinstance(skill, dict) else None
        version = skill.get("version") if isinstance(skill, dict) else None
        if not isinstance(skill_id, str) or not skill_id or not isinstance(version, int):
            raise R012ExecutionError("actual supplied skill lacks skill_id/version")
        by_identity[(skill_id, version)] = item
        by_id.setdefault(skill_id, []).append(item)
    if not isinstance(value, dict) or value.get("action") not in {"evolve", "skip"}:
        raise R012ExecutionError("evolution output must choose evolve or skip")
    if not isinstance(value.get("reason"), str) or not value["reason"].strip():
        raise R012ExecutionError("evolution output needs a nonempty reason")
    if value["action"] == "skip":
        return {"action": "skip", "reason": value["reason"]}
    skill_id = value.get("target_skill_id")
    if not isinstance(skill_id, str) or not skill_id:
        raise R012ExecutionError("evolution needs a target_skill_id")
    stated_version = value.get("target_skill_version")
    if stated_version is None:
        matches = by_id.get(skill_id, [])
        if len(matches) != 1:
            raise R012ExecutionError("ambiguous evolution target requires target_skill_version")
        base = matches[0]
    else:
        if not isinstance(stated_version, int):
            raise R012ExecutionError("target_skill_version must be an integer when present")
        base = by_identity.get((skill_id, stated_version))
        if base is None:
            raise R012ExecutionError("evolution may revise only an actually supplied skill")
    base_skill = base["skill"]
    raw_evolved_skill = value.get("skill") if isinstance(value.get("skill"), dict) else {}
    if "code_examples" in raw_evolved_skill:
        raise R012ExecutionError("evolution must use code_example_changes instead of embedding stored evidence in skill")
    internal = paper_skill_to_internal(
        raw_evolved_skill,
        benchmark=str(base_skill.get("benchmark", "")),
        expected_granularity=str(base_skill.get("granularity", "")),
    )
    try:
        updated_examples = apply_code_example_changes(
            value.get("code_example_changes"),
            base_skill.get("code_examples"),
            materialize_added=(
                (
                    lambda raw: materialize_lifecycle_code_example(
                        raw,
                        trajectory_evidence,
                        visible_step_ids_by_source=visible_step_ids_by_source,
                    )
                )
                if isinstance(trajectory_evidence, dict)
                else None
            ),
            revision_context={
                "phase": "evolution",
                "source_skill_id": base_skill["skill_id"],
                "source_skill_version": base_skill["version"],
            },
        )
    except (CodeExampleError, ValueError) as error:
        raise R012ExecutionError(f"evolution code-example update is invalid: {error}") from error
    if updated_examples:
        internal["code_examples"] = updated_examples
    try:
        validated_skill = validate_skill_candidate(internal)
    except BankError as error:
        raise R012ExecutionError(f"evolved skill violates the internal schema: {error}") from error
    return {
        "action": "evolve",
        "reason": value["reason"],
        "target_skill_id": base_skill["skill_id"],
        "target_skill_version": base_skill["version"],
        "base": deepcopy(base),
        "skill": validated_skill,
    }


@dataclass
class R012EvolutionMaintenanceExecutor:
    """Apply Full Lifecycle evidence through Fig.8 then Fig.9 when supplied."""

    manager: ManagerJsonClient | None
    encoder: SkillEncoder | None
    journal_root: Path
    instance_id: str
    profile: dict[str, Any]
    selections: dict[str, dict[str, Any]]
    evolution_prompt: str | None = None
    maintenance_prompt: str | None = None
    use_visible_maintenance: bool = False

    def __post_init__(self) -> None:
        self.journal_root = Path(self.journal_root)
        self.profile = validate_execution_profile(self.profile)
        self.instance_id = canonical_instance_id(self.instance_id)
        if not isinstance(self.use_visible_maintenance, bool):
            raise R012ExecutionError("use_visible_maintenance must be a boolean")

    def _journal_path(self, trial_id: str, phase: str) -> Path:
        return self.journal_root / sha256_text(trial_id)[:20] / f"{phase}.json"

    def _prepare_call(self, *, trial_id: str, phase: str, value: dict[str, Any]) -> Path:
        path = self._journal_path(trial_id, phase)
        if path.exists():
            raise R012ExecutionError(
                f"{trial_id}: existing {phase} pre-call journal requires manual reconciliation; automatic replay is forbidden"
            )
        write_json(
            path,
            {
                "schema_version": 1,
                "kind": "r012_pre_call_journal",
                "trial_id": trial_id,
                "phase": phase,
                "status": "prepared_before_manager_call",
                "created_at_utc": utc_now(),
                "profile_sha256": profile_sha256(self.profile),
                **deepcopy(value),
            },
        )
        return path

    @staticmethod
    def _finish_journal(path: Path, *, status: str, value: dict[str, Any]) -> None:
        from .types import read_json

        journal = read_json(path)
        journal.update({"status": status, "finished_at_utc": utc_now(), **deepcopy(value)})
        write_json(path, journal)

    def _call_manager(
        self,
        *,
        trial_id: str,
        phase: str,
        purpose: str,
        messages: list[dict[str, str]],
        metadata: dict[str, Any],
    ) -> tuple[dict[str, Any], Path]:
        if self.manager is None:
            raise R012ExecutionError("manager execution was not explicitly enabled")
        journal = self._prepare_call(
            trial_id=trial_id,
            phase=phase,
            value={
                "purpose": purpose,
                "messages": deepcopy(messages),
                "messages_sha256": sha256_text(canonical_json(messages)),
                "call_metadata": deepcopy(metadata),
            },
        )
        try:
            result = self.manager.call_json(purpose=purpose, messages=messages, call_metadata=metadata)
        except BaseException as error:
            self._finish_journal(
                journal,
                status="manager_call_raised",
                value={"error_type": type(error).__name__, "error": str(error), "automatic_retry": False},
            )
            raise
        if not isinstance(result, dict) or not isinstance(result.get("json"), dict) or not isinstance(result.get("call_id"), str):
            self._finish_journal(
                journal,
                status="manager_result_invalid",
                value={"result": deepcopy(result), "automatic_retry": False},
            )
            raise R012ExecutionError("manager client returned an invalid result after a pre-call journal")
        return result, journal

    def apply_extracted_candidate_maintenance(
        self,
        *,
        trial_id: str,
        bank: SkillBank,
        candidate: dict[str, Any],
        candidate_ordinal: int,
        candidate_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Run one newly extracted candidate through the real Fig.9 path.

        Arm A's shared extraction is deliberately not a maintenance decision.
        Arm B can therefore ingest it deterministically.  Arm C, however,
        must give every new (exact-content grouped) candidate its own Fig.9
        add/merge/drop decision against the C bank as it exists at that point
        in the single release transaction.  This method is separate from
        :meth:`apply_update`: there is no Fig.8 evolution input because the
        candidate came directly from Arm A rather than a C-supplied skill.
        """
        if not isinstance(candidate_ordinal, int) or candidate_ordinal <= 0:
            raise R012ExecutionError("extracted candidate ordinal must be positive")
        if not isinstance(candidate_evidence, dict) or not candidate_evidence:
            raise R012ExecutionError("extracted candidate needs durable source evidence")
        if self.encoder is None or not self.maintenance_prompt:
            raise R012ExecutionError("extracted-candidate maintenance requires an encoder and maintenance prompt")
        try:
            candidate = validate_skill_candidate(candidate)
        except BankError as error:
            raise R012ExecutionError(f"extracted candidate violates the internal schema: {error}") from error
        provenance = candidate.get("provenance")
        if not isinstance(provenance, dict):
            raise R012ExecutionError("extracted candidate needs provenance before maintenance")
        source_ids = provenance.get("source_instance_ids")
        if not isinstance(source_ids, list) or not source_ids or not all(isinstance(value, str) and value for value in source_ids):
            raise R012ExecutionError("extracted candidate provenance needs source_instance_ids")
        phase = f"extracted-candidate-maintenance-{candidate_ordinal:03d}"
        if not isinstance(bank, SkillBank):
            raise R012ExecutionError("extracted candidate maintenance needs its staged C bank")
        evidence = deepcopy(candidate_evidence)
        retrieved, retrieval = same_granularity_top5(bank, candidate, self.encoder)
        prompt_candidate = {
            key: candidate[key]
            for key in ("title", "granularity", "when_to_apply", "rules", "benchmark", "code_examples")
            if key in candidate
        }
        schema_sha256 = sha256_text(canonical_json(prompt_candidate))
        maintenance_call, journal = self._call_manager(
            trial_id=trial_id,
            phase=phase,
            purpose=f"r015_arm_a_candidate_maintenance:{trial_id}:{candidate_ordinal:03d}:{schema_sha256[:16]}",
            messages=(maintenance_from_skills_messages if self.use_visible_maintenance else maintenance_messages)(
                prompt_candidate, retrieved, paper_prompt=self.maintenance_prompt),
            metadata={
                "source_arm": "A",
                "candidate_ordinal": candidate_ordinal,
                "candidate_schema_sha256": schema_sha256,
                "retrieval": retrieval,
            },
        )
        try:
            maintenance = (validate_maintenance_from_skills if self.use_visible_maintenance else validate_maintenance)(
                maintenance_call["json"],
                candidate=candidate,
                retrieved_skill_ids={str(item["skill_id"]) for item in retrieved},
                retrieved_skills=retrieved,
            )
            candidate_for_operation = deepcopy(maintenance.get("skill", candidate))
            candidate_for_operation["provenance"] = deepcopy(provenance)
            operation = bank.apply(
                operation_id="r015-arm-a-candidate-" + sha256_text(
                    canonical_json(
                        {
                            "trial_id": trial_id,
                            "candidate_ordinal": candidate_ordinal,
                            "candidate_schema_sha256": schema_sha256,
                            "maintenance_call_id": maintenance_call["call_id"],
                        }
                    )
                )[:20],
                decision=maintenance["action"],
                candidate=candidate_for_operation,
                source_instance_ids=source_ids,
                merge_target_id=maintenance.get("merge_target_skill_id"),
                evidence={
                    "kind": "r015_arm_a_candidate_fig9_maintenance",
                    "source_arm": "A",
                    "candidate_ordinal": candidate_ordinal,
                    "candidate_evidence": evidence,
                    "maintenance": {"call_id": maintenance_call["call_id"], "validated": deepcopy(maintenance)},
                    "maintenance_retrieval": deepcopy(retrieval),
                },
            )
        except BaseException as error:
            self._finish_journal(
                journal,
                status="maintenance_output_or_bank_operation_rejected",
                value={"call_id": maintenance_call["call_id"], "error_type": type(error).__name__, "error": str(error)},
            )
            raise
        self._finish_journal(
            journal,
            status="maintenance_applied_to_staged_bank",
            value={"call_id": maintenance_call["call_id"], "validated": maintenance, "operation": operation},
        )
        return {
            "kind": "r015_arm_a_candidate_fig9_maintenance_applied",
            "candidate_ordinal": candidate_ordinal,
            "maintenance_call_id": maintenance_call["call_id"],
            "operation": operation,
        }

    def apply_update(self, trial_id: str, bank: SkillBank, assignment: dict[str, Any]) -> dict[str, Any]:
        selection = self.selections.get(trial_id)
        if selection is None:
            raise R012ExecutionError(f"{trial_id}: no explicit evolution selection")
        full_arms = set(self.profile["evolution"]["full_lifecycle_arms"])
        if assignment.get("arm") not in full_arms:
            if selection["action"] != "skip":
                raise R012ExecutionError(f"{trial_id}: a non-lifecycle arm cannot select an evolution target")
            return {"kind": "r012_no_evolution_for_arm", "selection": deepcopy(selection)}
        if selection["action"] != "evaluate_all_supplied":
            raise R012ExecutionError(
                f"{trial_id}: a Full Lifecycle trial must evaluate_all_supplied; manual evolution skip cannot bypass supplied evidence"
            )
        evidence = inspect_full_lifecycle_evidence(
            trial_id=trial_id,
            instance_id=self.instance_id,
            result_evidence=assignment.get("result_evidence"),
        )
        proxy_records = evidence["proxy_attempt_records"]
        trajectory = evidence["trajectory_evidence"]
        supplied = evidence["supplied"]
        if not supplied:
            return {
                "kind": "r012_no_evolution_without_supplied_skill",
                "selection": deepcopy(selection),
                "supplied": [],
                "proxy_attempt_count": len(proxy_records),
                "proxy_outcomes": [record.get("proxy_outcome") for record in proxy_records],
            }
        if not isinstance(trajectory, dict):
            raise R012ExecutionError(f"{trial_id}: an actually supplied skill requires trajectory evidence")
        try:
            messages = evolution_messages(
                supplied=supplied,
                trajectory_evidence=trajectory,
                paper_prompt=self.evolution_prompt or "",
            )
        except EvolutionEvidenceError as error:
            raise R012ExecutionError(f"{trial_id}: supplied-skill evidence is invalid: {error}") from error
        if not self.evolution_prompt:
            raise R012ExecutionError("evolution prompt is required before a manager call")
        evolution_call, evolution_journal = self._call_manager(
            trial_id=trial_id,
            phase="evolution",
            purpose=f"r012_evolution:{trial_id}",
            messages=messages,
            metadata={
                "candidate_selection_mode": "all_actually_supplied",
                "selection_reason": selection["reason"],
                "supplied_skill_identities": [
                    {"skill_id": item["skill"]["skill_id"], "version": item["skill"]["version"]} for item in supplied
                ],
            },
        )
        try:
            evolved = validate_evolution_output(
                evolution_call["json"],
                supplied=supplied,
                trajectory_evidence=trajectory,
            )
        except BaseException as error:
            self._finish_journal(
                evolution_journal,
                status="evolution_output_rejected",
                value={"call_id": evolution_call["call_id"], "error_type": type(error).__name__, "error": str(error)},
            )
            raise
        if evolved["action"] == "skip":
            self._finish_journal(
                evolution_journal,
                status="evolution_skip",
                value={"call_id": evolution_call["call_id"], "validated": evolved},
            )
            return {
                "kind": "r012_evolution_manager_skip",
                "selection": deepcopy(selection),
                "supplied": deepcopy(supplied),
                "manager_call_id": evolution_call["call_id"],
                "validated": evolved,
            }
        self._finish_journal(
            evolution_journal,
            status="evolution_validated",
            value={"call_id": evolution_call["call_id"], "validated": evolved},
        )
        base = evolved["base"]
        base_skill = base["skill"]
        candidate = deepcopy(evolved["skill"])
        base_provenance = base_skill.get("provenance") if isinstance(base_skill.get("provenance"), dict) else {}
        base_sources = base_provenance.get("source_instance_ids", [])
        base_raw_sources = base_provenance.get("source_instance_ids_raw", base_sources)
        base_parents = base_provenance.get("parent_skill_ids", [])
        if not isinstance(base_sources, list) or not isinstance(base_raw_sources, list) or not isinstance(base_parents, list):
            raise R012ExecutionError("supplied base skill has invalid provenance")
        candidate["provenance"] = {
            "source_instance_ids": sorted({canonical_instance_id(item) for item in [*base_sources, self.instance_id]}),
            "source_instance_ids_raw": sorted(set([*base_raw_sources, self.instance_id])),
            "parent_skill_ids": sorted(set([*base_parents, base_skill["skill_id"]])),
        }
        if self.encoder is None or not self.maintenance_prompt:
            raise R012ExecutionError("maintenance execution requires an encoder and maintenance prompt")
        retrieved, retrieval = same_granularity_top5(bank, candidate, self.encoder)
        candidate_for_maintenance_prompt = {
            key: candidate[key]
            for key in ("title", "granularity", "when_to_apply", "rules", "benchmark", "code_examples")
            if key in candidate
        }
        maintenance_call, maintenance_journal = self._call_manager(
            trial_id=trial_id,
            phase="maintenance",
            purpose=f"r012_evolution_maintenance:{trial_id}:{base_skill['skill_id']}:{base_skill['version']}",
            messages=(maintenance_from_skills_messages if self.use_visible_maintenance else maintenance_messages)(
                candidate_for_maintenance_prompt, retrieved, paper_prompt=self.maintenance_prompt),
            metadata={
                "evolution_call_id": evolution_call["call_id"],
                "candidate_parent_skill_id": base_skill["skill_id"],
                "retrieval": retrieval,
            },
        )
        try:
            maintenance = (validate_maintenance_from_skills if self.use_visible_maintenance else validate_maintenance)(
                maintenance_call["json"],
                candidate=candidate,
                retrieved_skill_ids={str(item["skill_id"]) for item in retrieved},
                retrieved_skills=retrieved,
            )
            candidate_for_operation = deepcopy(maintenance.get("skill", candidate))
            candidate_for_operation["provenance"] = deepcopy(candidate["provenance"])
            operation_id = "r012-evolution-" + sha256_text(
                canonical_json(
                    {
                        "trial_id": trial_id,
                        "base_skill_id": base_skill["skill_id"],
                        "base_skill_version": base_skill["version"],
                        "evolution_call_id": evolution_call["call_id"],
                        "maintenance_call_id": maintenance_call["call_id"],
                    }
                )
            )[:20]
            operation = bank.apply(
                operation_id=operation_id,
                decision=maintenance["action"],
                candidate=candidate_for_operation,
                source_instance_ids=[self.instance_id],
                merge_target_id=maintenance.get("merge_target_skill_id"),
                evidence={
                    "kind": "r012_evolution_then_maintenance",
                    "selection": deepcopy(selection),
                    "injection_evidence": deepcopy(base["injection_evidence"]),
                    "trajectory_evidence": deepcopy(trajectory),
                    "evolution": {"call_id": evolution_call["call_id"], "validated": deepcopy(evolved)},
                    "maintenance": {"call_id": maintenance_call["call_id"], "validated": deepcopy(maintenance)},
                    "maintenance_retrieval": deepcopy(retrieval),
                },
            )
        except BaseException as error:
            self._finish_journal(
                maintenance_journal,
                status="maintenance_output_or_bank_operation_rejected",
                value={"call_id": maintenance_call["call_id"], "error_type": type(error).__name__, "error": str(error)},
            )
            raise
        self._finish_journal(
            maintenance_journal,
            status="maintenance_applied_to_staged_bank",
            value={"call_id": maintenance_call["call_id"], "validated": maintenance, "operation": operation},
        )
        return {
            "kind": "r012_evolution_then_maintenance_applied",
            "selection": deepcopy(selection),
            "supplied": deepcopy(supplied),
            "evolution_call_id": evolution_call["call_id"],
            "maintenance_call_id": maintenance_call["call_id"],
            "operation": operation,
        }
