"""Historical three-call Event manager orchestration and reconciliation."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from codeskill_rebuild.pipeline import (
    event_extraction_with_evidence_messages, validate_event_extraction_with_evidence,
)

def _event_extraction(
    *,
    context: dict[str, Any],
    input_value: dict[str, Any],
    trace: dict[str, Any],
    executor: Any,
    hooks: dict[str, Any],
    reconciled_manager_calls: dict[str, dict[str, Any]] | None = None,
    reconciliation_path: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Run up to three ordered Fig.7 calls and retain every raw outcome."""
    _prompt_path = hooks["_prompt_path"]
    _manager_call = hooks["_manager_call"]
    _finish_manager_journal = hooks["_finish_manager_journal"]
    _add_provenance = hooks["_add_provenance"]
    _manager_derivation_binding = hooks["_manager_derivation_binding"]
    _historical_thinking_policy_identity = hooks["_historical_thinking_policy_identity"]
    _hash_json = hooks["_hash_json"]
    _object = hooks["_object"]
    _text = hooks["_text"]
    attempts: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {"kind": "r015_c_only_event_extraction", "manager_calls": [], "validation_errors": []}
    if trace.get("text_manager_eligible") is not True:
        attempts.append({"attempt_no": 1, "outcome": "skip", "candidate": None, "raw_response": {"reason": "trajectory contains image evidence and text manager is ineligible"}})
        evidence["decision"] = "skip"
        evidence["reason"] = "multimodal trajectory is not text-manager eligible"
        return attempts, candidates, evidence
    prior_ids: list[str] = []
    prior_summaries: list[dict[str, Any]] = []
    for attempt_no in range(1, 4):
        reconciled_call = reconciled_manager_calls.get(f"event-{attempt_no:03d}") if reconciled_manager_calls else None
        # Reproduce the audited bytes only for the exact historical call.
        # Every later, newly paid call uses the current custom contract.
        prompt_path = (
            "custom/r012_fig07_event_extraction_evidence.md"
            if reconciled_call is not None
            else "custom/r015_fig07_event_extraction_with_code_examples.md"
        )
        prompt = _prompt_path(prompt_path).read_text(encoding="utf-8")
        messages = event_extraction_with_evidence_messages(
            trace,
            runtime_prompt=prompt,
            prior_event_ids=prior_ids,
            prior_event_candidates=prior_summaries,
        )
        manager_call_kwargs: dict[str, Any] = {
            "trial_id": str(context["trial_id"]),
            "phase": f"event-{attempt_no:03d}",
            "purpose": f"r015_c_only_event_extraction:{context['trial_id']}:{attempt_no:03d}",
            "messages": messages,
            "metadata": {"attempt_no": attempt_no, "condition": "C-only", "task_id": context["task_id"]},
            "trajectory_context": context,
            "source_traces": [trace],
            "messages_builder": lambda values, prompt=prompt: event_extraction_with_evidence_messages(
                values[0], runtime_prompt=prompt, prior_event_ids=prior_ids, prior_event_candidates=prior_summaries,
            ),
        }
        if reconciled_call is not None:
            manager_call_kwargs.update(
                {
                    "reconciled_call": reconciled_call,
                    "reconciliation_path": reconciliation_path,
                    "reconciled_context": context,
                }
            )
        call, journal, invalid = _manager_call(executor, **manager_call_kwargs)
        response_ref = journal.get("response")
        if isinstance(response_ref, dict):
            evidence["manager_calls"].append(deepcopy(response_ref))
        if call is None:
            # ManagerClient has already durably classified this as a model
            # output failure; it is still an initial attempt and can be
            # followed by the next bounded call.
            journal = _finish_manager_journal(
                executor,
                journal,
                status="event_output_rejected",
                value={"error": invalid},
            )
            attempts.append({"attempt_no": attempt_no, "outcome": "invalid", "candidate": None, "raw_response": {"manager": invalid, "response": response_ref}})
            evidence["validation_errors"].append(deepcopy(invalid))
            continue
        model_output = call.get("json")
        try:
            checked = validate_event_extraction_with_evidence(
                model_output, trace, benchmark="terminal-bench",
                visible_step_ids=set(call["visible_step_ids_by_source"][str(context["task_id"])])
                if "visible_step_ids_by_source" in call else None,
            )
        except (ValueError, TypeError) as error:
            attempts.append({"attempt_no": attempt_no, "outcome": "invalid", "candidate": None, "raw_response": {"manager_call_id": call.get("call_id"), "response": response_ref, "model_output": deepcopy(model_output), "error_type": type(error).__name__, "error": str(error)}})
            evidence["validation_errors"].append({"call_id": call.get("call_id"), "error_type": type(error).__name__, "error": str(error), "response": response_ref})
            _finish_manager_journal(executor, journal, status="event_output_rejected", value={"call_id": call.get("call_id"), "validation_error": str(error)})
            continue
        if checked.get("action") == "skip":
            attempts.append({"attempt_no": attempt_no, "outcome": "skip", "candidate": None, "raw_response": {"manager_call_id": call.get("call_id"), "response": response_ref, "model_output": deepcopy(model_output)}})
            evidence["decision"] = "skip"
            evidence["reason"] = checked.get("reason")
            _finish_manager_journal(executor, journal, status="event_skip", value={"call_id": call.get("call_id"), "validated": checked})
            break
        skill = _add_provenance(_object(checked.get("skill"), field="event manager skill"), [str(context["task_id"])])
        fingerprint = _hash_json(skill)
        if fingerprint in prior_ids:
            # The protocol expects a duplicate candidate itself, and compares
            # its canonical fingerprint to the earlier generated candidate.
            attempts.append({"attempt_no": attempt_no, "outcome": "duplicate", "candidate": skill, "raw_response": {"manager_call_id": call.get("call_id"), "response": response_ref, "model_output": deepcopy(model_output)}})
            evidence["decision"] = "duplicate"
            _finish_manager_journal(executor, journal, status="event_duplicate", value={"call_id": call.get("call_id"), "validated": checked, "candidate_fingerprint": fingerprint})
            break
        candidate_id = f"event-{fingerprint[:16]}"
        journal = _finish_manager_journal(
            executor,
            journal,
            status="event_generated",
            value={
                "call_id": call.get("call_id"),
                "validated": checked,
                "candidate_fingerprint": fingerprint,
            },
        )
        derivation = _manager_derivation_binding(
            context=context,
            executor=executor,
            call_id=_text(call.get("call_id"), field="event manager call_id"),
            journal_ref=journal,
        )
        wrapper = {
            "candidate_id": candidate_id,
            "skill": skill,
            "raw": {
                "manager_call_id": call.get("call_id"),
                "response": response_ref,
                "model_output": deepcopy(model_output),
                "historical_thinking_policy": _historical_thinking_policy_identity(context),
                **({"derivation": derivation} if derivation is not None else {}),
            },
        }
        candidates.append(wrapper)
        attempts.append({"attempt_no": attempt_no, "outcome": "generated", "candidate": skill, "raw_response": {"manager_call_id": call.get("call_id"), "response": response_ref, "model_output": deepcopy(model_output)}})
        prior_ids.append(fingerprint)
        prior_summaries.append({"candidate_id": candidate_id, "skill": skill})
    evidence.setdefault("decision", "extract" if candidates else "skip")
    if not candidates and "reason" not in evidence:
        evidence["reason"] = "no valid event candidate survived the bounded initial attempts"
    return attempts, candidates, evidence











