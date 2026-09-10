"""Paper-aligned manager message builders and strict output adapters."""

from __future__ import annotations

import json
from typing import Any

from .traces import source_step_ids
from .types import canonical_instance_id


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


def description_messages(trace: dict[str, Any], *, custom_prompt: str) -> list[dict[str, str]]:
    evidence = {
        "task_context": trace["instruction"],
        "task_name": trace["source"]["task_name"],
        "official_outcome": trace["outcome"],
        "steps": trace["steps"],
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
    allowed = set(source_step_ids(trace))
    unknown = sorted(set(step_ids) - allowed)
    if unknown:
        raise ValueError(f"description cites unknown source steps: {unknown}")
    return {key: value[key] for key in required}


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
    selected = value.get("selected_instance_ids")
    if not isinstance(selected, list) or len(selected) not in {2, 3} or not all(isinstance(item, str) for item in selected):
        raise ValueError("selected_instance_ids must name 2–3 instances")
    if len(set(selected)) != len(selected) or anchor_id not in selected:
        raise ValueError("selected group must be unique and include its anchor")
    if not set(selected) <= candidate_ids | {anchor_id}:
        raise ValueError("selected group contains an instance outside MiniLM candidates")
    if not isinstance(value.get("reason"), str) or not value["reason"].strip():
        raise ValueError("selected group needs a reason")
    result = {"action": "select", "selected_instance_ids": selected, "reason": value["reason"]}
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
            if not isinstance(instance_id, str) or instance_id not in selected:
                raise ValueError("R011 instance_evidence must cite only selected instances")
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
            "steps": trace["steps"],
            "outcome": trace["outcome"],
        }
        for trace in traces
    ]
    return [{"role": "system", "content": paper_prompt}, {"role": "user", "content": json.dumps({"trajectories": evidence}, ensure_ascii=False)}]


def event_extraction_messages(trace: dict[str, Any], *, paper_prompt: str, prior_event_ids: list[str]) -> list[dict[str, str]]:
    if not trace.get("text_manager_eligible", False):
        raise ValueError("Event extraction cannot send multimodal-pending source evidence to a text manager")
    evidence = {
        "task_context": trace["instruction"],
        "source": trace["source"],
        "full_trajectory": trace["steps"],
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
        "full_trajectory": trace["steps"],
        "outcome": trace["outcome"],
        "previous_event_candidate_ids": prior_event_ids,
        # R012's second and third attempts must be able to see compact
        # candidate content and local step references, not opaque IDs alone.
        # The complete source trajectory remains present above.
        "previous_event_candidates": prior_event_candidates or [],
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
        unknown = sorted(set(selected) - set(positions))
        if unknown:
            if set(unknown) & compaction_controls:
                raise ValueError(f"event evidence {name} cites a native compaction summary/control, not an observed raw source step: {unknown}")
            raise ValueError(f"event evidence {name} cites unknown source steps: {unknown}")
        missing_raw = sorted(set(selected) - raw_evidence_ids)
        if missing_raw:
            raise ValueError(f"event evidence {name} cites a step without preserved raw source content: {missing_raw}")
        if visible_step_ids is not None:
            hidden = sorted(set(selected) - visible_step_ids)
            if hidden:
                raise ValueError(f"event evidence {name} cites steps absent from supplied original fragments: {hidden}")
        return selected

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
    if not any(roles[item] == "assistant" for item in response) or min(response_positions) <= max(trigger_positions):
        raise ValueError("event response must be an assistant action after the local trigger")
    if not any(roles[item] == "toolResult" for item in outcome) or min(outcome_positions) <= max(response_positions):
        raise ValueError("event outcome must be a later tool observation")
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
        unknown = sorted(set(step_ids) - set(positions))
        if unknown:
            if set(unknown) & compaction_controls:
                raise ValueError(f"rule evidence cites a native compaction summary/control, not an observed raw source step: {unknown}")
            raise ValueError(f"rule evidence cites unknown source steps: {unknown}")
        missing_raw = sorted(set(step_ids) - raw_evidence_ids)
        if missing_raw:
            raise ValueError(f"rule evidence cites a step without preserved raw source content: {missing_raw}")
        if visible_step_ids is not None:
            hidden = sorted(set(step_ids) - visible_step_ids)
            if hidden:
                raise ValueError(f"rule evidence cites steps absent from supplied original fragments: {hidden}")
        normalized_rules.append({"rule_index": expected_index, "step_ids": step_ids})
    return {**base, "evidence": {"trigger_step_ids": trigger, "response_step_ids": response, "outcome_step_ids": outcome, "rule_evidence": normalized_rules}}


def maintenance_messages(candidate: dict[str, Any], retrieved_skills: list[dict[str, Any]], *, paper_prompt: str) -> list[dict[str, str]]:
    # Figure 9 input intentionally contains only the candidate and similar bank
    # entries. Evidence provenance stays in the sidecar, not the solver skill.
    return [
        {"role": "system", "content": paper_prompt},
        {"role": "user", "content": json.dumps({"candidate_skill": candidate, "retrieved_skills": retrieved_skills}, ensure_ascii=False)},
    ]


def validate_maintenance(
    value: dict[str, Any],
    *,
    candidate: dict[str, Any],
    retrieved_skill_ids: set[str],
) -> dict[str, Any]:
    """Validate Fig.9's model decision before it mutates the common bank."""
    if not isinstance(value, dict) or value.get("action") not in {"add", "merge", "drop"}:
        raise ValueError("maintenance must return add, merge, or drop")
    if not isinstance(value.get("reason"), str) or not value["reason"].strip():
        raise ValueError("maintenance needs a nonempty reason")
    action = value["action"]
    result: dict[str, Any] = {"action": action, "reason": value["reason"]}
    if action != "merge":
        return result
    target = value.get("merge_target_skill_id")
    if not isinstance(target, str) or target not in retrieved_skill_ids:
        raise ValueError("maintenance merge target must be one retrieved skill")
    merged = validate_extraction(
        {"action": "generate", "skill": value.get("skill")},
        benchmark=str(candidate["benchmark"]),
        expected_granularity=str(candidate["granularity"]),
    )
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
        "previous_event_candidates": prior_event_candidates or [],
    }
    return [{"role": "system", "content": paper_prompt}, {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)}]


def validate_evidence_summary(value: dict[str, Any], trace: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("summary"), str) or not value["summary"].strip():
        raise ValueError("evidence summary must have nonempty summary text")
    step_ids = value.get("evidence_step_ids")
    if not isinstance(step_ids, list) or not step_ids or not all(isinstance(item, str) for item in step_ids):
        raise ValueError("evidence summary requires nonempty evidence_step_ids")
    unknown = sorted(set(step_ids) - set(source_step_ids(trace)))
    if unknown:
        raise ValueError(f"evidence summary cites unknown source steps: {unknown}")
    return {"summary": value["summary"], "evidence_step_ids": step_ids}


def validate_budget_summary(value: dict[str, Any], *, segment_step_ids: list[str]) -> dict[str, Any]:
    """R006 separates exhaustive segment coverage from the verbatim subset."""
    if not isinstance(value, dict) or not isinstance(value.get("summary"), str) or not value["summary"].strip():
        raise ValueError("budget summary must contain nonempty summary text")
    covered = value.get("covered_step_ids")
    verbatim = value.get("verbatim_evidence_step_ids")
    if not isinstance(covered, list) or not all(isinstance(item, str) for item in covered):
        raise ValueError("budget summary covered_step_ids must be a string list")
    if set(covered) != set(segment_step_ids) or len(covered) != len(set(covered)):
        raise ValueError("budget summary must account for every segment step exactly once")
    if not isinstance(verbatim, list) or not verbatim or not all(isinstance(item, str) for item in verbatim):
        raise ValueError("budget summary requires nonempty verbatim_evidence_step_ids")
    if len(verbatim) != len(set(verbatim)) or not set(verbatim) <= set(segment_step_ids):
        raise ValueError("verbatim evidence must be unique and come from this segment")
    return {"summary": value["summary"], "covered_step_ids": covered, "verbatim_evidence_step_ids": verbatim}


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
