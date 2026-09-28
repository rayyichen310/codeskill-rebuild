"""Versioned, replayable skill-bank transactions and provenance exclusion."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable

from .code_examples import CodeExampleError, validate_materialized_code_examples
from .types import canonical_instance_id, canonical_json, read_json, sha256_text, utc_now, write_json


class BankError(ValueError):
    pass


REQUIRED_SKILL_FIELDS = {"title", "granularity", "when_to_apply", "rules", "benchmark"}


def _validate_candidate(candidate: dict[str, Any]) -> None:
    missing = REQUIRED_SKILL_FIELDS - set(candidate)
    if missing:
        raise BankError(f"Skill candidate missing fields: {sorted(missing)}")
    if candidate["granularity"] not in {"task", "event"}:
        raise BankError("granularity must be task or event")
    if not isinstance(candidate["title"], str) or not candidate["title"].strip():
        raise BankError("title must be nonempty")
    if not isinstance(candidate["when_to_apply"], str) or not candidate["when_to_apply"].strip():
        raise BankError("when_to_apply must be nonempty")
    if not isinstance(candidate["rules"], list) or not candidate["rules"] or not all(isinstance(rule, str) and rule.strip() for rule in candidate["rules"]):
        raise BankError("rules must be a nonempty list of nonempty strings")
    if not isinstance(candidate["benchmark"], str) or not candidate["benchmark"].strip():
        raise BankError("benchmark must be nonempty")
    if "code_examples" in candidate:
        try:
            validate_materialized_code_examples(candidate["code_examples"])
        except CodeExampleError as error:
            raise BankError(f"invalid code_examples: {error}") from error


def validate_skill_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Validate a public internal skill schema before a costly later step."""
    if not isinstance(candidate, dict):
        raise BankError("skill candidate must be an object")
    _validate_candidate(candidate)
    return deepcopy(candidate)


def _source_instances(value: dict[str, Any]) -> set[str]:
    provenance = value.get("provenance", {})
    values = provenance.get("source_instance_ids", [])
    if not isinstance(values, list):
        raise BankError("source_instance_ids must be a list")
    return {canonical_instance_id(item) for item in values}


def _raw_source_instances(value: dict[str, Any]) -> set[str]:
    provenance = value.get("provenance", {})
    values = provenance.get("source_instance_ids_raw", provenance.get("source_instance_ids", []))
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise BankError("source_instance_ids_raw must be a list of strings")
    return set(values)


@dataclass
class SkillBank:
    benchmark: str
    skills: list[dict[str, Any]]
    operations: list[dict[str, Any]]
    sequence: int = 0
    states: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.states is None:
            self.states = [self._state_payload()]

    @classmethod
    def empty(cls, benchmark: str) -> "SkillBank":
        return cls(benchmark=benchmark, skills=[], operations=[], sequence=0)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SkillBank":
        bank = cls(
            benchmark=value["benchmark"],
            skills=deepcopy(value.get("skills", [])),
            operations=deepcopy(value.get("operations", [])),
            sequence=int(value.get("sequence", 0)),
            states=deepcopy(value.get("states")) if value.get("states") is not None else None,
        )
        return bank

    @classmethod
    def load(cls, path: Any) -> "SkillBank":
        return cls.from_dict(read_json(path))

    def save(self, path: Any) -> None:
        """Atomically persist all immutable states and the operation journal."""
        write_json(path, self.to_dict())

    def apply_and_save(self, path: Any, **kwargs: Any) -> dict[str, Any]:
        """Apply an idempotent operation, then atomically publish its state."""
        operation = self.apply(**kwargs)
        self.save(path)
        return operation

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "benchmark": self.benchmark,
            "sequence": self.sequence,
            "skills": deepcopy(self.skills),
            "operations": deepcopy(self.operations),
            "states": deepcopy(self.states),
        }

    def _state_payload(self) -> dict[str, Any]:
        value = {"benchmark": self.benchmark, "sequence": self.sequence, "skills": deepcopy(self.skills)}
        value["state_sha256"] = sha256_text(canonical_json(value))
        return value

    def _journal_sha256(self) -> str:
        # Exclude this derived field so a journal hash cannot contain itself.
        journal = [{key: value for key, value in operation.items() if key != "journal_sha256_after"} for operation in self.operations]
        return sha256_text(canonical_json(journal))

    def snapshot(self, sequence: int | None = None) -> dict[str, Any]:
        requested = self.sequence if sequence is None else sequence
        for state in self.states or []:
            if state["sequence"] == requested:
                return deepcopy(state)
        raise BankError(f"No immutable state snapshot for sequence {requested}")

    def _skill_id(self, operation_id: str, candidate: dict[str, Any]) -> str:
        return "skill-" + sha256_text(canonical_json({"operation_id": operation_id, "candidate": candidate}))[:16]

    def _find_active(self, skill_id: str, skills: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        for skill in self.skills if skills is None else skills:
            if skill.get("skill_id") == skill_id and skill.get("status") == "active":
                return skill
        raise BankError(f"Active skill does not exist: {skill_id}")

    def apply(self, *, operation_id: str, decision: str, candidate: dict[str, Any], source_instance_ids: Iterable[str], evidence: dict[str, Any], merge_target_id: str | None = None) -> dict[str, Any]:
        """Apply a model-selected add/merge/drop decision exactly once."""
        for operation in self.operations:
            if operation["operation_id"] == operation_id:
                return deepcopy(operation)
        _validate_candidate(candidate)
        if candidate["benchmark"] != self.benchmark:
            raise BankError("Candidate benchmark differs from bank")
        if decision not in {"add", "merge", "drop"}:
            raise BankError("decision must be add, merge, or drop")
        before = self.snapshot()["state_sha256"]
        candidate_provenance = candidate.get("provenance") if isinstance(candidate.get("provenance"), dict) else {}
        provided_source_ids = list(source_instance_ids)
        if not all(isinstance(value, str) for value in provided_source_ids):
            raise BankError("source_instance_ids must contain strings")
        candidate_source_ids = candidate_provenance.get("source_instance_ids", [])
        candidate_raw_source_ids = candidate_provenance.get("source_instance_ids_raw", candidate_source_ids)
        if not isinstance(candidate_source_ids, list) or not isinstance(candidate_raw_source_ids, list):
            raise BankError("candidate provenance source IDs must be lists")
        source_ids = sorted({canonical_instance_id(value) for value in [*provided_source_ids, *candidate_source_ids]})
        raw_source_ids = sorted(set(provided_source_ids) | set(candidate_raw_source_ids))
        candidate_parents = sorted(set(candidate_provenance.get("parent_skill_ids", [])))
        operation: dict[str, Any] = {
            "operation_id": operation_id,
            "decision": decision,
            "candidate": deepcopy(candidate),
            "source_instance_ids": source_ids,
            "source_instance_ids_raw": raw_source_ids,
            "evidence": deepcopy(evidence),
            "before_snapshot_sha256": before,
            "created_at_utc": utc_now(),
            "merge_target_id": merge_target_id,
        }
        if decision == "add":
            new_skill = deepcopy(candidate)
            new_skill.update(
                {
                    "skill_id": self._skill_id(operation_id, candidate),
                    "version": 1,
                    "status": "active",
                    "created_sequence": self.sequence + 1,
                    "provenance": {
                        "source_instance_ids": source_ids,
                        "source_instance_ids_raw": raw_source_ids,
                        "parent_skill_ids": candidate_parents,
                    },
                }
            )
            self.skills.append(new_skill)
            operation["result_skill_id"] = new_skill["skill_id"]
        elif decision == "merge":
            if not merge_target_id:
                raise BankError("merge requires merge_target_id")
            target = self._find_active(merge_target_id)
            if target["benchmark"] != candidate["benchmark"] or target["granularity"] != candidate["granularity"]:
                raise BankError("merge target benchmark/granularity mismatch")
            target["status"] = "superseded"
            merged = deepcopy(candidate)
            merged_sources = sorted(_source_instances(target) | set(source_ids))
            merged_raw_sources = sorted(_raw_source_instances(target) | set(raw_source_ids))
            merged_parents = sorted(set(target.get("provenance", {}).get("parent_skill_ids", [])) | set(candidate_parents) | {target["skill_id"]})
            merged.update(
                {
                    "skill_id": self._skill_id(operation_id, candidate),
                    "version": int(target.get("version", 1)) + 1,
                    "status": "active",
                    "created_sequence": self.sequence + 1,
                    "provenance": {
                        "source_instance_ids": merged_sources,
                        "source_instance_ids_raw": merged_raw_sources,
                        "parent_skill_ids": merged_parents,
                    },
                }
            )
            self.skills.append(merged)
            operation["result_skill_id"] = merged["skill_id"]
            operation["superseded_skill_id"] = target["skill_id"]
        self.sequence += 1
        after_state = self._state_payload()
        operation["after_state_sha256"] = after_state["state_sha256"]
        self.operations.append(operation)
        operation["journal_sha256_after"] = self._journal_sha256()
        self.states.append(after_state)
        return deepcopy(operation)

    def eligible(self, *, instance_id: str, granularity: str, frozen_sequence: int | None = None) -> list[dict[str, Any]]:
        return self.eligibility_report(
            instance_id=instance_id,
            granularity=granularity,
            frozen_sequence=frozen_sequence,
        )["eligible_skills"]

    def eligibility_report(
        self,
        *,
        instance_id: str,
        granularity: str,
        frozen_sequence: int | None = None,
    ) -> dict[str, Any]:
        """Return eligible skills plus explicit pre-scoring exclusion evidence."""
        state = self.snapshot(self.sequence if frozen_sequence is None else frozen_sequence)
        canonical_id = canonical_instance_id(instance_id)
        selected: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        for skill in state["skills"]:
            reasons: list[str] = []
            is_active = skill.get("status") == "active"
            granularity_matches = skill.get("granularity") == granularity
            if not is_active:
                reasons.append("status_not_active")
            if not granularity_matches:
                reasons.append("granularity_mismatch")
            evaluated_sources: list[str] | None = None
            if is_active and granularity_matches:
                # Preserve eligible()'s historical short-circuit behavior:
                # unrelated inactive/granularity records never need source
                # parsing to decide this phase's selection.
                evaluated_sources = sorted(_source_instances(skill))
                if canonical_id in evaluated_sources:
                    reasons.append("same_instance_provenance")
            candidates.append(
                {
                    "skill_id": skill.get("skill_id"),
                    "version": skill.get("version"),
                    "title": skill.get("title"),
                    "status": skill.get("status"),
                    "granularity": skill.get("granularity"),
                    "provenance": deepcopy(skill.get("provenance")),
                    "evaluated_source_instance_ids": evaluated_sources,
                    "eligible": not reasons,
                    "exclusion_reasons": reasons,
                }
            )
            if not reasons:
                selected.append(deepcopy(skill))
        return {
            "instance_id": canonical_id,
            "granularity": granularity,
            "frozen_sequence": state["sequence"],
            "state_sha256": state["state_sha256"],
            "eligible_skills": selected,
            "candidates": candidates,
        }
