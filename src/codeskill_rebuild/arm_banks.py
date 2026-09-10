"""Deterministic R010 candidate-bank construction and maintenance retrieval.

The shared extraction output is a candidate list.  Arm B derives a bank from
that list without a maintenance model decision; arm C may apply archived or
new Fig.9 decisions to an independent bank.  The two banks therefore never
share mutable state.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Protocol

from .bank import BankError, SkillBank
from .retrieval import cosine
from .types import canonical_instance_id, canonical_json, sha256_text


SCHEMA_FIELDS = ("title", "granularity", "when_to_apply", "rules", "benchmark")


class SkillEncoder(Protocol):
    def index_skill(self, skill: dict[str, Any]) -> tuple[list[float], dict[str, Any]]: ...


def exact_skill_schema(skill: dict[str, Any]) -> dict[str, Any]:
    """Return the only fields used for R010's exact candidate de-duplication."""
    if not isinstance(skill, dict):
        raise BankError("candidate must be an object")
    missing = [field for field in SCHEMA_FIELDS if field not in skill]
    if missing:
        raise BankError(f"candidate is missing schema fields: {missing}")
    return {field: deepcopy(skill[field]) for field in SCHEMA_FIELDS}


def _source_ids(record: dict[str, Any]) -> tuple[list[str], list[str]]:
    values = record.get("source_instance_ids")
    raw_values = record.get("source_instance_ids_raw", values)
    if not isinstance(values, list) or not values or not all(isinstance(value, str) and value for value in values):
        raise BankError("candidate record needs nonempty source_instance_ids")
    if not isinstance(raw_values, list) or not raw_values or not all(isinstance(value, str) and value for value in raw_values):
        raise BankError("candidate record needs nonempty source_instance_ids_raw")
    return sorted({canonical_instance_id(value) for value in values}), sorted(set(raw_values))


def group_exact_candidates(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Union provenance only when all five internal schema fields are identical."""
    grouped: dict[str, dict[str, Any]] = {}
    for ordinal, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise BankError("candidate record must be an object")
        schema = exact_skill_schema(record.get("skill"))
        canonical_sources, raw_sources = _source_ids(record)
        key = canonical_json(schema)
        bucket = grouped.setdefault(
            key,
            {
                "skill": schema,
                "source_instance_ids": set(),
                "source_instance_ids_raw": set(),
                "candidate_records": [],
            },
        )
        bucket["source_instance_ids"].update(canonical_sources)
        bucket["source_instance_ids_raw"].update(raw_sources)
        reference = record.get("candidate_record")
        if not isinstance(reference, dict):
            reference = {"ordinal": ordinal}
        bucket["candidate_records"].append(deepcopy(reference))

    grouped_values: list[dict[str, Any]] = []
    for key in sorted(grouped):
        bucket = grouped[key]
        grouped_values.append(
            {
                "skill": deepcopy(bucket["skill"]),
                "source_instance_ids": sorted(bucket["source_instance_ids"]),
                "source_instance_ids_raw": sorted(bucket["source_instance_ids_raw"]),
                "candidate_records": deepcopy(bucket["candidate_records"]),
                "exact_schema_sha256": sha256_text(key),
            }
        )
    return grouped_values


def direct_candidate_bank(*, benchmark: str, grouped_candidates: Iterable[dict[str, Any]], operation_prefix: str = "r010-b-direct") -> SkillBank:
    """Create Arm B's immutable direct-candidate bank without a Fig.9 call."""
    bank = SkillBank.empty(benchmark)
    for ordinal, group in enumerate(grouped_candidates, start=1):
        skill = exact_skill_schema(group.get("skill"))
        if skill["benchmark"] != benchmark:
            raise BankError("candidate benchmark differs from direct candidate bank")
        sources, raw_sources = _source_ids(group)
        skill["provenance"] = {
            "source_instance_ids": sources,
            "source_instance_ids_raw": raw_sources,
            "parent_skill_ids": [],
        }
        bank.apply(
            operation_id=f"{operation_prefix}-{ordinal:03d}-{group['exact_schema_sha256'][:16]}",
            decision="add",
            candidate=skill,
            source_instance_ids=sources,
            evidence={
                "kind": "r010_direct_schema_valid_candidate",
                "candidate_records": deepcopy(group.get("candidate_records", [])),
                "source_instance_ids_raw": raw_sources,
                "exact_schema_sha256": group["exact_schema_sha256"],
            },
        )
    return bank


def same_granularity_top5(
    bank: SkillBank,
    candidate: dict[str, Any],
    encoder: SkillEncoder,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Retrieve Fig.9's top five from every active same-granularity skill.

    Candidate provenance is intentionally *not* a retrieval exclusion here.
    A source exclusion belongs to solver evaluation, where a skill must not be
    injected into a trial from which it was learned.
    """
    schema = exact_skill_schema(candidate)
    active = [
        skill
        for skill in bank.skills
        if skill.get("status") == "active" and skill.get("granularity") == schema["granularity"]
    ]
    candidate_vector, candidate_index = encoder.index_skill(schema)
    ranked: list[dict[str, Any]] = []
    for skill in active:
        vector, index = encoder.index_skill(skill)
        ranked.append({"skill": skill, "score": cosine(candidate_vector, vector), "index_record": index})
    ranked.sort(key=lambda item: (-item["score"], item["skill"]["skill_id"]))
    selected = ranked[:5]
    return [item["skill"] for item in selected], {
        "kind": "r010_same_granularity_maintenance_retrieval",
        "bank_sequence_before": bank.sequence,
        "granularity": schema["granularity"],
        "max_retrieved_skills": 5,
        "candidate_index_record": candidate_index,
        "source_provenance_filter": "none",
        "ranked": [
            {"skill_id": item["skill"]["skill_id"], "score": item["score"], "index_record": item["index_record"]}
            for item in selected
        ],
    }


def overlapping_source_skill_ids(bank: SkillBank, *, candidate: dict[str, Any], source_instance_ids: Iterable[str]) -> list[str]:
    """Report which same-granularity entries a legacy source filter would remove."""
    from .bank import _source_instances  # Local helper keeps canonical identity semantics identical.

    sources = {canonical_instance_id(value) for value in source_instance_ids}
    return sorted(
        skill["skill_id"]
        for skill in bank.skills
        if skill.get("status") == "active"
        and skill.get("granularity") == candidate.get("granularity")
        and _source_instances(skill) & sources
    )
