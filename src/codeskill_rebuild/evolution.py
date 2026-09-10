"""Evidence-gated R012 evolution inputs.

Retrieval is not injection.  This module derives evolution candidates only
from durable proxy request records which show a complete skill block was sent
upstream.  Retired event priors remain eligible through that request evidence.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from .runtime import render_skill
from .types import sha256_text


class EvolutionEvidenceError(ValueError):
    pass


UPSTREAM_SUPPLIED_OUTCOMES = {"upstream_opened", "stream_forwarded", "stream_copy_error"}


def _key(skill: dict[str, Any]) -> tuple[str, int]:
    skill_id = skill.get("skill_id")
    version = skill.get("version")
    if not isinstance(skill_id, str) or not skill_id or not isinstance(version, int):
        raise EvolutionEvidenceError("supplied skill needs a nonempty skill_id and integer version")
    return skill_id, version


def _block_for(phase: str, skill: dict[str, Any]) -> str:
    if phase == "task":
        return "[CODESKILL TASK PRIOR KNOWLEDGE]\n" + render_skill(skill)
    if phase == "event":
        return "[CODESKILL EVENT PRIOR KNOWLEDGE]\n" + render_skill(skill)
    raise EvolutionEvidenceError(f"unknown injection phase {phase!r}")


def supplied_skills_for_evolution(
    attempt_records: Iterable[dict[str, Any]],
    *,
    trial_id: str,
) -> list[dict[str, Any]]:
    """Return every actually supplied unique skill, never merely retrieved ones."""
    selected: dict[tuple[str, int], dict[str, Any]] = {}
    known: dict[tuple[str, int], dict[str, Any]] = {}
    ordered_records = sorted(
        (record for record in attempt_records if isinstance(record, dict)),
        key=lambda record: (int(record.get("attempt_ordinal", -1)), int(record.get("forwarded_request_ordinal", -1))),
    )
    for record in ordered_records:
        if not isinstance(record, dict) or record.get("trial_id") != trial_id:
            continue
        # A successful prepare persists selection state before the transport
        # opens.  Remember that identity even if this particular connection
        # later fails; a retry may re-overlay the same prior without making a
        # new selector call.
        if isinstance(record.get("forwarded_request"), dict) and "uncommitted_selection" not in record:
            phase_records = [("task", record.get("task_selection"))]
            event_selections = record.get("event_selection", [])
            if not isinstance(event_selections, list):
                raise EvolutionEvidenceError("forwarded attempt has invalid event_selection evidence")
            phase_records.extend(("event", item) for item in event_selections)
            for phase, phase_record in phase_records:
                if not isinstance(phase_record, dict):
                    continue
                injected = phase_record.get("injected_skills")
                if not isinstance(injected, list):
                    continue
                for item in injected:
                    if not isinstance(item, dict) or not isinstance(item.get("skill"), dict):
                        raise EvolutionEvidenceError("injection evidence lacks a skill object")
                    skill = item["skill"]
                    skill_id, version = _key(skill)
                    if phase == "task":
                        block = phase_record.get("block")
                        if not isinstance(block, str) or phase_record.get("block_sha256") != sha256_text(block):
                            raise EvolutionEvidenceError("task selection lacks a verified combined task block")
                        if item.get("rendered_skill_sha256") != sha256_text(render_skill(skill)):
                            raise EvolutionEvidenceError("task selection skill hash does not match the supplied skill")
                    else:
                        block = _block_for("event", skill)
                        if item.get("block_sha256") != sha256_text(block):
                            raise EvolutionEvidenceError("event injection block hash does not match the supplied skill")
                    known[(skill_id, version)] = {
                        "skill": deepcopy(skill),
                        "phase": phase,
                        "block": block,
                        "anchor_id": item.get("anchor_id"),
                    }
        if record.get("proxy_outcome") not in UPSTREAM_SUPPLIED_OUTCOMES:
            continue
        forwarded = record.get("forwarded_request")
        messages = forwarded.get("messages") if isinstance(forwarded, dict) else None
        if not isinstance(messages, list):
            raise EvolutionEvidenceError("an upstream-supplied record lacks forwarded messages")
        for key, prior in known.items():
            skill = prior["skill"]
            block = prior["block"]
            if not any(block in str(message.get("content")) for message in messages if isinstance(message, dict)):
                continue
            evidence = {
                    "trial_id": trial_id,
                    "phase": prior["phase"],
                    "attempt_ordinal": record.get("attempt_ordinal"),
                    "forwarded_request_ordinal": record.get("forwarded_request_ordinal"),
                    "proxy_outcome": record.get("proxy_outcome"),
                    "block_sha256": sha256_text(block),
                    "anchor_id": prior["anchor_id"],
                }
            if key not in selected:
                selected[key] = {"skill": deepcopy(skill), "injection_evidence": [evidence]}
            else:
                selected[key]["injection_evidence"].append(evidence)
    return [selected[key] for key in sorted(selected)]


def select_evolution_candidate(
    *,
    supplied: Iterable[dict[str, Any]],
    selected_skill_id: str | None,
    selected_version: int | None,
    new_trace_evidence: dict[str, Any],
) -> dict[str, Any]:
    """Build a maintenance-ready evolution record while rejecting unused skills."""
    values = list(supplied)
    allowed = {
        _key(item["skill"]): item
        for item in values
        if isinstance(item, dict) and isinstance(item.get("skill"), dict)
    }
    if selected_skill_id is None and selected_version is None:
        return {"action": "skip", "reason": "no supplied skill selected for evolution", "supplied_candidates": deepcopy(values)}
    if not isinstance(selected_skill_id, str) or not selected_skill_id or not isinstance(selected_version, int):
        raise EvolutionEvidenceError("an evolution selection needs both skill_id and version, or neither")
    selected = allowed.get((selected_skill_id, selected_version))
    if selected is None:
        raise EvolutionEvidenceError("evolution may select only a skill actually supplied to this trial")
    if not isinstance(new_trace_evidence, dict) or not new_trace_evidence:
        raise EvolutionEvidenceError("evolution needs new trajectory evidence")
    return {
        "action": "evolve",
        "base_skill": deepcopy(selected["skill"]),
        "injection_evidence": deepcopy(selected["injection_evidence"]),
        "new_trace_evidence": deepcopy(new_trace_evidence),
        "maintenance_required": True,
    }
