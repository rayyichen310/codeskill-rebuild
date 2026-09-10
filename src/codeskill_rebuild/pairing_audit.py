"""Evidence packet for human review of an R012 no-related-group decision."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .types import canonical_instance_id, sha256_text, write_json


class PairingAuditError(ValueError):
    pass


def _packet(*, trace: dict[str, Any], description: dict[str, Any], trace_ref: dict[str, Any] | None, description_ref: dict[str, Any] | None) -> dict[str, Any]:
    source = trace.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("canonical_instance_id"), str):
        raise PairingAuditError("trace needs source.canonical_instance_id")
    cited = description.get("source_step_ids")
    if not isinstance(cited, list) or not all(isinstance(item, str) for item in cited):
        raise PairingAuditError("description needs source_step_ids for raw-description audit")
    steps = trace.get("steps")
    if not isinstance(steps, list):
        raise PairingAuditError("trace needs a step list")
    by_id = {str(step.get("source_entry_id")): step for step in steps if isinstance(step, dict) and step.get("source_entry_id")}
    missing = sorted(set(cited) - set(by_id))
    if missing:
        raise PairingAuditError(f"description cites steps absent from raw trace: {missing}")
    return {
        "canonical_instance_id": canonical_instance_id(source["canonical_instance_id"]),
        "description": deepcopy(description),
        "description_ref": deepcopy(description_ref),
        "raw_trace_ref": deepcopy(trace_ref),
        "description_cited_steps": [deepcopy(by_id[step_id]) for step_id in cited],
        "all_raw_step_ids": list(by_id),
        "uncited_raw_step_ids": [step_id for step_id in by_id if step_id not in set(cited)],
        "raw_trace_step_id_sha256": sha256_text("\n".join(by_id)),
    }


def no_related_group_audit(
    *,
    anchor_trace: dict[str, Any],
    anchor_description: dict[str, Any],
    candidates: list[dict[str, Any]],
    pairing_result: dict[str, Any],
    anchor_trace_ref: dict[str, Any] | None = None,
    anchor_description_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture evidence for review without deciding whether a group exists."""
    if pairing_result.get("action") != "no_related_group" or not isinstance(pairing_result.get("reason"), str):
        raise PairingAuditError("audit only accepts a no_related_group pairing result with its reason")
    values: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("trace"), dict) or not isinstance(candidate.get("description"), dict):
            raise PairingAuditError("each candidate needs trace and description")
        values.append(
            _packet(
                trace=candidate["trace"],
                description=candidate["description"],
                trace_ref=candidate.get("trace_ref"),
                description_ref=candidate.get("description_ref"),
            )
        )
    anchor = _packet(
        trace=anchor_trace,
        description=anchor_description,
        trace_ref=anchor_trace_ref,
        description_ref=anchor_description_ref,
    )
    return {
        "schema_version": 1,
        "kind": "r012_no_related_group_raw_description_audit",
        "pairing_result": deepcopy(pairing_result),
        "anchor": anchor,
        "candidates": values,
        "automated_classification": None,
        "review_required": "compare descriptions with cited and uncited raw steps; do not retune pairing or force a group from this artifact",
    }


def write_no_related_group_audit(path: Path, audit: dict[str, Any]) -> None:
    if audit.get("kind") != "r012_no_related_group_raw_description_audit":
        raise PairingAuditError("refusing to write an unrelated pairing audit artifact")
    write_json(Path(path), audit)
