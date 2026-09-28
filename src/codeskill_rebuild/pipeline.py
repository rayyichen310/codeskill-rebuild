"""Paper-aligned manager message builders and strict output adapters."""

from __future__ import annotations

import json
import hashlib
from copy import deepcopy
from typing import Any

from .code_examples import (
    CodeExampleError,
    apply_code_example_changes,
    code_example_id,
    materialize_code_example,
    materialize_code_example_from_stored_source,
    materialize_maintenance_code_example,
    validate_materialized_code_examples,
)
from .traces import source_step_ids
from .types import canonical_instance_id


def resolve_source_ids(
    values: list[str],
    allowed_values: list[str] | set[str],
    *,
    field: str,
) -> tuple[list[str], dict[str, str]]:
    """Resolve exact IDs or unambiguous prefixes without guessing.

    The historical manager prompts sometimes emit the first eight or more
    characters of a source ID.  Exact IDs always win, while a shortened ID is
    accepted only when it has at least eight characters and names exactly one
    allowed value.  Callers retain the returned expansion map as evidence.
    """
    allowed = [str(item) for item in allowed_values]
    if len(allowed) != len(set(allowed)):
        raise ValueError(f"{field} allowed source IDs are not unique")
    allowed_set = set(allowed)
    resolved: list[str] = []
    expansions: dict[str, str] = {}
    unknown: list[str] = []
    ambiguous: dict[str, list[str]] = {}
    for item in values:
        if item in allowed_set:
            resolved.append(item)
            continue
        matches = [candidate for candidate in allowed if len(item) >= 8 and candidate.startswith(item)]
        if len(matches) == 1:
            resolved.append(matches[0])
            expansions[item] = matches[0]
        elif len(matches) > 1:
            ambiguous[item] = sorted(matches)
        else:
            unknown.append(item)
    if ambiguous:
        raise ValueError(f"{field} cites ambiguous source ID prefixes: {ambiguous}")
    if unknown:
        raise ValueError(f"{field} cites unknown source IDs: {sorted(set(unknown))}")
    if len(resolved) != len(set(resolved)):
        raise ValueError(f"{field} resolves to duplicate source IDs")
    return resolved, expansions


def normalize_extraction_evidence(value: dict[str, Any], *, granularity: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Repair only unambiguous evidence layout, without changing source facts.

    The original manager JSON remains in its response record.  The returned
    record binds this derived input to that JSON and is persisted with the
    validator result, separately from the immutable response.
    """
    if value.get("action") != "generate":
        return value, None
    normalized = deepcopy(value)
    skill = normalized.get("skill")
    if not isinstance(skill, dict):
        return normalized, None
    changes: list[dict[str, Any]] = []
    nested = skill.get("evidence")
    root = normalized.get("evidence")
    if nested is not None:
        if not isinstance(nested, dict):
            raise ValueError("skill.evidence must be an object")
        if root is not None and root != nested:
            raise ValueError("root evidence conflicts with skill.evidence")
        if root is None:
            normalized["evidence"] = skill.pop("evidence")
            changes.append({"kind": "move_nested_evidence"})
        else:
            skill.pop("evidence")
            changes.append({"kind": "deduplicate_identical_nested_evidence"})
    if granularity == "task" and isinstance(normalized.get("evidence"), dict):
        rules = normalized["evidence"].get("rule_evidence")
        if isinstance(rules, list):
            for rule_index, rule in enumerate(rules):
                if not isinstance(rule, dict) or not isinstance(rule.get("sources"), list):
                    continue
                grouped: dict[str, dict[str, Any]] = {}
                merged: list[dict[str, Any]] = []
                for source in rule["sources"]:
                    if not isinstance(source, dict) or not isinstance(source.get("canonical_instance_id"), str):
                        merged.append(source)
                        continue
                    source_id = source["canonical_instance_id"]
                    prior = grouped.get(source_id)
                    if prior is None:
                        copied = deepcopy(source)
                        grouped[source_id] = copied
                        merged.append(copied)
                        continue
                    if set(source) != {"canonical_instance_id", "step_ids"} or set(prior) != {"canonical_instance_id", "step_ids"}:
                        raise ValueError(f"task rule evidence[{rule_index}] duplicate source entries conflict")
                    if not isinstance(source["step_ids"], list) or not isinstance(prior["step_ids"], list):
                        raise ValueError(f"task rule evidence[{rule_index}] duplicate source step_ids must be lists")
                    if (not source["step_ids"] or not prior["step_ids"]
                            or not all(isinstance(step_id, str) and step_id for step_id in [*prior["step_ids"], *source["step_ids"]])):
                        raise ValueError(f"task rule evidence[{rule_index}] duplicate source step_ids are invalid")
                    prior["step_ids"] = list(dict.fromkeys([*prior["step_ids"], *source["step_ids"]]))
                    changes.append({"kind": "merge_same_source_steps", "rule_index": rule_index, "canonical_instance_id": source_id})
                if len(merged) != len(rule["sources"]):
                    rule["sources"] = merged
    if not changes:
        return normalized, None
    digest = lambda item: hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return normalized, {
        "schema_version": 1,
        "input_model_json_sha256": digest(value),
        "normalized_model_json_sha256": digest(normalized),
        "changes": changes,
    }


def paper_skill_to_internal(skill: dict[str, Any], *, benchmark: str, expected_granularity: str) -> dict[str, Any]:
    paper_value = skill.get("granularity")
    expected_paper = "general" if expected_granularity == "task" else "event-driven"
    if paper_value != expected_paper:
        raise ValueError(f"Paper granularity must be {expected_paper}, got {paper_value!r}")
    return {
        "title": skill.get("title"),
        "granularity": expected_granularity,
        "when_to_apply": skill.get("when_to_apply"),
        "rules": skill.get("rules"),
        "benchmark": benchmark,
    }


def internal_skill_to_paper(skill: dict[str, Any]) -> dict[str, Any]:
    """Project an internal bank skill into Fig.8/Fig.9 paper vocabulary.

    The runtime bank uses ``task`` and ``event`` as its compact internal
    labels, while the paper prompts require ``general`` and ``event-driven``.
    Keep all other fields (including retrieved skill IDs) intact so Fig.9 can
    select a merge target, but never expose the internal label to the manager.
    """
    if not isinstance(skill, dict):
        raise ValueError("internal skill must be an object")
    paper_granularity = {"task": "general", "event": "event-driven"}.get(skill.get("granularity"))
    if paper_granularity is None:
        raise ValueError(f"internal skill has unsupported granularity {skill.get('granularity')!r}")
    result = deepcopy(skill)
    result["granularity"] = paper_granularity
    if "code_examples" in result:
        result["code_examples"] = _manager_visible_code_examples(result["code_examples"])
    return result


def _manager_visible_code_examples(value: Any) -> list[dict[str, Any]]:
    """Project stored examples without duplicating raw operations in prompts."""
    projected: list[dict[str, Any]] = []
    for item in validate_materialized_code_examples(value):
        projected.append(
            {
                "schema_version": item["schema_version"],
                "example_id": code_example_id(item),
                "language": item["language"],
                "purpose": item["purpose"],
                "source_binding": deepcopy(item["source_binding"]),
                "source_evidence_hashes": {
                    "operation_sha256": item["source_operation"]["sha256"],
                    "observation_sha256": item["source_observation"]["sha256"],
                },
                "generated_example": deepcopy(item["generated_example"]),
                "evidence_boundary": deepcopy(item["evidence_boundary"]),
            }
        )
    return projected


def trajectory_prompt_steps(trace: dict[str, Any], *, full_key: str = "steps") -> dict[str, Any]:
    """Label budget summaries separately from complete original trajectories."""
    if trace.get("trajectory_input_mode") == "evidence_compacted":
        return {
            "evidence_compacted": True,
            "evidence_summaries": trace["evidence_summaries"],
            "original_cited_fragments": trace["steps"],
        }
    return {full_key: trace["steps"]}


def description_messages(trace: dict[str, Any], *, custom_prompt: str) -> list[dict[str, str]]:
    evidence = {
        "task_context": trace["instruction"],
        "task_name": trace["source"]["task_name"],
        "official_outcome": trace["outcome"],
        **trajectory_prompt_steps(trace),
    }
    return [
        {
            "role": "system",
            "content": custom_prompt,
        },
        {
            "role": "user",
            "content": json.dumps(evidence, ensure_ascii=False),
        },
    ]


def pairing_messages(anchor: dict[str, Any], candidates: list[dict[str, Any]], *, custom_prompt: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": custom_prompt},
        {"role": "user", "content": json.dumps({"anchor": anchor, "candidates": candidates}, ensure_ascii=False)},
    ]


def validate_description(value: dict[str, Any], trace: dict[str, Any]) -> dict[str, Any]:
    required = ("task_family", "observed_obstacle", "attempted_procedure", "observed_outcome", "source_step_ids")
    if not isinstance(value, dict) or any(key not in value for key in required):
        raise ValueError("description output lacks D01 fields")
    if not all(isinstance(value[key], str) and value[key].strip() for key in required[:-1]):
        raise ValueError("description fields must be nonempty strings")
    family = value["task_family"].strip().casefold()
    source = trace.get("source", {})
    forbidden_family_labels = {
        str(source.get(key, "")).strip().casefold()
        for key in ("canonical_instance_id", "instance_id", "task_name", "official_task_name")
    }
    forbidden_family_labels.discard("")
    if family in forbidden_family_labels:
        raise ValueError("description task_family must be an observed reusable activity label, not an instance identifier")
    step_ids = value["source_step_ids"]
    if not isinstance(step_ids, list) or not step_ids or not all(isinstance(item, str) for item in step_ids):
        raise ValueError("description source_step_ids must be a nonempty string list")
    allowed_step_ids = source_step_ids(trace)
    resolved, expansions = resolve_source_ids(step_ids, allowed_step_ids, field="description source_step_ids")
    preserved_expansions = value.get("source_step_id_expansions", {})
    if not isinstance(preserved_expansions, dict) or not all(
        isinstance(prefix, str) and isinstance(full_id, str)
        for prefix, full_id in preserved_expansions.items()
    ):
        raise ValueError("description source_step_id_expansions must be a string mapping")
    for prefix, full_id in preserved_expansions.items():
        matches = [candidate for candidate in allowed_step_ids if len(prefix) >= 8 and candidate.startswith(prefix)]
        if len(matches) != 1 or matches[0] != full_id or prefix == full_id or full_id not in resolved:
            raise ValueError("description source_step_id_expansions is inconsistent with cited source steps")
        if prefix in expansions and expansions[prefix] != full_id:
            raise ValueError("description source_step_id_expansions conflicts with resolved source steps")
    expansions = {**preserved_expansions, **expansions}
    result = {key: value[key] for key in required}
    result["source_step_ids"] = resolved
    if expansions:
        result["source_step_id_expansions"] = expansions
    return result


def validate_pairing(
    value: dict[str, Any],
    *,
    anchor_id: str,
    candidate_ids: set[str],
    require_shared_evidence: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("action"), str):
        raise ValueError("pairing output lacks action")
    if value["action"] == "no_related_group":
        if not isinstance(value.get("reason"), str) or not value["reason"].strip():
            raise ValueError("no_related_group needs a reason")
        return {"action": "no_related_group", "reason": value["reason"]}
    if value["action"] != "select":
        raise ValueError("pairing action must be select or no_related_group")
    selected_raw = value.get("selected_instance_ids")
    if not isinstance(selected_raw, list) or len(selected_raw) not in {2, 3} or not all(isinstance(item, str) for item in selected_raw):
        raise ValueError("selected_instance_ids must name 2–3 instances")
    selected, selected_expansions = resolve_source_ids(
        selected_raw,
        candidate_ids | {anchor_id},
        field="pairing selected_instance_ids",
    )
    if anchor_id not in selected:
        raise ValueError("selected group must be unique and include its anchor")
    if not isinstance(value.get("reason"), str) or not value["reason"].strip():
        raise ValueError("selected group needs a reason")
    result = {"action": "select", "selected_instance_ids": selected, "reason": value["reason"]}
    source_id_expansions: dict[str, dict[str, str]] = {}
    if selected_expansions:
        source_id_expansions["selected_instance_ids"] = selected_expansions
    if require_shared_evidence:
        shared = value.get("shared_subprocedure")
        evidence = value.get("instance_evidence")
        if not isinstance(shared, str) or not shared.strip():
            raise ValueError("R011 selected group needs a nonempty shared_subprocedure")
        if not isinstance(evidence, list) or len(evidence) != len(selected):
            raise ValueError("R011 selected group needs one instance_evidence entry per selected instance")
        by_id: dict[str, str] = {}
        for entry in evidence:
            if not isinstance(entry, dict):
                raise ValueError("R011 instance_evidence entries must be objects")
            instance_id = entry.get("canonical_instance_id")
            description_evidence = entry.get("description_evidence")
            if not isinstance(instance_id, str):
                raise ValueError("R011 instance_evidence must cite only selected instances")
            resolved_instance_ids, evidence_expansions = resolve_source_ids(
                [instance_id], selected, field="pairing instance_evidence canonical_instance_id"
            )
            instance_id = resolved_instance_ids[0]
            if evidence_expansions:
                source_id_expansions.setdefault("instance_evidence", {}).update(evidence_expansions)
            if not isinstance(description_evidence, str) or not description_evidence.strip():
                raise ValueError("R011 instance_evidence needs nonempty description_evidence")
            if instance_id in by_id:
                raise ValueError("R011 instance_evidence must not duplicate an instance")
            by_id[instance_id] = description_evidence
        if set(by_id) != set(selected):
            raise ValueError("R011 instance_evidence must cover every selected instance")
        result["shared_subprocedure"] = shared
        result["instance_evidence"] = [
            {"canonical_instance_id": instance_id, "description_evidence": by_id[instance_id]}
            for instance_id in selected
        ]
    if source_id_expansions:
        result["source_id_expansions"] = source_id_expansions
    return result


def validate_extraction(value: dict[str, Any], *, benchmark: str, expected_granularity: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("action"), str):
        raise ValueError("extraction output lacks action")
    if value["action"] == "skip":
        if not isinstance(value.get("reason"), str) or not value["reason"].strip():
            raise ValueError("extraction skip needs a reason")
        return {"action": "skip", "reason": value["reason"]}
    if value["action"] != "generate" or not isinstance(value.get("skill"), dict):
        raise ValueError("extraction action must be generate or skip")
    return {"action": "generate", "skill": paper_skill_to_internal(value["skill"], benchmark=benchmark, expected_granularity=expected_granularity)}


def task_extraction_messages(traces: list[dict[str, Any]], *, paper_prompt: str) -> list[dict[str, str]]:
    if any(not trace.get("text_manager_eligible", False) for trace in traces):
        raise ValueError("Task extraction cannot send multimodal-pending source evidence to a text manager")
    instances = {canonical_instance_id(trace["source"]["instance_id"]) for trace in traces}
    if len(traces) not in {2, 3} or len(instances) != len(traces):
        raise ValueError("Task extraction requires 2–3 different instances")
    evidence = [
        {
            "task_context": trace["instruction"],
            "source": trace["source"],
            **trajectory_prompt_steps(trace),
            "outcome": trace["outcome"],
        }
        for trace in traces
    ]
    return [{"role": "system", "content": paper_prompt}, {"role": "user", "content": json.dumps({"trajectories": evidence}, ensure_ascii=False)}]


def task_candidate_extraction_messages(trace: dict[str, Any], *, prompt: str) -> list[dict[str, str]]:
    """Compose the isolated single-trajectory SOP-candidate request."""
    if not trace.get("text_manager_eligible", False):
        raise ValueError("Task candidate extraction cannot send multimodal-pending evidence to a text manager")
    evidence = {
        "task_context": trace["instruction"],
        "source": trace["source"],
        **trajectory_prompt_steps(trace),
        "outcome": trace["outcome"],
    }
    return [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps({"trajectory": evidence}, ensure_ascii=False)}]


def task_candidate_merge_messages(
    candidates: list[dict[str, Any]],
    traces: list[dict[str, Any]],
    *,
    paper_prompt: str,
) -> list[dict[str, str]]:
    """Compose Fig.6 from selected SOP candidates plus their raw evidence."""
    messages = task_extraction_messages(traces, paper_prompt=paper_prompt)
    if len(candidates) != len(traces):
        raise ValueError("Task merge needs one single-task candidate per source trajectory")
    content = json.loads(messages[1]["content"])
    projected_candidates = deepcopy(candidates)
    for candidate in projected_candidates:
        skill = candidate.get("skill") if isinstance(candidate, dict) else None
        if isinstance(skill, dict) and "code_examples" in skill:
            skill["code_examples"] = _manager_visible_code_examples(skill["code_examples"])
    content["single_task_candidates"] = projected_candidates
    messages[1]["content"] = json.dumps(content, ensure_ascii=False)
    return messages


def _materialize_skill_code_examples(
    raw_skill: dict[str, Any],
    trace_by_id: dict[str, dict[str, Any]],
    *,
    visible_step_ids_by_source: dict[str, list[str]] | None,
) -> list[dict[str, Any]]:
    """Bind manager-generated examples to exact raw tool calls and results."""
    raw_examples = raw_skill.get("code_examples")
    if raw_examples is None:
        return []
    if not isinstance(raw_examples, list):
        raise ValueError("skill code_examples must be a list when supplied")
    if not raw_examples:
        return []
    materialized: list[dict[str, Any]] = []
    seen_examples: set[str] = set()
    for index, raw_example in enumerate(raw_examples):
        if not isinstance(raw_example, dict):
            raise ValueError(f"skill code_examples[{index}] must be an object")
        source = raw_example.get("source")
        if not isinstance(source, dict):
            raise ValueError(f"skill code_examples[{index}] needs a source reference")
        required_source_fields = {
            "canonical_instance_id",
            "action_step_id",
            "tool_call_id",
            "result_step_id",
        }
        if set(source) != required_source_fields:
            raise ValueError(
                f"skill code_examples[{index}].source must contain exactly {sorted(required_source_fields)}"
            )
        if not all(isinstance(source.get(field), str) and source[field] for field in required_source_fields):
            raise ValueError(f"skill code_examples[{index}] source references must be nonempty strings")
        resolved_sources, source_expansions = resolve_source_ids(
            [source["canonical_instance_id"]],
            list(trace_by_id),
            field=f"skill code_examples[{index}] canonical_instance_id",
        )
        source_id = resolved_sources[0]
        trace = trace_by_id[source_id]
        entries = trace.get("steps")
        if not isinstance(entries, list):
            raise ValueError(f"skill code_examples[{index}] source trajectory has no steps")
        by_step_id = {
            str(entry["source_entry_id"]): entry
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("source_entry_id"), str)
        }
        order = {
            str(entry["source_entry_id"]): position
            for position, entry in enumerate(entries)
            if isinstance(entry, dict) and isinstance(entry.get("source_entry_id"), str)
        }
        resolved_steps, step_expansions = resolve_source_ids(
            [source["action_step_id"], source["result_step_id"]],
            list(by_step_id),
            field=f"skill code_examples[{index}] source step IDs",
        )
        action_step_id, result_step_id = resolved_steps
        action_step = by_step_id[action_step_id]
        result_step = by_step_id[result_step_id]
        if action_step.get("role") != "assistant" or result_step.get("role") != "toolResult":
            raise ValueError("code example source must bind an assistant action to a toolResult")
        if order[action_step_id] >= order[result_step_id]:
            raise ValueError("code example source result must follow its assistant action")
        controls = {
            str(control)
            for control in trace.get("historical_compaction", {}).get("control_event_ids", [])
            if isinstance(control, str)
        }
        if {action_step_id, result_step_id} & controls:
            raise ValueError("code example source cannot cite native compaction controls")
        declared_raw_ids = trace.get("historical_compaction", {}).get("raw_message_step_ids")
        raw_ids = (
            set(declared_raw_ids)
            if isinstance(declared_raw_ids, list) and all(isinstance(step_id, str) for step_id in declared_raw_ids)
            else set(by_step_id)
        )
        if not {action_step_id, result_step_id} <= raw_ids:
            raise ValueError("code example source lacks preserved raw action/result content")
        if visible_step_ids_by_source is not None:
            visible = visible_step_ids_by_source.get(source_id)
            if not isinstance(visible, list) or not {action_step_id, result_step_id} <= set(visible):
                raise ValueError("code example source action/result is absent from supplied original fragments")
        calls = action_step.get("assistant", {}).get("tool_calls", []) if isinstance(action_step.get("assistant"), dict) else []
        matching_calls = [
            call
            for call in calls
            if isinstance(call, dict) and call.get("tool_call_id") == source["tool_call_id"]
        ]
        if len(matching_calls) != 1:
            raise ValueError("code example source tool_call_id must name exactly one call in the cited action")
        result_call_id = result_step.get("tool_result", {}).get("tool_call_id") if isinstance(result_step.get("tool_result"), dict) else None
        if result_call_id != source["tool_call_id"]:
            raise ValueError("code example source action/result does not share the cited tool_call_id")
        manager_fields = {key: deepcopy(value) for key, value in raw_example.items() if key != "source"}
        try:
            checked = materialize_code_example(
                manager_fields,
                canonical_instance_id=source_id,
                action_step_id=action_step_id,
                result_step_id=result_step_id,
                tool_call=matching_calls[0],
                result_step=result_step,
                whole_task_outcome=trace.get("outcome"),
            )
        except CodeExampleError as error:
            raise ValueError(f"skill code_examples[{index}] is invalid: {error}") from error
        expansions: dict[str, Any] = {}
        if source_expansions:
            expansions["canonical_instance_id"] = source_expansions
        if step_expansions:
            expansions["step_ids"] = step_expansions
        if expansions:
            checked["source_id_expansions"] = expansions
        identity = code_example_id(checked)
        if identity in seen_examples:
            raise ValueError("skill code_examples cannot duplicate one exact code-example revision")
        seen_examples.add(identity)
        materialized.append(checked)
    return materialized


def materialize_lifecycle_code_example(
    value: dict[str, Any],
    trace: dict[str, Any],
    *,
    visible_step_ids_by_source: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Materialize one evolution-added example from the current raw trajectory."""
    source = value.get("source") if isinstance(value, dict) else None
    trace_source = trace.get("source") if isinstance(trace, dict) else None
    raw_source_id = trace_source.get("canonical_instance_id", trace_source.get("instance_id")) if isinstance(trace_source, dict) else None
    if not isinstance(source, dict) or not isinstance(raw_source_id, str) or not raw_source_id:
        raise ValueError("lifecycle code example needs a source and a trajectory identity")
    source_id = canonical_instance_id(raw_source_id)
    checked = _materialize_skill_code_examples(
        {"code_examples": [value]},
        {source_id: trace},
        visible_step_ids_by_source=visible_step_ids_by_source,
    )
    return checked[0]


def _compact_prior_event_candidates(value: Any) -> list[dict[str, Any]]:
    """Keep reusable content and references without repeating stored raw bytes."""
    if not isinstance(value, list):
        raise ValueError("previous event candidates must be a list")
    compact: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"previous event candidate[{index}] must be an object")
        if isinstance(item.get("skill"), dict):
            skill = item["skill"]
            content: dict[str, Any] = {
                "title": skill.get("title"),
                "when_to_apply": skill.get("when_to_apply"),
                "rules": deepcopy(skill.get("rules")),
            }
            if "code_examples" in skill:
                content["code_examples"] = _manager_visible_code_examples(skill["code_examples"])
            compact.append({"candidate_id": item.get("candidate_id"), "content": content})
            continue
        raw_content = item.get("content")
        if not isinstance(raw_content, dict):
            raise ValueError(f"previous event candidate[{index}] needs compact content or a skill")
        content = {
            "title": raw_content.get("title"),
            "when_to_apply": raw_content.get("when_to_apply"),
            "rules": deepcopy(raw_content.get("rules")),
        }
        if "code_examples" in raw_content:
            content["code_examples"] = _manager_visible_code_examples(raw_content["code_examples"])
        projected = {"candidate_id": item.get("candidate_id"), "content": content}
        step_references = item.get("step_references")
        if isinstance(step_references, dict):
            projected["step_references"] = {
                key: deepcopy(step_references.get(key, []))
                for key in ("trigger_step_ids", "response_step_ids", "outcome_step_ids")
            }
        if isinstance(item.get("exact_content_fingerprint"), str):
            projected["exact_content_fingerprint"] = item["exact_content_fingerprint"]
        compact.append(projected)
    return compact


def _validate_task_rules_with_evidence(
    value: dict[str, Any],
    traces: list[dict[str, Any]],
    *,
    benchmark: str,
    allowed_source_counts: set[int],
    visible_step_ids_by_source: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Require every task-level rule to cite an action/result in every source."""
    base = validate_extraction(value, benchmark=benchmark, expected_granularity="task")
    if base["action"] == "skip":
        return base
    value, evidence_normalization = normalize_extraction_evidence(value, granularity="task")
    if visible_step_ids_by_source is not None and not isinstance(visible_step_ids_by_source, dict):
        raise ValueError("task evidence visible-step record must be a source mapping")
    if len(traces) not in allowed_source_counts:
        expected = " or ".join(str(value) for value in sorted(allowed_source_counts))
        raise ValueError(f"task evidence requires {expected} source trajectories")
    trace_by_id: dict[str, dict[str, Any]] = {}
    for trace in traces:
        source = trace.get("source")
        if not isinstance(source, dict):
            raise ValueError("task evidence source trajectory lacks source metadata")
        raw_instance_id = source.get("canonical_instance_id", source.get("instance_id"))
        if not isinstance(raw_instance_id, str):
            raise ValueError("task evidence source trajectory lacks an instance ID")
        instance_id = canonical_instance_id(raw_instance_id)
        if instance_id in trace_by_id:
            raise ValueError("task evidence requires different source instances")
        trace_by_id[instance_id] = trace
    sidecar = value.get("evidence")
    if not isinstance(sidecar, dict):
        raise ValueError("generated task skill requires an evidence sidecar")
    rule_evidence = sidecar.get("rule_evidence")
    rules = base["skill"]["rules"]
    if not isinstance(rule_evidence, list) or len(rule_evidence) != len(rules):
        raise ValueError("task evidence requires one rule_evidence record per rule")

    normalized_rules: list[dict[str, Any]] = []
    computed_expansions: dict[str, Any] = {}
    expected_source_ids = list(trace_by_id)
    for expected_index, item in enumerate(rule_evidence):
        if not isinstance(item, dict) or item.get("rule_index") != expected_index:
            raise ValueError("task rule_evidence indices must cover rules in order")
        sources = item.get("sources")
        if not isinstance(sources, list) or len(sources) != len(expected_source_ids):
            raise ValueError("each task rule needs one source evidence entry per selected instance")
        by_source: dict[str, list[str]] = {}
        rule_expansions: dict[str, Any] = {}
        for source_evidence in sources:
            if not isinstance(source_evidence, dict):
                raise ValueError("task rule source evidence entries must be objects")
            raw_source_id = source_evidence.get("canonical_instance_id")
            if not isinstance(raw_source_id, str):
                raise ValueError("task rule source evidence must cite a selected instance")
            resolved_sources, source_expansions = resolve_source_ids(
                [raw_source_id], expected_source_ids, field=f"task rule evidence[{expected_index}] source"
            )
            source_id = resolved_sources[0]
            if source_id in by_source:
                raise ValueError("task rule evidence must not duplicate a source instance")
            step_ids = source_evidence.get("step_ids")
            if not isinstance(step_ids, list) or not step_ids or not all(isinstance(step_id, str) for step_id in step_ids):
                raise ValueError("task rule source evidence needs nonempty step_ids")
            trace = trace_by_id[source_id]
            entries = trace.get("steps")
            if not isinstance(entries, list):
                raise ValueError("task evidence source trajectory has no steps")
            positions = {
                str(entry["source_entry_id"]): entry
                for entry in entries
                if isinstance(entry, dict) and "source_entry_id" in entry
            }
            step_order = {
                str(entry["source_entry_id"]): index
                for index, entry in enumerate(entries)
                if isinstance(entry, dict) and "source_entry_id" in entry
            }
            resolved_steps, step_expansions = resolve_source_ids(
                step_ids, list(positions), field=f"task rule evidence[{expected_index}] {source_id} step_ids"
            )
            controls = {
                str(control)
                for control in trace.get("historical_compaction", {}).get("control_event_ids", [])
                if isinstance(control, str)
            }
            cited_controls = sorted(set(resolved_steps) & controls)
            if cited_controls:
                raise ValueError(f"task rule evidence cites native compaction controls: {cited_controls}")
            declared_raw_ids = trace.get("historical_compaction", {}).get("raw_message_step_ids")
            raw_ids = (
                set(declared_raw_ids)
                if isinstance(declared_raw_ids, list) and all(isinstance(step_id, str) for step_id in declared_raw_ids)
                else set(positions)
            )
            missing_raw = sorted(set(resolved_steps) - raw_ids)
            if missing_raw:
                raise ValueError(f"task rule evidence cites steps without preserved raw source content: {missing_raw}")
            if visible_step_ids_by_source is not None:
                visible = visible_step_ids_by_source.get(source_id)
                if not isinstance(visible, list):
                    raise ValueError(f"task rule evidence has no visible-step record for source {source_id}")
                hidden = sorted(set(resolved_steps) - set(visible))
                if hidden:
                    raise ValueError(f"task rule evidence cites steps absent from supplied original fragments: {hidden}")
            action_ids = [step_id for step_id in resolved_steps if positions[step_id].get("role") == "assistant"]
            result_ids = [step_id for step_id in resolved_steps if positions[step_id].get("role") == "toolResult"]
            ordered_pairs = [
                (action_id, result_id)
                for action_id in action_ids
                for result_id in result_ids
                if step_order[action_id] < step_order[result_id]
            ]
            if not ordered_pairs:
                raise ValueError("each task rule source needs an assistant action followed by an observed tool result")
            assistant_call_ids = {
                call.get("tool_call_id")
                for step_id in resolved_steps
                for call in (
                    positions[step_id].get("assistant", {}).get("tool_calls", [])
                    if isinstance(positions[step_id].get("assistant"), dict)
                    else []
                )
                if isinstance(call, dict) and isinstance(call.get("tool_call_id"), str)
            }
            result_call_ids = {
                positions[step_id]["tool_result"].get("tool_call_id")
                for step_id in resolved_steps
                if isinstance(positions[step_id].get("tool_result"), dict)
                and isinstance(positions[step_id]["tool_result"].get("tool_call_id"), str)
            }
            if assistant_call_ids or result_call_ids:
                if not any(
                    isinstance(positions[result_id].get("tool_result"), dict)
                    and any(
                        isinstance(call, dict)
                        and call.get("tool_call_id") == positions[result_id]["tool_result"].get("tool_call_id")
                        for call in (
                            positions[action_id]["assistant"].get("tool_calls", [])
                            if isinstance(positions[action_id].get("assistant"), dict) else []
                        )
                    )
                    for action_id, result_id in ordered_pairs
                ):
                    raise ValueError("task rule source action/result evidence does not share a tool call ID in an ordered pair")
            by_source[source_id] = resolved_steps
            if source_expansions:
                rule_expansions.setdefault("instance_ids", {}).update(source_expansions)
            if step_expansions:
                rule_expansions.setdefault("step_ids", {})[source_id] = step_expansions
        if set(by_source) != set(expected_source_ids):
            raise ValueError("task rule evidence must cover every selected instance")
        normalized_rules.append(
            {
                "rule_index": expected_index,
                "sources": [
                    {"canonical_instance_id": source_id, "step_ids": by_source[source_id]}
                    for source_id in expected_source_ids
                ],
            }
        )
        if rule_expansions:
            computed_expansions[str(expected_index)] = rule_expansions
    materialized_examples = _materialize_skill_code_examples(
        value["skill"],
        trace_by_id,
        visible_step_ids_by_source=visible_step_ids_by_source,
    )
    if materialized_examples:
        base["skill"]["code_examples"] = materialized_examples
    normalized_evidence: dict[str, Any] = {"rule_evidence": normalized_rules}
    if computed_expansions:
        normalized_evidence["source_id_expansions"] = {"rule_evidence": computed_expansions}
    checked = {**base, "evidence": normalized_evidence}
    if evidence_normalization is not None:
        checked["evidence_normalization"] = evidence_normalization
    return checked


def validate_task_candidate_with_evidence(
    value: dict[str, Any],
    trace: dict[str, Any],
    *,
    benchmark: str,
    visible_step_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Validate one isolated task SOP candidate without making it publishable."""
    source = trace.get("source")
    if not isinstance(source, dict):
        raise ValueError("task candidate trajectory lacks source metadata")
    source_id = canonical_instance_id(str(source.get("canonical_instance_id", source.get("instance_id", ""))))
    visible = {source_id: visible_step_ids} if visible_step_ids is not None else None
    checked = _validate_task_rules_with_evidence(
        value,
        [trace],
        benchmark=benchmark,
        allowed_source_counts={1},
        visible_step_ids_by_source=visible,
    )
    if checked["action"] == "skip":
        return checked
    context = value.get("candidate_context")
    if not isinstance(context, dict):
        raise ValueError("generated task candidate requires candidate_context")
    normalized_context: dict[str, Any] = {}
    for field in ("task_goal", "whole_task_outcome"):
        item = context.get(field)
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"task candidate {field} must be nonempty text")
        normalized_context[field] = item
    for field in (
        "hard_constraints",
        "environment_assumptions",
        "observed_results",
        "known_limitations",
    ):
        items = context.get(field)
        if not isinstance(items, list) or not all(isinstance(item, str) and item.strip() for item in items):
            raise ValueError(f"task candidate {field} must be a list of nonempty strings")
        normalized_context[field] = items
    return {**checked, "candidate_context": normalized_context}


def validate_task_extraction_with_evidence(
    value: dict[str, Any],
    traces: list[dict[str, Any]],
    *,
    benchmark: str,
    visible_step_ids_by_source: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Validate a publishable task skill against 2–3 selected sources."""
    return _validate_task_rules_with_evidence(
        value,
        traces,
        benchmark=benchmark,
        allowed_source_counts={2, 3},
        visible_step_ids_by_source=visible_step_ids_by_source,
    )


def event_extraction_messages(trace: dict[str, Any], *, paper_prompt: str, prior_event_ids: list[str]) -> list[dict[str, str]]:
    if not trace.get("text_manager_eligible", False):
        raise ValueError("Event extraction cannot send multimodal-pending source evidence to a text manager")
    evidence = {
        "task_context": trace["instruction"],
        "source": trace["source"],
        **trajectory_prompt_steps(trace, full_key="full_trajectory"),
        "outcome": trace["outcome"],
        "previous_event_candidate_ids": prior_event_ids,
    }
    return [{"role": "system", "content": paper_prompt}, {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)}]


def event_extraction_with_evidence_messages(
    trace: dict[str, Any],
    *,
    runtime_prompt: str,
    prior_event_ids: list[str],
    prior_event_candidates: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Fig.7-compatible output with a declared D04 provenance sidecar delta."""
    if not trace.get("text_manager_eligible", False):
        raise ValueError("Event extraction cannot send multimodal-pending source evidence to a text manager")
    evidence = {
        "task_context": trace["instruction"],
        "source": trace["source"],
        **trajectory_prompt_steps(trace, full_key="full_trajectory"),
        "outcome": trace["outcome"],
        "previous_event_candidate_ids": prior_event_ids,
        # R012's second and third attempts must be able to see compact
        # candidate content and local step references, not opaque IDs alone.
        # The complete source trajectory remains present above.
        "previous_event_candidates": _compact_prior_event_candidates(prior_event_candidates or []),
    }
    return [{"role": "system", "content": runtime_prompt}, {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)}]


def event_evidence_repair_messages(
    trace: dict[str, Any],
    *,
    repair_prompt: str,
    original_model_output: dict[str, Any],
    validator_error: str,
) -> list[dict[str, str]]:
    """R011's one-shot provenance-only repair input.

    The normalized trace is deliberately unprojected here.  The repair can
    cite any observed message but must preserve the original skill object
    byte-for-byte at the JSON value level.
    """
    if not trace.get("text_manager_eligible", False):
        raise ValueError("Event repair cannot send multimodal-pending source evidence to a text manager")
    evidence = {
        "task_context": trace["instruction"],
        "source": trace["source"],
        "original_full_trajectory": trace["steps"],
        "outcome": trace["outcome"],
        "original_model_output": original_model_output,
        "exact_validator_error": validator_error,
    }
    return [{"role": "system", "content": repair_prompt}, {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)}]


def validate_event_evidence_repair(
    value: dict[str, Any],
    trace: dict[str, Any],
    *,
    original_model_output: dict[str, Any],
    benchmark: str,
) -> dict[str, Any]:
    """Validate R011 repair while refusing a rewritten candidate skill."""
    if not isinstance(value, dict) or not isinstance(value.get("action"), str):
        raise ValueError("event evidence repair output lacks action")
    if value["action"] == "cannot_repair_evidence":
        if not isinstance(value.get("reason"), str) or not value["reason"].strip():
            raise ValueError("cannot_repair_evidence needs a reason")
        return {"action": "cannot_repair_evidence", "reason": value["reason"]}
    if value["action"] != "generate":
        raise ValueError("event evidence repair action must be generate or cannot_repair_evidence")
    original_skill = original_model_output.get("skill") if isinstance(original_model_output, dict) else None
    if not isinstance(original_skill, dict):
        raise ValueError("event evidence repair original output has no generated skill")
    if value.get("skill") != original_skill:
        raise ValueError("event evidence repair must preserve the original skill JSON exactly")
    return validate_event_extraction_with_evidence(value, trace, benchmark=benchmark)


def validate_event_extraction_with_evidence(
    value: dict[str, Any],
    trace: dict[str, Any],
    *,
    benchmark: str,
    visible_step_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Require a local observed trigger, later response/outcome, and rule evidence."""
    base = validate_extraction(value, benchmark=benchmark, expected_granularity="event")
    if base["action"] == "skip":
        return base
    value, evidence_normalization = normalize_extraction_evidence(value, granularity="event")
    sidecar = value.get("evidence")
    if not isinstance(sidecar, dict):
        raise ValueError("generated event skill requires an evidence sidecar")
    entries = trace["steps"]
    positions = {str(entry["source_entry_id"]): index for index, entry in enumerate(entries)}
    roles = {str(entry["source_entry_id"]): entry.get("role") for entry in entries}
    compaction_controls = {
        str(item)
        for item in trace.get("historical_compaction", {}).get("control_event_ids", [])
        if isinstance(item, str)
    }
    declared_raw_ids = trace.get("historical_compaction", {}).get("raw_message_step_ids")
    raw_evidence_ids = set(declared_raw_ids) if isinstance(declared_raw_ids, list) and all(isinstance(item, str) for item in declared_raw_ids) else set(positions)
    first_user_index = next((index for index, entry in enumerate(entries) if entry.get("role") == "user"), None)
    if first_user_index is None:
        raise ValueError("trace has no initial user message")

    def ids(name: str) -> list[str]:
        selected = sidecar.get(name)
        if not isinstance(selected, list) or not selected or not all(isinstance(item, str) for item in selected):
            raise ValueError(f"event evidence {name} must be a nonempty string list")
        controls = sorted(set(selected) & compaction_controls)
        if controls:
            raise ValueError(f"event evidence {name} cites a native compaction summary/control, not an observed raw source step: {controls}")
        selected, expansions = resolve_source_ids(selected, list(positions), field=f"event evidence {name}")
        if expansions:
            source_id_expansions[name] = expansions
        missing_raw = sorted(set(selected) - raw_evidence_ids)
        if missing_raw:
            raise ValueError(f"event evidence {name} cites a step without preserved raw source content: {missing_raw}")
        if visible_step_ids is not None:
            hidden = sorted(set(selected) - visible_step_ids)
            if hidden:
                raise ValueError(f"event evidence {name} cites steps absent from supplied original fragments: {hidden}")
        return selected

    source_id_expansions: dict[str, Any] = {}
    trigger = ids("trigger_step_ids")
    response = ids("response_step_ids")
    outcome = ids("outcome_step_ids")
    trigger_positions = [positions[item] for item in trigger]
    response_positions = [positions[item] for item in response]
    outcome_positions = [positions[item] for item in outcome]
    if any(position <= first_user_index for position in trigger_positions):
        raise ValueError("event trigger cannot be the initial task request")
    if not any(roles[item] in {"toolResult", "user"} for item in trigger):
        raise ValueError("event trigger must include a local observation or later user clarification")
    if any(roles[item] != "assistant" for item in response) or min(response_positions) <= max(trigger_positions):
        raise ValueError("event response must be an assistant action after the local trigger")
    if any(roles[item] != "toolResult" for item in outcome):
        raise ValueError("event outcome must be a later tool observation")
    step_by_id = {str(entry["source_entry_id"]): entry for entry in entries}
    for outcome_id in outcome:
        outcome_step = step_by_id[outcome_id]
        result = outcome_step.get("tool_result")
        result_call_id = result.get("tool_call_id") if isinstance(result, dict) else None
        matching_action = False
        for response_id in response:
            if positions[response_id] >= positions[outcome_id]:
                continue
            action = step_by_id[response_id].get("assistant")
            calls = action.get("tool_calls", []) if isinstance(action, dict) else []
            if isinstance(result_call_id, str):
                matching_action = any(
                    isinstance(call, dict) and call.get("tool_call_id") == result_call_id
                    for call in calls
                )
            elif not calls:
                # Older normalized fixtures have roles and order only.
                matching_action = True
            if matching_action:
                break
        if not matching_action:
            raise ValueError("event outcome must follow a matching response tool call")
    rule_evidence = sidecar.get("rule_evidence")
    rules = base["skill"]["rules"]
    if not isinstance(rule_evidence, list) or len(rule_evidence) != len(rules):
        raise ValueError("event evidence requires one rule_evidence record per rule")
    normalized_rules = []
    for expected_index, item in enumerate(rule_evidence):
        if not isinstance(item, dict) or item.get("rule_index") != expected_index:
            raise ValueError("rule_evidence indices must cover rules in order")
        step_ids = item.get("step_ids")
        if not isinstance(step_ids, list) or not step_ids or not all(isinstance(step_id, str) for step_id in step_ids):
            raise ValueError("each rule_evidence record needs nonempty step_ids")
        controls = sorted(set(step_ids) & compaction_controls)
        if controls:
            raise ValueError(f"rule evidence cites a native compaction summary/control, not an observed raw source step: {controls}")
        step_ids, expansions = resolve_source_ids(
            step_ids, list(positions), field=f"rule evidence[{expected_index}].step_ids"
        )
        if expansions:
            source_id_expansions.setdefault("rule_evidence", {})[str(expected_index)] = expansions
        missing_raw = sorted(set(step_ids) - raw_evidence_ids)
        if missing_raw:
            raise ValueError(f"rule evidence cites a step without preserved raw source content: {missing_raw}")
        if visible_step_ids is not None:
            hidden = sorted(set(step_ids) - visible_step_ids)
            if hidden:
                raise ValueError(f"rule evidence cites steps absent from supplied original fragments: {hidden}")
        normalized_rules.append({"rule_index": expected_index, "step_ids": step_ids})
    source = trace.get("source")
    raw_source_id = source.get("canonical_instance_id", source.get("instance_id")) if isinstance(source, dict) else None
    if isinstance(raw_source_id, str) and raw_source_id:
        source_id = canonical_instance_id(raw_source_id)
        materialized_examples = _materialize_skill_code_examples(
            value["skill"],
            {source_id: trace},
            visible_step_ids_by_source={source_id: sorted(visible_step_ids)} if visible_step_ids is not None else None,
        )
        if materialized_examples:
            base["skill"]["code_examples"] = materialized_examples
    elif value["skill"].get("code_examples"):
        raise ValueError("event code examples require source trajectory identity")
    normalized_evidence: dict[str, Any] = {
        "trigger_step_ids": trigger,
        "response_step_ids": response,
        "outcome_step_ids": outcome,
        "rule_evidence": normalized_rules,
    }
    if source_id_expansions:
        normalized_evidence["source_id_expansions"] = source_id_expansions
    checked = {**base, "evidence": normalized_evidence}
    if evidence_normalization is not None:
        checked["evidence_normalization"] = evidence_normalization
    return checked


def maintenance_from_skills_messages(candidate: dict[str, Any], retrieved_skills: list[dict[str, Any]], *, paper_prompt: str) -> list[dict[str, str]]:
    # Figure 9 input intentionally contains only the candidate and similar bank
    # entries. Evidence provenance stays in the sidecar, not the solver skill.
    # Convert the bank's compact internal granularity labels before composing
    # the paper-facing request; validate_maintenance converts any merged paper
    # skill back to the internal schema after the call.
    return [
        {"role": "system", "content": paper_prompt},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "candidate_skill": _maintenance_visible_skill(candidate, candidate_id=True),
                    "retrieved_skills": [_maintenance_visible_skill(skill) for skill in retrieved_skills],
                },
                ensure_ascii=False,
            ),
        },
    ]


def _maintenance_visible_skill(skill: dict[str, Any], *, candidate_id: bool = False) -> dict[str, Any]:
    """Send only the skill content and exact IDs available for a Fig.9 decision."""
    paper = internal_skill_to_paper({key: skill[key] for key in (
        "title", "granularity", "when_to_apply", "rules") if key in skill})
    paper["skill_id"] = str(skill.get("skill_id") or "candidate") if candidate_id else str(skill["skill_id"])
    if "code_examples" in skill:
        paper["code_examples"] = [{
            "example_id": code_example_id(item), "language": item["language"],
            "purpose": item["purpose"], "generated_code": item["generated_example"]["code"],
            "applicability": list(dict.fromkeys(
                adaptation["applicability"] for adaptation in item["generated_example"]["adaptations"]
                if adaptation.get("applicability"))),
            "prerequisites": deepcopy(item["generated_example"]["prerequisites"]),
            "known_limitations": deepcopy(item["generated_example"]["known_limitations"]),
            "unknowns": deepcopy(item["generated_example"]["unknowns"]),
        } for item in validate_materialized_code_examples(skill["code_examples"])]
    return paper


def validate_maintenance_from_skills(
    value: dict[str, Any],
    *,
    candidate: dict[str, Any],
    retrieved_skill_ids: set[str],
    retrieved_skills: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate Fig.9's model decision before it mutates the common bank."""
    if not isinstance(value, dict) or value.get("action") not in {"add", "merge", "drop"}:
        raise ValueError("maintenance must return add, merge, or drop")
    if not isinstance(value.get("reason"), str) or not value["reason"].strip():
        raise ValueError("maintenance needs a nonempty reason")
    action = value["action"]
    candidate_id = str(candidate.get("skill_id") or "candidate")
    supplied_ids = {candidate_id, *retrieved_skill_ids}
    evidence = value.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != {"source_skill_ids", "source_example_ids"}:
        raise ValueError("maintenance needs exact visible skill/example references")
    skill_ids = evidence["source_skill_ids"]
    example_ids = evidence["source_example_ids"]
    if (not isinstance(skill_ids, list) or not skill_ids
            or not all(isinstance(item, str) and item in supplied_ids for item in skill_ids)
            or len(skill_ids) != len(set(skill_ids))
            or candidate_id not in skill_ids):
        raise ValueError("maintenance source_skill_ids must cite supplied skills including candidate")
    visible_examples = [*validate_materialized_code_examples(candidate.get("code_examples"))]
    for skill in retrieved_skills or []:
        visible_examples.extend(validate_materialized_code_examples(skill.get("code_examples")))
    known_example_ids = {code_example_id(item) for item in visible_examples}
    if (not isinstance(example_ids, list)
            or not all(isinstance(item, str) and item in known_example_ids for item in example_ids)
            or len(example_ids) != len(set(example_ids))):
        raise ValueError("maintenance source_example_ids must cite supplied examples")
    result: dict[str, Any] = {"action": action, "reason": value["reason"],
                              "evidence": {"source_skill_ids": skill_ids,
                                           "source_example_ids": example_ids}}
    if action != "merge":
        if "code_example_changes" in value:
            raise ValueError("maintenance add/drop cannot rewrite code examples")
        return result
    target = value.get("merge_target_skill_id")
    if not isinstance(target, str) or target not in retrieved_skill_ids:
        raise ValueError("maintenance merge target must be one retrieved skill")
    if target not in skill_ids:
        raise ValueError("maintenance merge must cite the selected target skill")
    raw_merged_skill = value.get("skill")
    if isinstance(raw_merged_skill, dict) and "code_examples" in raw_merged_skill:
        raise ValueError("maintenance must use code_example_changes instead of embedding stored evidence in skill")
    merged = validate_extraction(
        {"action": "generate", "skill": raw_merged_skill},
        benchmark=str(candidate["benchmark"]),
        expected_granularity=str(candidate["granularity"]),
    )
    target_skill = None
    if retrieved_skills is not None:
        matches = [skill for skill in retrieved_skills if isinstance(skill, dict) and skill.get("skill_id") == target]
        if len(matches) != 1:
            raise ValueError("maintenance retrieved_skills must contain the selected merge target exactly once")
        target_skill = matches[0]
    elif candidate.get("code_examples"):
        raise ValueError("maintenance merge needs retrieved skill payloads to update code_examples")
    source_examples = [
        *validate_materialized_code_examples(candidate.get("code_examples")),
        *validate_materialized_code_examples(target_skill.get("code_examples") if isinstance(target_skill, dict) else None),
    ]

    def materialize_change(raw: dict[str, Any], source: dict[str, Any], *, reason: str,
                           phase: str) -> dict[str, Any]:
        return materialize_maintenance_code_example(
            raw, source_example=source, reason=reason,
            revision_context={
                "phase": phase,
                "merge_target_skill_id": target,
                "merge_target_skill_version": target_skill.get("version") if isinstance(target_skill, dict) else None,
            },
        )

    by_example_id = {code_example_id(item): item for item in source_examples}
    changes = value.get("code_example_changes")
    if isinstance(changes, list):
        for change in changes:
            if not isinstance(change, dict):
                continue
            if change.get("action") in {"retain", "remove", "revise", "add"}:
                source_id = change.get("source_example_id") if change["action"] == "add" else change.get("example_id")
                if not isinstance(source_id, str) or source_id not in by_example_id:
                    raise ValueError("maintenance example change cites an unknown source example ID")
                if source_id not in example_ids:
                    raise ValueError("maintenance example change must cite its visible source example")
            if change.get("action") in {"revise", "add"}:
                if not isinstance(change.get("reason"), str) or not change["reason"].strip():
                    raise ValueError("maintenance example change needs a reason")

    def materialize_added_from_change(raw: dict[str, Any]) -> dict[str, Any]:
        source_id = raw.get("source_example_id")
        source = by_example_id.get(source_id)
        if source is None:
            raise CodeExampleError("maintenance-added code example needs a known source_example_id")
        return materialize_change(raw["example"], source, reason=raw["reason"], phase="maintenance_add")

    def materialize_revised(raw: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        reason = next(change["reason"] for change in changes if change.get("action") == "revise"
                      and change.get("example_id") == code_example_id(source))
        return materialize_change(raw, source, reason=reason, phase="maintenance_merge")

    try:
        updated_examples = apply_code_example_changes(
            changes,
            candidate.get("code_examples"),
            target_skill.get("code_examples") if isinstance(target_skill, dict) else None,
            materialize_added_change=materialize_added_from_change,
            materialize_revised=materialize_revised,
            revision_context={
                "phase": "maintenance_merge",
                "merge_target_skill_id": target,
                "merge_target_skill_version": target_skill.get("version") if isinstance(target_skill, dict) else None,
            },
        )
    except CodeExampleError as error:
        raise ValueError(f"maintenance code-example update is invalid: {error}") from error
    if updated_examples:
        merged["skill"]["code_examples"] = updated_examples
    return {**result, "merge_target_skill_id": target, "skill": merged["skill"]}


def maintenance_messages(candidate: dict[str, Any], retrieved_skills: list[dict[str, Any]], *, paper_prompt: str) -> list[dict[str, str]]:
    """Historical Fig.9 request used by earlier R009–R012 workflows."""
    return [
        {"role": "system", "content": paper_prompt},
        {"role": "user", "content": json.dumps({
            "candidate_skill": internal_skill_to_paper(candidate),
            "retrieved_skills": [internal_skill_to_paper(skill) for skill in retrieved_skills],
        }, ensure_ascii=False)},
    ]


def validate_maintenance(
    value: dict[str, Any], *, candidate: dict[str, Any], retrieved_skill_ids: set[str],
    retrieved_skills: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Historical source-bound Fig.9 contract; active C-only uses a separate adapter."""
    if not isinstance(value, dict) or value.get("action") not in {"add", "merge", "drop"}:
        raise ValueError("maintenance must return add, merge, or drop")
    if not isinstance(value.get("reason"), str) or not value["reason"].strip():
        raise ValueError("maintenance needs a nonempty reason")
    action = value["action"]
    result: dict[str, Any] = {"action": action, "reason": value["reason"]}
    if action != "merge":
        if "code_example_changes" in value:
            raise ValueError("maintenance add/drop cannot rewrite code examples")
        return result
    target = value.get("merge_target_skill_id")
    if not isinstance(target, str) or target not in retrieved_skill_ids:
        raise ValueError("maintenance merge target must be one retrieved skill")
    raw_merged_skill = value.get("skill")
    if isinstance(raw_merged_skill, dict) and "code_examples" in raw_merged_skill:
        raise ValueError("maintenance must use code_example_changes instead of embedding stored evidence in skill")
    merged = validate_extraction(
        {"action": "generate", "skill": raw_merged_skill},
        benchmark=str(candidate["benchmark"]),
        expected_granularity=str(candidate["granularity"]),
    )
    target_skill = None
    if retrieved_skills is not None:
        matches = [skill for skill in retrieved_skills if isinstance(skill, dict) and skill.get("skill_id") == target]
        if len(matches) != 1:
            raise ValueError("maintenance retrieved_skills must contain the selected merge target exactly once")
        target_skill = matches[0]
    elif candidate.get("code_examples"):
        raise ValueError("maintenance merge needs retrieved skill payloads to update code_examples")
    source_examples = [
        *validate_materialized_code_examples(candidate.get("code_examples")),
        *validate_materialized_code_examples(target_skill.get("code_examples") if isinstance(target_skill, dict) else None),
    ]

    def materialize_added(raw: dict[str, Any]) -> dict[str, Any]:
        source = raw.get("source")
        if not isinstance(source, dict):
            raise CodeExampleError("maintenance-added code example needs a source reference")
        matches = [item for item in source_examples if item["source_binding"] == source]
        if not matches:
            raise CodeExampleError("maintenance-added code example must cite an existing immutable source")
        evidence_hashes = {
            (item["source_operation"]["sha256"], item["source_observation"]["sha256"])
            for item in matches
        }
        if len(evidence_hashes) != 1:
            raise CodeExampleError("maintenance source binding has conflicting immutable evidence")
        manager_fields = {key: deepcopy(item) for key, item in raw.items() if key != "source"}
        return materialize_code_example_from_stored_source(
            manager_fields, source_example=matches[0],
            revision_context={
                "phase": "maintenance_add", "merge_target_skill_id": target,
                "merge_target_skill_version": target_skill.get("version") if isinstance(target_skill, dict) else None,
            },
        )

    try:
        updated_examples = apply_code_example_changes(
            value.get("code_example_changes"), candidate.get("code_examples"),
            target_skill.get("code_examples") if isinstance(target_skill, dict) else None,
            materialize_added=materialize_added,
            revision_context={
                "phase": "maintenance_merge", "merge_target_skill_id": target,
                "merge_target_skill_version": target_skill.get("version") if isinstance(target_skill, dict) else None,
            },
        )
    except CodeExampleError as error:
        raise ValueError(f"maintenance code-example update is invalid: {error}") from error
    if updated_examples:
        merged["skill"]["code_examples"] = updated_examples
    return {**result, "merge_target_skill_id": target, "skill": merged["skill"]}


def compacted_event_extraction_messages(
    trace: dict[str, Any],
    *,
    evidence_summaries: list[dict[str, Any]],
    original_fragments: list[dict[str, Any]],
    paper_prompt: str,
    prior_event_ids: list[str],
    prior_event_candidates: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """V02 input: step-cited summaries plus the cited original fragments."""
    if not trace.get("text_manager_eligible", False):
        raise ValueError("Event extraction cannot send multimodal-pending source evidence to a text manager")
    evidence = {
        "task_context": trace["instruction"],
        "source": trace["source"],
        "evidence_compacted": True,
        "evidence_summaries": evidence_summaries,
        "original_cited_fragments": original_fragments,
        "outcome": trace["outcome"],
        "previous_event_candidate_ids": prior_event_ids,
        "previous_event_candidates": _compact_prior_event_candidates(prior_event_candidates or []),
    }
    return [{"role": "system", "content": paper_prompt}, {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)}]


def validate_evidence_summary(value: dict[str, Any], trace: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("summary"), str) or not value["summary"].strip():
        raise ValueError("evidence summary must have nonempty summary text")
    step_ids = value.get("evidence_step_ids")
    if not isinstance(step_ids, list) or not step_ids or not all(isinstance(item, str) for item in step_ids):
        raise ValueError("evidence summary requires nonempty evidence_step_ids")
    resolved, expansions = resolve_source_ids(step_ids, source_step_ids(trace), field="evidence summary evidence_step_ids")
    result = {"summary": value["summary"], "evidence_step_ids": resolved}
    if expansions:
        result["source_step_id_expansions"] = expansions
    return result


def validate_budget_summary(value: dict[str, Any], *, segment_step_ids: list[str]) -> dict[str, Any]:
    """R006 separates exhaustive segment coverage from the verbatim subset."""
    if not isinstance(value, dict) or not isinstance(value.get("summary"), str) or not value["summary"].strip():
        raise ValueError("budget summary must contain nonempty summary text")
    covered = value.get("covered_step_ids")
    verbatim = value.get("verbatim_evidence_step_ids")
    if not isinstance(covered, list) or not all(isinstance(item, str) for item in covered):
        raise ValueError("budget summary covered_step_ids must be a string list")
    covered, covered_expansions = resolve_source_ids(
        covered, segment_step_ids, field="budget summary covered_step_ids"
    )
    if set(covered) != set(segment_step_ids):
        raise ValueError("budget summary must account for every segment step exactly once")
    if not isinstance(verbatim, list) or not verbatim or not all(isinstance(item, str) for item in verbatim):
        raise ValueError("budget summary requires nonempty verbatim_evidence_step_ids")
    verbatim, verbatim_expansions = resolve_source_ids(
        verbatim, segment_step_ids, field="budget summary verbatim_evidence_step_ids"
    )
    result = {"summary": value["summary"], "covered_step_ids": covered, "verbatim_evidence_step_ids": verbatim}
    expansions = {key: value for key, value in {
        "covered_step_ids": covered_expansions,
        "verbatim_evidence_step_ids": verbatim_expansions,
    }.items() if value}
    if expansions:
        result["source_id_expansions"] = expansions
    return result


def validate_r006_budget_summary(
    value: dict[str, Any],
    *,
    segment_steps: list[dict[str, Any]],
    final_segment: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate R006 coverage and its bounded, original-evidence subset."""
    from .compaction import complete_tool_pair_count, expand_evidence_fragments

    step_ids = [str(step["source_entry_id"]) for step in segment_steps]
    summary = validate_budget_summary(value, segment_step_ids=step_ids)
    fragments = expand_evidence_fragments(segment_steps, summary["verbatim_evidence_step_ids"])
    if complete_tool_pair_count(fragments) > 3:
        raise ValueError("R006 permits at most three complete action-observation pairs per segment")
    if final_segment:
        final_observation = next(
            (str(step["source_entry_id"]) for step in reversed(segment_steps) if step.get("role") == "toolResult"),
            None,
        )
        supplied = {str(step["source_entry_id"]) for step in fragments}
        if final_observation is None or final_observation not in supplied:
            raise ValueError("R006 final segment must retain its original final observed result")
    return summary, fragments
