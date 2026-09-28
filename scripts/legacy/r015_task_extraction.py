"""Historical R015 Task extraction used by the bounded thinking A/B diagnostic.

The active C-only driver uses TaskGraphStages.  This module keeps the old
manager-only comparison executable without reintroducing Task control there.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from codeskill_rebuild.bank import BankError, validate_skill_candidate
from codeskill_rebuild.pipeline import (
    description_messages, pairing_messages, task_candidate_extraction_messages,
    task_candidate_merge_messages, validate_description, validate_pairing,
    validate_task_candidate_with_evidence, validate_task_extraction_with_evidence,
)
from codeskill_rebuild.r012_execution import R012EvolutionMaintenanceExecutor
from codeskill_rebuild.retrieval import cosine
from codeskill_rebuild.types import canonical_instance_id, canonical_json, sha256_file
from scripts.run_r015_c_only_harbor_driver import (
    COnlyHarborDriverError, _add_provenance, _ensure_encoder,
    _finish_manager_journal, _hash_json, _historical_thinking_policy_identity,
    _journal_evidence_ref, _manager_call, _manager_derivation_binding,
    _model_output_from_response_ref, _object, _prompt_path, _read_json_file,
    _text, _validate_manager_derivation_binding,
)



def _validate_description_record(
    value: dict[str, Any],
    *,
    field: str,
    expected_round: int,
    expected_task_id: str,
    expected_trajectory_ref: dict[str, Any],
    expected_trace: dict[str, Any],
    expected_historical_thinking_policy: dict[str, str],
) -> dict[str, Any]:
    """Validate one immutable D01 record before it can enter D02.

    The description pool is a derived same-round store.  Checking only a
    task label or a 64-character string would allow a caller to relabel a
    response from another session; verify the response bytes, D01 value,
    trajectory binding, and validator result together.
    """
    item = _object(value, field=field)
    if item.get("round_id") != expected_round or canonical_instance_id(_text(item.get("task_id"), field=f"{field}.task_id")) != expected_task_id:
        raise COnlyHarborDriverError(f"{field} is outside the active C-only round/task")
    path = Path(_text(item.get("path"), field=f"{field}.path"))
    stated_hash = _text(item.get("sha256"), field=f"{field}.sha256")
    if not path.is_file() or sha256_file(path) != stated_hash:
        raise COnlyHarborDriverError(f"{field} path/hash is not an immutable registered manager response")
    response_sha = _text(item.get("response_sha256", stated_hash), field=f"{field}.response_sha256")
    if response_sha != stated_hash:
        raise COnlyHarborDriverError(f"{field}.response_sha256 differs from its response file")
    description = _object(item.get("value"), field=f"{field}.value")
    value_hash = _text(item.get("value_sha256"), field=f"{field}.value_sha256")
    if value_hash != _hash_json(description):
        raise COnlyHarborDriverError(f"{field}.value_sha256 does not match its D01 value")
    trajectory_ref = _object(item.get("trajectory_ref"), field=f"{field}.trajectory_ref")
    if canonical_json(trajectory_ref) != canonical_json(expected_trajectory_ref):
        raise COnlyHarborDriverError(f"{field}.trajectory_ref differs from the registered trajectory")
    manager_call_id = item.get("manager_call_id")
    if not isinstance(manager_call_id, str) or not manager_call_id:
        manager_call_id = "legacy-keep"
    response_ref = {
        "call_id": manager_call_id,
        "path": str(path),
        "sha256": stated_hash,
    }
    derivation = item.get("derivation")
    _validate_manager_derivation_binding(
        derivation,
        field=f"{field}.derivation",
        wrapper_policy=item.get("historical_thinking_policy"),
        expected_policy=expected_historical_thinking_policy,
        expected_traces=[expected_trace],
        response_ref=response_ref,
        expected_call_id=manager_call_id,
        allowed_journal_statuses={"description_validated"},
    )
    if derivation is not None:
        if item.get("manager_response_path") != str(path) or item.get("manager_response_sha256") != stated_hash:
            raise COnlyHarborDriverError(f"{field} manager response aliases differ from its response file")
        persisted_output = _model_output_from_response_ref(
            response_ref,
            field=f"{field}.manager_response",
            expected_call_id=manager_call_id,
        )
        try:
            persisted_description = validate_description(persisted_output, expected_trace)
        except (ValueError, TypeError) as error:
            raise COnlyHarborDriverError(f"{field} saved manager output no longer validates: {error}") from error
        if canonical_json(persisted_description) != canonical_json(description):
            raise COnlyHarborDriverError(f"{field}.value differs from its saved manager response")
    try:
        checked = validate_description(description, expected_trace)
    except (ValueError, TypeError) as error:
        raise COnlyHarborDriverError(f"{field}.value no longer passes the D01 validator: {error}") from error
    if canonical_json(checked) != canonical_json(description):
        raise COnlyHarborDriverError(f"{field}.value is not the canonical D01 validator result")
    normalized = deepcopy(item)
    normalized["task_id"] = expected_task_id
    normalized["sha256"] = stated_hash
    normalized["response_sha256"] = response_sha
    normalized["value_sha256"] = value_hash
    normalized["trajectory_ref"] = deepcopy(trajectory_ref)
    normalized["value"] = checked
    return normalized


def _registered_description_sources(
    *,
    context: dict[str, Any],
    input_value: dict[str, Any],
    traces: list[tuple[str, dict[str, Any], dict[str, Any]]],
    current_descriptions: list[dict[str, Any]],
) -> tuple[dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]], dict[str, Any]]:
    """Return exact text-eligible descriptions keyed by task identity.

    Multimodal trajectories stay in the campaign and in the trajectory pool,
    but they are explicit D02 exclusions.  A later text-only task can still
    rank earlier eligible descriptions; one unusable source must not suppress
    the complete rest of the pool.
    """
    round_id = int(str(context["trial_id"]).split(":", 1)[0].removeprefix("r"))
    trace_by_task = {task_id: (trace, ref) for task_id, trace, ref in traces}
    current_id = str(context["task_id"])
    current_trace, current_ref = trace_by_task.get(current_id, (None, None))
    if not isinstance(current_trace, dict) or not isinstance(current_ref, dict):
        raise COnlyHarborDriverError("D02 has no current trajectory binding")
    evidence: dict[str, Any] = {
        "kind": "r015_c_only_d02_source_eligibility",
        "excluded": [],
        "eligible_task_ids": [],
    }

    def exclude(task_id: str, reason: str, trajectory_ref: dict[str, Any] | None = None) -> None:
        """Record one source exclusion without making it an eligibility veto."""
        value: dict[str, Any] = {"task_id": task_id, "reason": reason}
        if isinstance(trajectory_ref, dict):
            value["trajectory_ref"] = deepcopy(trajectory_ref)
        if not any(
            item.get("task_id") == task_id and item.get("reason") == reason
            for item in evidence["excluded"]
            if isinstance(item, dict)
        ):
            evidence["excluded"].append(value)

    sources: dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = {}
    if current_trace.get("text_manager_eligible") is not True:
        evidence["current_status"] = "skipped_multimodal"
        evidence["excluded"].append({"task_id": current_id, "reason": "current trajectory is not text-manager eligible"})
        return sources, evidence
    if len(current_descriptions) != 1:
        evidence["current_status"] = "missing_or_ambiguous_description"
        evidence["excluded"].append({"task_id": current_id, "reason": "current text-eligible trajectory has no unique D01 record"})
        return sources, evidence
    current_description = _validate_description_record(
        current_descriptions[0],
        field="current_description",
        expected_round=round_id,
        expected_task_id=current_id,
        expected_trajectory_ref=current_ref,
        expected_trace=current_trace,
        expected_historical_thinking_policy=_historical_thinking_policy_identity(context),
    )
    sources[current_id] = (current_description, current_trace, current_ref)

    # Eligibility is per source, never a global property of the prior pool.
    # In particular, code-from-image remains a valid benchmark trial but is
    # excluded from text-manager D01/D02 inputs; later text-only tasks must
    # still be eligible.  Such a source may have no D01 record at all, so
    # record the exclusion from the trajectory pool before walking the D01
    # material below.
    for task_id, trace, trajectory_ref in traces:
        if task_id == current_id:
            continue
        if trace.get("text_manager_eligible") is not True:
            exclude(task_id, "trajectory contains unsupported image evidence", trajectory_ref)

    material = _object(input_value.get("round_material"), field="input.round_material")
    raw_pool = material.get("description_pool", [])
    if not isinstance(raw_pool, list):
        raise COnlyHarborDriverError("round material description_pool must be a list")
    seen: set[str] = set()
    for index, raw in enumerate(raw_pool):
        item = _object(raw, field=f"round_material.description_pool[{index}]")
        task_id = canonical_instance_id(_text(item.get("task_id"), field=f"description_pool[{index}].task_id"))
        if task_id in seen or task_id == current_id:
            raise COnlyHarborDriverError("round material has duplicate or current-task description material")
        seen.add(task_id)
        trace_entry = trace_by_task.get(task_id)
        if trace_entry is None:
            raise COnlyHarborDriverError(f"description pool task {task_id} has no matching trajectory pool entry")
        trace, trajectory_ref = trace_entry
        if trace.get("text_manager_eligible") is not True:
            exclude(task_id, "trajectory contains unsupported image evidence", trajectory_ref)
            continue
        description = _validate_description_record(
            item,
            field=f"round_material.description_pool[{index}]",
            expected_round=round_id,
            expected_task_id=task_id,
            expected_trajectory_ref=trajectory_ref,
            expected_trace=trace,
            expected_historical_thinking_policy=_historical_thinking_policy_identity(context),
        )
        sources[task_id] = (description, trace, trajectory_ref)
    # Do not silently discard an otherwise eligible trajectory merely because
    # its D01 phase was not registered.  D02 must skip that source, retain an
    # auditable reason, and continue ranking any other legal source.
    for task_id, trace, trajectory_ref in traces:
        if task_id == current_id or trace.get("text_manager_eligible") is not True:
            continue
        if task_id not in sources:
            exclude(task_id, "eligible trajectory has no registered D01 description", trajectory_ref)
    evidence["eligible_task_ids"] = [task_id for task_id, _trace, _ref_value in traces if task_id in sources]
    evidence["current_status"] = "eligible"
    return sources, evidence


def _description_extraction(
    *,
    context: dict[str, Any],
    trace: dict[str, Any],
    current_ref: dict[str, Any],
    executor: R012EvolutionMaintenanceExecutor,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if trace.get("text_manager_eligible") is not True:
        return [], {"status": "skipped_multimodal", "reason": "trajectory is not text-manager eligible"}
    messages = description_messages(trace, custom_prompt=_prompt_path("custom/m2_description.md").read_text(encoding="utf-8"))
    call, journal, invalid = _manager_call(
        executor,
        trial_id=str(context["trial_id"]),
        phase="description",
        purpose=f"r015_c_only_description:{context['trial_id']}",
        messages=messages,
        trajectory_context=context, source_traces=[trace],
        messages_builder=lambda values: description_messages(values[0], custom_prompt=_prompt_path("custom/m2_description.md").read_text(encoding="utf-8")),
        metadata={"condition": "C-only", "task_id": context["task_id"]},
    )
    response_ref = journal.get("response")
    if call is None:
        journal = _finish_manager_journal(
            executor,
            journal,
            status="description_output_rejected",
            value={"error": invalid},
        )
        return [], {"status": "description_extraction_failed", "error": invalid, "response": response_ref, "journal": _journal_evidence_ref(journal)}
    try:
        checked = validate_description(call.get("json"), trace)
    except (ValueError, TypeError) as error:
        journal = _finish_manager_journal(executor, journal, status="description_output_rejected", value={"call_id": call.get("call_id"), "error_type": type(error).__name__, "error": str(error)})
        return [], {"status": "description_extraction_failed", "call_id": call.get("call_id"), "response": response_ref, "journal": _journal_evidence_ref(journal), "error_type": type(error).__name__, "error": str(error), "model_output": deepcopy(call.get("json"))}
    journal = _finish_manager_journal(executor, journal, status="description_validated", value={"call_id": call.get("call_id"), "validated": checked})
    derivation = _manager_derivation_binding(
        context=context,
        executor=executor,
        call_id=_text(call.get("call_id"), field="description manager call_id"),
        journal_ref=journal,
    )
    return [
        {
            "task_id": context["task_id"],
            "round_id": int(str(context["trial_id"]).split(":", 1)[0].removeprefix("r")),
            "sha256": response_ref["sha256"],
            "path": response_ref["path"],
            "response_sha256": response_ref["sha256"],
            # Keep the D01 record bound to the exact ManagerClient call.  The
            # generic path/hash pair proves byte integrity, while these
            # explicit aliases let the production outer runner reject a
            # response relabelled from another call or run.
            "manager_call_id": call.get("call_id"),
            "manager_response_sha256": response_ref["sha256"],
            "manager_response_path": response_ref["path"],
            "value": checked,
            "value_sha256": _hash_json(checked),
            "trajectory_ref": deepcopy(current_ref),
            "historical_thinking_policy": _historical_thinking_policy_identity(context),
            **({"derivation": derivation} if derivation is not None else {}),
        }
    ], {"status": "validated", "call_id": call.get("call_id"), "response": response_ref, "journal": _journal_evidence_ref(journal), "value": checked}


def _validate_task_candidate_record(
    value: dict[str, Any],
    *,
    field: str,
    expected_round: int,
    expected_task_id: str,
    expected_trace: dict[str, Any],
    expected_trajectory_ref: dict[str, Any],
    expected_historical_thinking_policy: dict[str, str],
) -> dict[str, Any]:
    """Revalidate one isolated SOP candidate against its exact raw trace."""
    item = _object(value, field=field)
    if item.get("status") != "validated" or item.get("source") != "current_round_c_only":
        raise COnlyHarborDriverError(f"{field} is not a validated current-round task candidate")
    if item.get("round_id") != expected_round or canonical_instance_id(_text(item.get("task_id"), field=f"{field}.task_id")) != expected_task_id:
        raise COnlyHarborDriverError(f"{field} is outside the expected round/task")
    if canonical_json(_object(item.get("trajectory_ref"), field=f"{field}.trajectory_ref")) != canonical_json(expected_trajectory_ref):
        raise COnlyHarborDriverError(f"{field}.trajectory_ref differs from the registered trajectory")
    raw = _object(item.get("raw"), field=f"{field}.raw")
    response = _object(raw.get("response"), field=f"{field}.raw.response")
    manager_call_id = _text(raw.get("manager_call_id"), field=f"{field}.raw.manager_call_id")
    model_output = _object(raw.get("model_output"), field=f"{field}.raw.model_output")
    _validate_manager_derivation_binding(
        raw.get("derivation"),
        field=f"{field}.raw.derivation",
        wrapper_policy=raw.get("historical_thinking_policy"),
        expected_policy=expected_historical_thinking_policy,
        expected_traces=[expected_trace],
        response_ref=response,
        expected_call_id=manager_call_id,
        allowed_journal_statuses={"task_candidate_generated"},
    )
    persisted_model_output = _model_output_from_response_ref(
        response,
        field=f"{field}.raw.response",
        expected_call_id=manager_call_id,
    )
    if canonical_json(model_output) != canonical_json(persisted_model_output):
        raise COnlyHarborDriverError(f"{field}.raw.model_output differs from its saved manager response")
    try:
        checked = validate_task_candidate_with_evidence(
            model_output,
            expected_trace,
            benchmark="terminal-bench",
            visible_step_ids=raw.get("visible_step_ids"),
        )
    except (ValueError, TypeError) as error:
        raise COnlyHarborDriverError(f"{field} no longer passes task-candidate validation: {error}") from error
    if checked.get("action") != "generate":
        raise COnlyHarborDriverError(f"{field} does not contain a generated SOP candidate")
    expected_skill = _add_provenance(_object(checked.get("skill"), field=f"{field}.validated_skill"), [expected_task_id])
    skill = validate_skill_candidate(_object(item.get("skill"), field=f"{field}.skill"))
    if canonical_json(skill) != canonical_json(expected_skill):
        raise COnlyHarborDriverError(f"{field}.skill differs from its revalidated manager output")
    fingerprint = _text(item.get("candidate_fingerprint"), field=f"{field}.candidate_fingerprint")
    if fingerprint != _hash_json(skill):
        raise COnlyHarborDriverError(f"{field}.candidate_fingerprint differs from its skill")
    if canonical_json(item.get("candidate_context")) != canonical_json(checked.get("candidate_context")):
        raise COnlyHarborDriverError(f"{field}.candidate_context differs from its revalidated manager output")
    if canonical_json(item.get("evidence")) != canonical_json(checked.get("evidence")):
        raise COnlyHarborDriverError(f"{field}.evidence differs from its revalidated manager output")
    if canonical_json(item.get("evidence_normalization")) != canonical_json(checked.get("evidence_normalization")):
        raise COnlyHarborDriverError(f"{field}.evidence_normalization differs from its revalidated manager output")
    if canonical_json(item.get("official_task_outcome")) != canonical_json(expected_trace.get("outcome")):
        raise COnlyHarborDriverError(f"{field}.official_task_outcome differs from its source trajectory")
    return deepcopy(item)


def _single_task_candidate_extraction(
    *,
    context: dict[str, Any],
    trace: dict[str, Any],
    current_ref: dict[str, Any],
    executor: R012EvolutionMaintenanceExecutor,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extract one isolated SOP candidate before any cross-task pairing."""
    if trace.get("text_manager_eligible") is not True:
        return [], {"status": "skipped_multimodal", "reason": "trajectory is not text-manager eligible"}
    prompt = _prompt_path("custom/r015_task_sop_candidate.md").read_text(encoding="utf-8")
    messages = task_candidate_extraction_messages(trace, prompt=prompt)
    call, journal, invalid = _manager_call(
        executor,
        trial_id=str(context["trial_id"]),
        phase="task-sop-candidate",
        purpose=f"r015_c_only_task_sop_candidate:{context['trial_id']}",
        messages=messages,
        trajectory_context=context,
        source_traces=[trace],
        messages_builder=lambda values: task_candidate_extraction_messages(values[0], prompt=prompt),
        metadata={"condition": "C-only", "task_id": context["task_id"], "candidate_pool": "isolated"},
    )
    response_ref = journal.get("response")
    if call is None:
        journal = _finish_manager_journal(executor, journal, status="task_candidate_output_rejected", value={"error": invalid})
        return [], {"status": "task_candidate_extraction_failed", "error": invalid, "response": response_ref, "journal": _journal_evidence_ref(journal)}
    source_id = str(context["task_id"])
    visible_by_source = call.get("visible_step_ids_by_source")
    visible_ids = visible_by_source.get(source_id) if isinstance(visible_by_source, dict) else None
    try:
        checked = validate_task_candidate_with_evidence(
            call.get("json"),
            trace,
            benchmark="terminal-bench",
            visible_step_ids=visible_ids,
        )
    except (ValueError, TypeError) as error:
        journal = _finish_manager_journal(executor, journal, status="task_candidate_output_rejected", value={"call_id": call.get("call_id"), "error_type": type(error).__name__, "error": str(error)})
        return [], {"status": "task_candidate_extraction_failed", "call_id": call.get("call_id"), "response": response_ref, "journal": _journal_evidence_ref(journal), "model_output": deepcopy(call.get("json")), "error_type": type(error).__name__, "error": str(error)}
    if checked.get("action") == "skip":
        journal = _finish_manager_journal(executor, journal, status="task_candidate_skip", value={"call_id": call.get("call_id"), "validated": checked})
        return [], {"status": "skip", "call_id": call.get("call_id"), "response": response_ref, "journal": _journal_evidence_ref(journal), "value": checked}
    try:
        skill = _add_provenance(_object(checked.get("skill"), field="task candidate skill"), [source_id])
    except (BankError, COnlyHarborDriverError, TypeError, ValueError) as error:
        journal = _finish_manager_journal(
            executor,
            journal,
            status="task_candidate_output_rejected",
            value={
                "call_id": call.get("call_id"),
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )
        return [], {
            "status": "task_candidate_extraction_failed",
            "call_id": call.get("call_id"),
            "response": response_ref,
            "journal": _journal_evidence_ref(journal),
            "model_output": deepcopy(call.get("json")),
            "error_type": type(error).__name__,
            "error": str(error),
        }
    fingerprint = _hash_json(skill)
    journal = _finish_manager_journal(executor, journal, status="task_candidate_generated", value={"call_id": call.get("call_id"), "validated": checked, "candidate_fingerprint": fingerprint})
    derivation = _manager_derivation_binding(
        context=context,
        executor=executor,
        call_id=_text(call.get("call_id"), field="task candidate manager call_id"),
        journal_ref=journal,
    )
    record = {
        "candidate_id": f"task-sop-{fingerprint[:16]}",
        "candidate_fingerprint": fingerprint,
        "status": "validated",
        "source": "current_round_c_only",
        "round_id": current_ref["round_id"],
        "task_id": source_id,
        "trial_id": current_ref["trial_id"],
        "session_id": current_ref["session_id"],
        "trajectory_ref": deepcopy(current_ref),
        "skill": skill,
        "candidate_context": deepcopy(checked["candidate_context"]),
        "evidence": deepcopy(checked["evidence"]),
        **({"evidence_normalization": deepcopy(checked["evidence_normalization"])} if "evidence_normalization" in checked else {}),
        "official_task_outcome": deepcopy(trace.get("outcome")),
        "raw": {
            "manager_call_id": call.get("call_id"),
            "response": response_ref,
            "model_output": deepcopy(call.get("json")),
            "visible_step_ids": deepcopy(visible_ids),
            "historical_thinking_policy": _historical_thinking_policy_identity(context),
            **({"derivation": derivation} if derivation is not None else {}),
        },
    }
    return [record], {"status": "generated", "call_id": call.get("call_id"), "response": response_ref, "journal": _journal_evidence_ref(journal), "candidate_id": record["candidate_id"], "candidate_fingerprint": fingerprint}


def _registered_task_candidate_sources(
    *,
    context: dict[str, Any],
    input_value: dict[str, Any],
    traces: list[tuple[str, dict[str, Any], dict[str, Any]]],
    current_records: list[dict[str, Any]],
) -> dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    """Load the current SOP candidate and only earlier same-round candidates."""
    round_id = int(str(context["trial_id"]).split(":", 1)[0].removeprefix("r"))
    current_id = str(context["task_id"])
    trace_by_task = {task_id: (trace, ref) for task_id, trace, ref in traces}
    if len(current_records) != 1:
        return {}
    current_trace, current_ref = trace_by_task[current_id]
    result = {
        current_id: (
            _validate_task_candidate_record(
                current_records[0],
                field="current_task_candidate",
                expected_round=round_id,
                expected_task_id=current_id,
                expected_trace=current_trace,
                expected_trajectory_ref=current_ref,
                expected_historical_thinking_policy=_historical_thinking_policy_identity(context),
            ),
            current_trace,
            current_ref,
        )
    }
    material = _object(input_value.get("round_material"), field="input.round_material")
    raw_pool = material.get("task_candidate_pool", [])
    if not isinstance(raw_pool, list):
        raise COnlyHarborDriverError("round material task_candidate_pool must be a list")
    state_ref = _object(input_value.get("state"), field="input.state")
    state_path = Path(_text(state_ref.get("path"), field="input.state.path"))
    state_value = _read_json_file(state_path, field="current C-only state")
    state_rounds = _object(state_value.get("rounds"), field="current C-only state.rounds")
    state_round = _object(state_rounds.get(str(round_id)), field="current C-only state.round")
    completed_tasks = _object(state_round.get("completed_tasks"), field="current C-only state.completed_tasks")
    state_pool = state_round.get("task_candidate_pool", [])
    if not isinstance(state_pool, list):
        raise COnlyHarborDriverError("current C-only state task_candidate_pool must be a list")
    prior_state_pool = [
        item
        for item in state_pool
        if not isinstance(item, dict) or item.get("task_id") != current_id
    ]
    if canonical_json(raw_pool) != canonical_json(prior_state_pool):
        raise COnlyHarborDriverError(
            "round material task_candidate_pool differs from the durable prior-task pool"
        )
    seen: set[str] = set()
    for index, raw in enumerate(raw_pool):
        item = _object(raw, field=f"round_material.task_candidate_pool[{index}]")
        task_id = canonical_instance_id(_text(item.get("task_id"), field=f"task_candidate_pool[{index}].task_id"))
        if task_id == current_id or task_id in seen:
            raise COnlyHarborDriverError("task candidate pool contains the current task or a duplicate task")
        completed = completed_tasks.get(task_id)
        if not isinstance(completed, dict) or completed.get("outcome") != "completed" or not isinstance(completed.get("trajectory"), dict):
            raise COnlyHarborDriverError(
                f"task candidate pool task {task_id} is not an earlier completed current-round task"
            )
        seen.add(task_id)
        trace_entry = trace_by_task.get(task_id)
        if trace_entry is None:
            raise COnlyHarborDriverError(f"task candidate pool task {task_id} has no matching trajectory")
        trace, ref_value = trace_entry
        result[task_id] = (
            _validate_task_candidate_record(
                item,
                field=f"round_material.task_candidate_pool[{index}]",
                expected_round=round_id,
                expected_task_id=task_id,
                expected_trace=trace,
                expected_trajectory_ref=ref_value,
                expected_historical_thinking_policy=_historical_thinking_policy_identity(context),
            ),
            trace,
            ref_value,
        )
    return result


def _task_extraction(
    *,
    context: dict[str, Any],
    input_value: dict[str, Any],
    traces: list[tuple[str, dict[str, Any], dict[str, Any]]],
    task_candidate_records: list[dict[str, Any]] | None = None,
    executor: R012EvolutionMaintenanceExecutor,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # D02 ranks isolated same-round SOP candidates, then lets the manager
    # select a related 2--3-candidate group.  Only the current local record
    # and earlier durable pool records can participate.
    task_candidate_records = list(task_candidate_records or [])
    current_traces = [item for item in traces if item[0] == context["task_id"]]
    if len(current_traces) != 1:
        return [], {"status": "invalid_source", "reason": "D02 requires exactly one current C-only trajectory"}
    sources = _registered_task_candidate_sources(
        context=context,
        input_value=input_value,
        traces=traces,
        current_records=task_candidate_records,
    )
    current_id = str(context["task_id"])
    current_source = sources.get(current_id)
    if current_source is None:
        return [], {
            "status": "no_current_task_candidate",
            "reason": "D02 has no validated isolated SOP candidate for the current trace",
            "ranking": {
                "kind": "r015_c_only_d02_minilm_task_candidate_ranking",
                "max_candidates": 12,
                "ranked_candidates": [],
            },
        }
    eligible_prior = [
        (task_id, value)
        for task_id, value in sources.items()
        if task_id != current_id
    ]
    if not eligible_prior:
        return [], {
            "status": "no_eligible_sources",
            "reason": "fewer than one distinct earlier validated current-round SOP candidate",
            "ranking": {
                "kind": "r015_c_only_d02_minilm_task_candidate_ranking",
                "max_candidates": 12,
                "ranked_candidates": [],
            },
        }
    encoder = _ensure_encoder(context, executor)
    anchor_candidate, anchor_trace, anchor_ref = current_source
    anchor_vector, anchor_index = encoder.index_skill(anchor_candidate["skill"])
    ranked: list[dict[str, Any]] = []
    for task_id, (candidate_record, trace, trajectory_ref) in eligible_prior:
        vector, index_record = encoder.index_skill(candidate_record["skill"])
        ranked.append(
            {
                "task_id": task_id,
                "score": cosine(anchor_vector, vector),
                "candidate_id": candidate_record["candidate_id"],
                "candidate_fingerprint": candidate_record["candidate_fingerprint"],
                "skill": deepcopy(candidate_record["skill"]),
                "candidate_context": deepcopy(candidate_record["candidate_context"]),
                "official_task_outcome": deepcopy(candidate_record["official_task_outcome"]),
                "evidence": deepcopy(candidate_record["evidence"]),
                "trajectory_ref": deepcopy(trajectory_ref),
                "index": index_record,
            }
        )
    ranked.sort(key=lambda item: (-float(item["score"]), str(item["task_id"])))
    ranked = ranked[:12]
    ranking = {
        "kind": "r015_c_only_d02_minilm_task_candidate_ranking",
        "encoder": {
            "repo_id": getattr(encoder, "repo_id", None),
            "resolved_revision": getattr(encoder, "resolved_revision", None),
            "max_seq_length": getattr(getattr(encoder, "_model", None), "max_seq_length", None),
        },
        "max_candidates": 12,
        "anchor": {
            "task_id": current_id,
            "candidate_id": anchor_candidate["candidate_id"],
            "candidate_fingerprint": anchor_candidate["candidate_fingerprint"],
            "trajectory_ref": deepcopy(anchor_ref),
            "index": anchor_index,
        },
        "ranked_candidates": ranked,
    }
    if not ranked:
        return [], {"status": "no_eligible_sources", "reason": "no legal same-round D02 candidates survived MiniLM ranking", "ranking": ranking}
    pairing_anchor = {
        "canonical_instance_id": current_id,
        "candidate_id": anchor_candidate["candidate_id"],
        "skill": deepcopy(anchor_candidate["skill"]),
        "candidate_context": deepcopy(anchor_candidate["candidate_context"]),
        "official_task_outcome": deepcopy(anchor_candidate["official_task_outcome"]),
        "evidence": deepcopy(anchor_candidate["evidence"]),
        "trajectory_ref": deepcopy(anchor_ref),
    }
    pairing_candidates = [
        {
            "canonical_instance_id": item["task_id"],
            "candidate_id": item["candidate_id"],
            "skill": deepcopy(item["skill"]),
            "candidate_context": deepcopy(item["candidate_context"]),
            "official_task_outcome": deepcopy(item["official_task_outcome"]),
            "evidence": deepcopy(item["evidence"]),
            "trajectory_ref": deepcopy(item["trajectory_ref"]),
            "similarity": item["score"],
        }
        for item in ranked
    ]
    pairing_call, pairing_journal, pairing_invalid = _manager_call(
        executor,
        trial_id=str(context["trial_id"]),
        phase="task-pairing",
        purpose=f"r015_c_only_d02_pairing:{context['trial_id']}",
        messages=pairing_messages(
            pairing_anchor,
            pairing_candidates,
            custom_prompt=_prompt_path("custom/m2_task_pairing.md").read_text(encoding="utf-8"),
        ),
        metadata={
            "condition": "C-only",
            "task_id": current_id,
            "d02": "minilm_task_candidate_rank_then_deepseek_pairing",
            "ranked_task_ids": [item["task_id"] for item in ranked],
        },
    )
    pairing_response = pairing_journal.get("response")
    pairing_evidence: dict[str, Any] = {
        "status": "manager_call",
        "response": pairing_response,
        "journal": {key: pairing_journal.get(key) for key in ("path", "sha256")},
        "ranking": ranking,
    }
    if pairing_call is None:
        pairing_journal = _finish_manager_journal(executor, pairing_journal, status="pairing_output_rejected", value={"error": pairing_invalid})
        pairing_evidence["journal"] = _journal_evidence_ref(pairing_journal)
        pairing_evidence.update({"status": "pairing_extraction_failed", "error": pairing_invalid})
        pairing_evidence["ranking"] = ranking
        return [], pairing_evidence
    try:
        checked_pairing = validate_pairing(
            pairing_call.get("json"),
            anchor_id=current_id,
            candidate_ids={item["task_id"] for item in ranked},
            require_shared_evidence=True,
        )
    except (ValueError, TypeError) as error:
        pairing_journal = _finish_manager_journal(
            executor,
            pairing_journal,
            status="pairing_output_rejected",
            value={"call_id": pairing_call.get("call_id"), "error_type": type(error).__name__, "error": str(error)},
        )
        pairing_evidence["journal"] = _journal_evidence_ref(pairing_journal)
        pairing_evidence.update({"status": "pairing_extraction_failed", "call_id": pairing_call.get("call_id"), "model_output": deepcopy(pairing_call.get("json")), "error_type": type(error).__name__, "error": str(error)})
        pairing_evidence["ranking"] = ranking
        return [], pairing_evidence
    pairing_evidence.update({"status": checked_pairing["action"], "call_id": pairing_call.get("call_id"), "value": checked_pairing})
    pairing_evidence["ranking"] = ranking
    if checked_pairing["action"] == "no_related_group":
        pairing_journal = _finish_manager_journal(executor, pairing_journal, status="pairing_no_related_group", value={"call_id": pairing_call.get("call_id"), "validated": checked_pairing})
        pairing_evidence["journal"] = _journal_evidence_ref(pairing_journal)
        return [], pairing_evidence
    selected_ids = [str(item) for item in checked_pairing["selected_instance_ids"]]
    group_sources: list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for task_id in selected_ids:
        source = sources.get(task_id)
        if source is None:
            raise COnlyHarborDriverError(f"D02 manager selected a source without a validated SOP candidate: {task_id}")
        group_sources.append((task_id, source[0], source[1], source[2]))
    ordered = [(current_id, anchor_candidate, anchor_trace, anchor_ref)] + [item for item in group_sources if item[0] != current_id]
    group_hash = _hash_json(sorted(candidate_record["candidate_fingerprint"] for _task_id, candidate_record, _trace, _ref_value in ordered))
    pairing_evidence["group_id"] = f"d02-{group_hash[:20]}"
    pairing_evidence["selected_candidate_ids"] = [candidate_record["candidate_id"] for _task_id, candidate_record, _trace, _ref_value in ordered]
    pairing_evidence["selected_candidate_fingerprints"] = [candidate_record["candidate_fingerprint"] for _task_id, candidate_record, _trace, _ref_value in ordered]
    pairing_evidence["selected_trajectory_hashes"] = [ref_value["sha256"] for _task_id, _candidate, _trace, ref_value in ordered]
    # The pairing decision is a completed manager phase even when Fig.6 is
    # still to be called.  Finish it before copying the journal reference into
    # evidence; otherwise a later coordinator hash check sees the mutable
    # prepared journal bytes.
    pairing_journal = _finish_manager_journal(
        executor,
        pairing_journal,
        status="pairing_selected",
        value={
            "call_id": pairing_call.get("call_id"),
            "validated": checked_pairing,
            "group_id": pairing_evidence["group_id"],
            "selected_candidate_ids": pairing_evidence["selected_candidate_ids"],
            "selected_trajectory_hashes": pairing_evidence["selected_trajectory_hashes"],
        },
    )
    pairing_evidence["journal"] = _journal_evidence_ref(pairing_journal)
    # A repeated exact trajectory group is a duplicate initial task-source
    # decision.  Preserve the pairing response but do not pay for Fig.6 a
    # second time.  Completed assignments are the only durable duplicate
    # index; the current task is not yet present there.
    state_path = Path(_text(_object(input_value.get("state"), field="input.state").get("path"), field="input.state.path"))
    if state_path.is_file():
        state_value = _read_json_file(state_path, field="current C-only state")
        round_state = _object(_object(state_value.get("rounds"), field="state.rounds").get(str(current_source[2]["round_id"])), field="state.current_round")
        for prior_assignment in _object(round_state.get("assignments"), field="state.current_round.assignments").values():
            if not isinstance(prior_assignment, dict):
                continue
            prior_extraction = prior_assignment.get("extraction")
            prior_task_evidence = prior_extraction.get("evidence") if isinstance(prior_extraction, dict) else None
            prior_task = prior_task_evidence.get("task") if isinstance(prior_task_evidence, dict) else None
            if isinstance(prior_task, dict) and prior_task.get("group_id") == pairing_evidence["group_id"]:
                pairing_journal = _finish_manager_journal(executor, pairing_journal, status="pairing_duplicate_group", value={"call_id": pairing_call.get("call_id"), "group_id": pairing_evidence["group_id"]})
                pairing_evidence["journal"] = _journal_evidence_ref(pairing_journal)
                pairing_evidence.update({"status": "duplicate", "reason": "same trajectory-hash group was already used in this round"})
                return [], pairing_evidence
    merge_candidates = [
        {
            "canonical_instance_id": task_id,
            "candidate_id": candidate_record["candidate_id"],
            "skill": deepcopy(candidate_record["skill"]),
            "candidate_context": deepcopy(candidate_record["candidate_context"]),
            "official_task_outcome": deepcopy(candidate_record["official_task_outcome"]),
            "evidence": deepcopy(candidate_record["evidence"]),
        }
        for task_id, candidate_record, _trace, _ref_value in ordered
    ]
    messages = task_candidate_merge_messages(
        merge_candidates,
        [trace for _task_id, _candidate, trace, _ref_value in ordered],
        paper_prompt=_prompt_path("custom/r015_fig06_task_extraction_with_code_examples.md").read_text(encoding="utf-8"),
    )
    executor_phase = f"task-extraction-{group_hash[:12]}"
    call, journal, invalid = _manager_call(
        executor,
        trial_id=str(context["trial_id"]),
        phase=executor_phase,
        purpose=f"r015_c_only_task_extraction:{context['trial_id']}",
        messages=messages,
        trajectory_context=context, source_traces=[item[2] for item in ordered],
        messages_builder=lambda values: task_candidate_merge_messages(merge_candidates, values, paper_prompt=_prompt_path("custom/r015_fig06_task_extraction_with_code_examples.md").read_text(encoding="utf-8")),
        metadata={"condition": "C-only", "task_id": context["task_id"], "source_task_ids": [item[0] for item in ordered], "source_candidate_ids": pairing_evidence["selected_candidate_ids"], "d02_group_id": pairing_evidence["group_id"], "pairing_response": pairing_response},
    )
    response_ref = journal.get("response")
    evidence: dict[str, Any] = {
        "status": "manager_call",
        "response": response_ref,
        "pairing": deepcopy(pairing_evidence),
        "ranking": ranking,
        "group_id": pairing_evidence["group_id"],
    }
    if call is None:
        journal = _finish_manager_journal(executor, journal, status="task_extraction_output_rejected", value={"error": invalid})
        evidence.update({"status": "task_extraction_failed", "error": invalid, "journal": _journal_evidence_ref(journal)})
        return [], evidence
    try:
        checked = validate_task_extraction_with_evidence(
            call.get("json"),
            [trace for _task_id, _candidate, trace, _ref_value in ordered],
            benchmark="terminal-bench",
            visible_step_ids_by_source=call.get("visible_step_ids_by_source"),
        )
    except (ValueError, TypeError) as error:
        journal = _finish_manager_journal(executor, journal, status="task_extraction_output_rejected", value={"call_id": call.get("call_id"), "error_type": type(error).__name__, "error": str(error)})
        evidence.update({"status": "task_extraction_failed", "call_id": call.get("call_id"), "journal": _journal_evidence_ref(journal), "model_output": deepcopy(call.get("json")), "error_type": type(error).__name__, "error": str(error)})
        return [], evidence
    if checked.get("action") == "skip":
        journal = _finish_manager_journal(executor, journal, status="task_extraction_skip", value={"call_id": call.get("call_id"), "validated": checked})
        evidence.update({"status": "skip", "call_id": call.get("call_id"), "journal": _journal_evidence_ref(journal), "value": checked})
        return [], evidence
    source_ids = [task_id for task_id, _candidate, _trace, _ref_value in ordered]
    skill = _add_provenance(_object(checked.get("skill"), field="task manager skill"), source_ids)
    fingerprint = _hash_json(skill)
    pairing = {
        "source_task_ids": source_ids,
        "source_candidate_ids": [candidate_record["candidate_id"] for _task_id, candidate_record, _trace, _ref_value in ordered],
        "source_candidate_fingerprints": [candidate_record["candidate_fingerprint"] for _task_id, candidate_record, _trace, _ref_value in ordered],
        "trajectory_refs": [deepcopy(ref_value) for _task_id, _candidate, _trace, ref_value in ordered],
        "group_id": pairing_evidence["group_id"],
        "d02_pairing": deepcopy(checked_pairing),
    }
    journal = _finish_manager_journal(executor, journal, status="task_extraction_generated", value={"call_id": call.get("call_id"), "validated": checked, "candidate_fingerprint": fingerprint, "source_task_ids": source_ids})
    derivation = _manager_derivation_binding(
        context=context,
        executor=executor,
        call_id=_text(call.get("call_id"), field="task extraction manager call_id"),
        journal_ref=journal,
    )
    candidate = {
        "candidate_id": f"task-{fingerprint[:16]}",
        "skill": skill,
        "pairing": pairing,
        "raw": {
            "manager_call_id": call.get("call_id"),
            "response": response_ref,
            "model_output": deepcopy(call.get("json")),
            "historical_thinking_policy": _historical_thinking_policy_identity(context),
            **({"derivation": derivation} if derivation is not None else {}),
        },
    }
    evidence.update({"status": "generated", "call_id": call.get("call_id"), "journal": _journal_evidence_ref(journal), "value": checked, "candidate_fingerprint": fingerprint, "source_task_ids": source_ids})
    return [candidate], evidence
