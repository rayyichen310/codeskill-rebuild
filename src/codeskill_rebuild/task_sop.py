"""Compact SOP-only requests and source-candidate citations for active Task synthesis."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from .bank import validate_skill_candidate
from .pipeline import validate_extraction
from .types import canonical_instance_id


TASK_SOP_MERGE_SCHEMA = {"name": "task_sop_merge_v1", "schema": {"oneOf": [
    {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["generate"]},
        "skill": {"type": "object", "properties": {
            "title": {"type": "string", "minLength": 1},
            "granularity": {"type": "string", "enum": ["general"]},
            "when_to_apply": {"type": "string", "minLength": 1},
            "rules": {"type": "array", "items": {"type": "string", "minLength": 1},
                      "minItems": 1}},
            "required": ["title", "granularity", "when_to_apply", "rules"],
            "additionalProperties": False},
        "evidence": {"type": "object", "properties": {
            "source_candidate_ids": {"type": "array", "items": {"type": "string", "minLength": 1},
                                     "minItems": 2}},
            "required": ["source_candidate_ids"], "additionalProperties": False}},
     "required": ["action", "skill", "evidence"], "additionalProperties": False},
    {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["skip"]},
        "reason": {"type": "string", "minLength": 1}},
     "required": ["action", "reason"], "additionalProperties": False},
]}}


def _model_sop(record: dict[str, Any]) -> dict[str, Any]:
    """Whitelist model-visible fields; Store and trajectory refs stay local."""
    skill = record["skill"]
    if skill.get("granularity") != "task":
        raise ValueError("Task synthesis requires a task SOP")
    return {
        "canonical_instance_id": canonical_instance_id(str(record["canonical_instance_id"])),
        "candidate_id": str(record["candidate_id"]),
        "sop": {"title": skill["title"], "granularity": "general",
                "when_to_apply": skill["when_to_apply"],
                "rules": deepcopy(skill["rules"])},
        "candidate_context": deepcopy(record["candidate_context"]),
        "official_task_outcome": deepcopy(record["official_task_outcome"]),
    }


def task_sop_pairing_messages(anchor: dict[str, Any], candidates: list[dict[str, Any]],
                              *, prompt: str) -> list[dict[str, str]]:
    payload = {"anchor": _model_sop(anchor),
               "candidates": [_model_sop(item) for item in candidates]}
    return [{"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def task_sop_merge_messages(selected: list[dict[str, Any]], *, prompt: str) -> list[dict[str, str]]:
    if len(selected) not in {2, 3}:
        raise ValueError("Task synthesis needs two or three selected SOPs")
    payload = {"selected_sops": [_model_sop(item) for item in selected]}
    return [{"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def validate_task_sop_merge(value: dict[str, Any], selected: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate exact SOP citations without imposing per-rule source coverage."""
    benchmarks = {item.get("skill", {}).get("benchmark", "terminal-bench") for item in selected}
    if len(benchmarks) != 1 or not all(isinstance(item, str) and item for item in benchmarks):
        raise ValueError("Task synthesis needs one source benchmark")
    checked = validate_extraction(value, benchmark=next(iter(benchmarks)), expected_granularity="task")
    if checked["action"] == "skip":
        return checked
    validate_skill_candidate(checked["skill"])
    if set(value["skill"]) != {"title", "granularity", "when_to_apply", "rules"}:
        raise ValueError("Task synthesis skill must use the SOP-only fields")
    evidence = value.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != {"source_candidate_ids"}:
        raise ValueError("Task synthesis needs one evidence.source_candidate_ids set")
    ids = evidence["source_candidate_ids"]
    if (not isinstance(ids, list) or len(ids) not in {2, 3}
            or any(not isinstance(item, str) or not item for item in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError("Task synthesis needs two or three distinct source SOP candidate IDs")
    selected_by_id = {item["candidate_id"]: item["canonical_instance_id"] for item in selected}
    if (len(selected_by_id) != len(selected)
            or len(set(selected_by_id.values())) != len(selected)
            or not set(ids) <= set(selected_by_id)):
        raise ValueError("Task synthesis cites an unknown source SOP candidate ID")
    if len({selected_by_id[item] for item in ids}) < 2:
        raise ValueError("Task synthesis needs SOPs from two distinct tasks")
    return {**checked, "evidence": {"source_candidate_ids": ids}}
