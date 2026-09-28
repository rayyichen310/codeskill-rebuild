"""Shape and source-ID checks for direct segment extraction."""

from __future__ import annotations

from typing import Any

from .bank import validate_skill_candidate
from .pipeline import validate_extraction
from .types import canonical_instance_id


def segment_generation_schema(kind: str) -> dict[str, Any]:
    if kind not in {"task", "event"}:
        raise ValueError("unknown segment candidate kind")
    properties: dict[str, Any] = {
        "action": {"type": "string", "enum": ["generate"]},
        "skill": {"type": "object", "properties": {
            "title": {"type": "string", "minLength": 1},
            "granularity": {"type": "string", "enum": ["general" if kind == "task" else "event-driven"]},
            "when_to_apply": {"type": "string", "minLength": 1},
            "rules": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
        }, "required": ["title", "granularity", "when_to_apply", "rules"],
           "additionalProperties": False},
        "evidence": {"type": "object", "properties": {
            "step_ids": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
        }, "required": ["step_ids"], "additionalProperties": False},
    }
    required = ["action", "skill", "evidence"]
    if kind == "task":
        properties["candidate_context"] = {"type": "object", "properties": {
            "task_goal": {"type": "string", "minLength": 1},
            "whole_task_outcome": {"type": "string", "minLength": 1},
            **{name: {"type": "array", "items": {"type": "string", "minLength": 1}}
               for name in ("hard_constraints", "environment_assumptions",
                            "observed_results", "known_limitations")},
        }, "required": ["task_goal", "whole_task_outcome", "hard_constraints",
                        "environment_assumptions", "observed_results", "known_limitations"],
            "additionalProperties": False}
        required.append("candidate_context")
    return {"name": f"{kind}_candidate_from_segment_v1",
            "schema": {"oneOf": [
                {"type": "object", "properties": properties,
                 "required": required, "additionalProperties": False},
                {"type": "object", "properties": {
                    "action": {"type": "string", "enum": ["skip"]},
                    "reason": {"type": "string", "minLength": 1}},
                 "required": ["action", "reason"], "additionalProperties": False},
            ]}}


TASK_SEGMENT_SCHEMA = segment_generation_schema("task")
EVENT_SEGMENT_SCHEMA = segment_generation_schema("event")


def validate_segment_candidate(value: Any, *, kind: str, trace: dict[str, Any],
                               visible_ids: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("segment candidate must be an object")
    source = trace.get("source", {})
    benchmark = source.get("benchmark", "terminal-bench") if isinstance(source, dict) else None
    if not isinstance(benchmark, str) or not benchmark:
        raise ValueError("segment source has no benchmark")
    checked = validate_extraction(value, benchmark=benchmark,
                                  expected_granularity="task" if kind == "task" else "event")
    if checked["action"] == "skip":
        return checked
    validate_skill_candidate(checked["skill"])
    raw_evidence = value.get("evidence")
    if not isinstance(raw_evidence, dict) or set(raw_evidence) != {"step_ids"}:
        raise ValueError("segment candidate needs one skill-level evidence.step_ids list")
    ids = raw_evidence["step_ids"]
    if not isinstance(ids, list) or not ids or any(not isinstance(ref, str) or not ref for ref in ids):
        raise ValueError("segment candidate evidence.step_ids must be a nonempty string list")
    source_ids = {step.get("source_entry_id") for step in trace.get("steps", [])
                  if isinstance(step, dict)}
    visible = set(visible_ids) & source_ids
    if visible != set(visible_ids):
        raise ValueError("segment contains an unknown source ID")
    source = trace["source"]
    source_id = canonical_instance_id(str(source.get("canonical_instance_id", source.get("instance_id", ""))))
    if any(ref not in visible for ref in ids):
        raise ValueError("segment skill cites a source ID absent from its request")
    result = {**checked, "evidence": {
        "canonical_instance_id": source_id, "step_ids": list(dict.fromkeys(ids))}}
    if kind == "task":
        context = value.get("candidate_context")
        if not isinstance(context, dict):
            raise ValueError("Task candidate needs candidate_context")
        expected = {"task_goal", "whole_task_outcome", "hard_constraints",
                    "environment_assumptions", "observed_results", "known_limitations"}
        if set(context) != expected:
            raise ValueError("Task candidate_context has invalid fields")
        for field in ("task_goal", "whole_task_outcome"):
            if not isinstance(context[field], str) or not context[field].strip():
                raise ValueError(f"Task candidate_context.{field} is empty")
        for field in expected - {"task_goal", "whole_task_outcome"}:
            if not isinstance(context[field], list) or any(not isinstance(x, str) or not x.strip() for x in context[field]):
                raise ValueError(f"Task candidate_context.{field} is invalid")
        result["candidate_context"] = context
    return result
