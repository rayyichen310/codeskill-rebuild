"""Durable, per-trial OpenAI-request overlay for the R008 V05 integration.

OpenClaw's native session remains untouched.  This module receives the native
history immediately before an upstream solver request and rebuilds the same
task/event supplementary user blocks at their original positions on every
request.  It deliberately does not mutate tool results or solver responses.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .runtime import render_skill
from .types import canonical_json, read_json, sha256_text, utc_now, write_json


class OverlayError(RuntimeError):
    pass


class OverlayContextError(OverlayError):
    """Base class for overlay errors which historically shared one name."""


class OverlayInputLimitError(OverlayContextError):
    """The fully rendered upstream payload exceeds its configured token cap."""


class OverlayAnchorEvidenceError(OverlayContextError):
    """A durable prior lost its anchor without valid compaction evidence."""


class OverlayEventSkillBudgetError(OverlayContextError):
    """A selected event block exceeds its frozen explicit token budget."""


TaskSelector = Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any] | list[dict[str, Any]] | None]
EventSelector = Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any] | list[dict[str, Any]] | None]
# The solver template can include tool schemas, tool_choice, response-format
# controls, and other payload fields in addition to messages.  Counting only
# messages would under-report the actual upstream input.
TokenCounter = Callable[[dict[str, Any]], int]


@dataclass(frozen=True)
class EventSelectionSettings:
    """An explicitly supplied development setting for multi-event injection.

    R012 permits more than one matching event skill but deliberately leaves the
    final count, relevance rule, and token budget to a later user-approved
    profile.  The overlay therefore has no hidden threshold, budget, or model
    judge.  A caller which wants to inject multiple skills must name the
    temporary development cap and the profile which authorized it.
    """

    max_matching_skills: int
    profile_ref: str
    skill_token_budget: int | None = None
    skill_token_budget_scope: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.max_matching_skills, int) or isinstance(self.max_matching_skills, bool) or self.max_matching_skills <= 0:
            raise ValueError("max_matching_skills must be a positive integer")
        if not isinstance(self.profile_ref, str) or not self.profile_ref.strip():
            raise ValueError("profile_ref must be a nonempty string")
        if self.skill_token_budget is not None and (
            not isinstance(self.skill_token_budget, int) or isinstance(self.skill_token_budget, bool) or self.skill_token_budget <= 0
        ):
            raise ValueError("skill_token_budget must be a positive integer when configured")
        if self.skill_token_budget is None and self.skill_token_budget_scope is not None:
            raise ValueError("skill_token_budget_scope requires skill_token_budget")
        if self.skill_token_budget is not None and self.skill_token_budget_scope != "complete_payload_active_event_blocks_delta":
            raise ValueError("skill_token_budget_scope must explicitly be complete_payload_active_event_blocks_delta")

    def evidence(self) -> dict[str, Any]:
        return {
            "max_matching_skills": self.max_matching_skills,
            "profile_ref": self.profile_ref,
            "relevance_threshold": None,
            "event_token_budget": self.skill_token_budget,
            "event_token_budget_scope": self.skill_token_budget_scope,
            "llm_relevance_judge": "not_required_by_r012",
        }


def _message_fingerprint(message: dict[str, Any]) -> str:
    return sha256_text(canonical_json(message))


def _skill_key(skill: dict[str, Any]) -> tuple[str, int]:
    skill_id = skill.get("skill_id")
    version = skill.get("version")
    if not isinstance(skill_id, str) or not skill_id or not isinstance(version, int):
        raise OverlayError("selected skill needs nonempty skill_id and integer version")
    return skill_id, version


def _selection(value: dict[str, Any] | list[dict[str, Any]] | None, *, phase: str) -> dict[str, Any]:
    if value is None:
        return {"skills": [], "bank_snapshot": None, "query": None}
    if isinstance(value, list):
        result: dict[str, Any] = {"skills": value, "bank_snapshot": None, "query": None}
    elif isinstance(value, dict):
        if "skills" in value:
            result = {"skills": value.get("skills"), "bank_snapshot": value.get("bank_snapshot"), "query": value.get("query")}
        elif "skill" in value:
            result = {"skills": [] if value["skill"] is None else [value["skill"]], "bank_snapshot": value.get("bank_snapshot"), "query": value.get("query")}
        else:
            raise OverlayError(f"{phase} selector returned neither skill nor skills")
    else:
        raise OverlayError(f"{phase} selector returned unsupported value")
    if not isinstance(result["skills"], list) or not all(isinstance(skill, dict) for skill in result["skills"]):
        raise OverlayError(f"{phase} selector skills must be a list of objects")
    for skill in result["skills"]:
        _skill_key(skill)
    return result


def _append_to_user_message(message: dict[str, Any], block: str) -> dict[str, Any]:
    if message.get("role") != "user":
        raise OverlayError("task overlay can only attach to an original user message")
    copied = deepcopy(message)
    content = copied.get("content")
    if isinstance(content, str):
        copied["content"] = content + "\n\n" + block
    elif isinstance(content, list):
        copied["content"] = [*content, {"type": "text", "text": "\n\n" + block}]
    else:
        raise OverlayError("original task user content must be text or a content block list")
    return copied


def _task_block(skills: list[dict[str, Any]]) -> str:
    return "[CODESKILL TASK PRIOR KNOWLEDGE]\n" + "\n\n".join(render_skill(skill) for skill in skills)


def _event_block(skill: dict[str, Any]) -> str:
    return "[CODESKILL EVENT PRIOR KNOWLEDGE]\n" + render_skill(skill)


def _tool_call_ids(message: dict[str, Any]) -> list[str]:
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return []
    ids = [str(call.get("id")) for call in calls if isinstance(call, dict) and isinstance(call.get("id"), str) and call["id"]]
    if len(ids) != len(set(ids)):
        raise OverlayError("assistant tool-call batch has duplicate IDs")
    return ids


def _complete_batches(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find only fully resolved OpenAI tool-call batches in native history."""
    batches: list[dict[str, Any]] = []
    for assistant_index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        ids = _tool_call_ids(message)
        if not ids:
            continue
        remaining = set(ids)
        result_indices: list[int] = []
        for index in range(assistant_index + 1, len(messages)):
            following = messages[index]
            if following.get("role") == "assistant":
                break
            if following.get("role") == "tool" and isinstance(following.get("tool_call_id"), str):
                call_id = following["tool_call_id"]
                if call_id in remaining:
                    remaining.remove(call_id)
                    result_indices.append(index)
                    if not remaining:
                        anchor_payload = {
                            "assistant_message": message,
                            "tool_call_ids": ids,
                            "tool_result_messages": [messages[item] for item in result_indices],
                        }
                        batches.append(
                            {
                                "anchor_id": sha256_text(canonical_json(anchor_payload)),
                                "assistant_index": assistant_index,
                                "after_native_index": max(result_indices),
                                "tool_call_ids": ids,
                                "tool_result_indices": result_indices,
                                "tool_result_message_fingerprints": [_message_fingerprint(messages[item]) for item in result_indices],
                            }
                        )
                        break
        # An incomplete batch is intentionally absent: no selector call and no
        # supplementary user message may be placed between its tool results.
    return batches


class DurableOverlay:
    """Persistent state machine for one isolated M3 trial.

    ``prepare`` is synchronous: its event selector finishes before it returns
    the upstream request, establishing the tool-batch-to-next-decision
    boundary required by R008.
    """

    def __init__(
        self,
        *,
        trial_id: str,
        state_path: Path,
        evidence_dir: Path,
        token_counter: TokenCounter,
        max_input_tokens: int = 250_000,
        task_selector: TaskSelector | None = None,
        event_selector: EventSelector | None = None,
        event_selection_settings: EventSelectionSettings | None = None,
        enable_task: bool = True,
        enable_event: bool = True,
    ) -> None:
        if not trial_id:
            raise ValueError("trial_id is required")
        if max_input_tokens <= 0:
            raise ValueError("max_input_tokens must be positive")
        self.trial_id = trial_id
        self.state_path = Path(state_path)
        self.evidence_dir = Path(evidence_dir)
        self.token_counter = token_counter
        self.max_input_tokens = max_input_tokens
        self.task_selector = task_selector
        self.event_selector = event_selector
        self.event_selection_settings = event_selection_settings
        self.enable_task = enable_task
        self.enable_event = enable_event
        self.state = self._load_state()

    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema_version": 3,
            "kind": "r012_durable_overlay_state",
            "trial_id": self.trial_id,
            "created_at_utc": utc_now(),
            "request_count": 0,
            "attempt_count": 0,
            "overlay_initialized": False,
            "task": None,
            "events": [],
            # A complete batch is terminal only once: it is either considered
            # for an event at that request boundary, or durably recorded as
            # pre-existing / non-terminal history.  This blocks a later
            # request from retrospectively learning from old native history.
            "processed_batches": [],
            "selected_skill_versions": [],
            "event_selection_settings": self.event_selection_settings.evidence() if self.event_selection_settings else None,
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._empty_state()
        state = read_json(self.state_path)
        if state.get("kind") not in {"r008_durable_overlay_state", "r012_durable_overlay_state"} or state.get("trial_id") != self.trial_id:
            raise OverlayError("overlay state belongs to a different trial or schema")
        if not isinstance(state.get("events"), list) or not isinstance(state.get("selected_skill_versions"), list):
            raise OverlayError("overlay state has an invalid schema")
        # Version 1 only existed as an offline prototype.  Keep an existing
        # state readable, while making all subsequent attempts/batches
        # durable under the current R012 invariants.
        state.setdefault("attempt_count", int(state.get("request_count", 0)))
        state.setdefault("overlay_initialized", True)
        state.setdefault("processed_batches", [
            {
                "anchor": deepcopy(event["anchor"]),
                "anchor_id": event["anchor"]["anchor_id"],
                "decision": "migrated_existing_event",
                "first_seen_attempt_ordinal": int(state["attempt_count"]),
            }
            for event in state["events"]
            if isinstance(event, dict) and isinstance(event.get("anchor"), dict) and isinstance(event["anchor"].get("anchor_id"), str)
        ])
        configured_settings = self.event_selection_settings.evidence() if self.event_selection_settings else None
        recorded_settings = state.setdefault("event_selection_settings", configured_settings)
        if recorded_settings != configured_settings:
            raise OverlayError("event selection settings changed within an existing trial")
        for event in state["events"]:
            if not isinstance(event, dict):
                raise OverlayError("overlay state contains a non-object event")
            if event.get("status") is None:
                # R008 carried an already relocated event.  R012 must never
                # keep carrying it.  Preserve the old relocation evidence as
                # a migration record instead of silently deleting provenance.
                if event.get("relocation") is not None:
                    event["status"] = "retired_native_compaction"
                    event["retirement"] = {
                        "reason": "r012_migration_of_r008_carried_event",
                        "compaction": deepcopy(event.get("relocation", {}).get("compaction")),
                    }
                    event.setdefault("retirement_history", []).append(
                        {"at_attempt_ordinal": int(state["attempt_count"]), **deepcopy(event["retirement"])}
                    )
                    event["relocation"] = None
                else:
                    event["status"] = "active"
            if event.get("status") not in {"active", "retired_native_compaction"}:
                raise OverlayError("overlay state has an invalid event status")
        state["schema_version"] = 3
        state["kind"] = "r012_durable_overlay_state"
        if not isinstance(state.get("processed_batches"), list):
            raise OverlayError("overlay state has invalid processed_batches")
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        write_json(self.state_path, state)

    def _record_attempt(self, attempt_ordinal: int, record: dict[str, Any]) -> None:
        # ``attempt_count`` advances even when no request can be forwarded, so
        # an unknown-anchor/context failure can never be overwritten by a
        # retry which happens to use the same forwarded-request ordinal.
        write_json(self.evidence_dir / "upstream_requests" / f"attempt-{attempt_ordinal:04d}.json", record)

    def record_proxy_rejection(
        self,
        payload: dict[str, Any],
        *,
        outcome: str,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Durably retain an M3 proxy limit rejection before any upstream I/O."""
        next_state = deepcopy(self.state)
        attempt_ordinal = int(next_state["attempt_count"]) + 1
        next_state["attempt_count"] = attempt_ordinal
        next_state["updated_at_utc"] = utc_now()
        next_state["last_attempt_outcome"] = outcome
        self._save_state(next_state)
        self.state = next_state
        record: dict[str, Any] = {
            "schema_version": 2,
            "kind": "r008_proxy_limit_rejection",
            "trial_id": self.trial_id,
            "attempt_ordinal": attempt_ordinal,
            "forwarded_request_ordinal": int(next_state["request_count"]) + 1,
            "created_at_utc": utc_now(),
            "native_request": deepcopy(payload),
            "proxy_outcome": outcome,
            "error_code": code,
            "error": message,
            "state_path": str(self.state_path),
        }
        if details:
            record["limit_details"] = deepcopy(details)
        self._record_attempt(attempt_ordinal, record)
        return record

    def prepare_native_summary(self, payload: dict[str, Any], *, permit: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Persist one unmodified native-summary request for a public-hook permit.

        A native OpenClaw summary is not a solver decision.  It must therefore
        never be overlaid, selected against, or checked against CODESKILL's
        solver-input ceiling.  The sidecar's permit gate supplies the only
        authorization; it is tied to an isolated native session and is later
        retired only by an independently read SQLite transition.
        """
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list) or not all(
            isinstance(item, dict) for item in payload["messages"]
        ):
            raise OverlayError("OpenAI-compatible payload requires a messages object list")
        if not isinstance(permit, dict) or permit.get("kind") != "r012_public_hook_native_summary_permit":
            raise OverlayError("native summary requires a validated public-hook permit")
        next_state = deepcopy(self.state)
        attempt_ordinal = int(next_state["attempt_count"]) + 1
        forwarded_request_ordinal = int(next_state["request_count"]) + 1
        next_state["attempt_count"] = attempt_ordinal
        next_state["request_count"] = forwarded_request_ordinal
        next_state["updated_at_utc"] = utc_now()
        next_state["last_attempt_outcome"] = "native_summary_forwardable"
        forwarded = deepcopy(payload)
        exact_tokens = int(self.token_counter(forwarded))
        record: dict[str, Any] = {
            "schema_version": 1,
            "kind": "r012_native_summary_upstream_request",
            "trial_id": self.trial_id,
            "attempt_ordinal": attempt_ordinal,
            "forwarded_request_ordinal": forwarded_request_ordinal,
            "created_at_utc": utc_now(),
            "native_request": deepcopy(payload),
            "forwarded_request": deepcopy(forwarded),
            "exact_forwarded_input_tokens": exact_tokens,
            "token_count_scope": "complete_native_summary_payload",
            "max_input_tokens": None,
            "native_summary_permit": deepcopy(permit),
            "overlay_disposition": {
                "kind": "native_summary_bypass",
                "reason": "public_hook_permit_for_exact_isolated_native_session",
                "payload_or_prompt_heuristic": "not_used",
            },
            "outcome": "native_summary_forwardable",
            "state_path": str(self.state_path),
        }
        self._save_state(next_state)
        self.state = next_state
        self._record_attempt(attempt_ordinal, record)
        return forwarded, record

    @staticmethod
    def _initial_user_index(messages: list[dict[str, Any]]) -> int | None:
        return next((index for index, message in enumerate(messages) if message.get("role") == "user"), None)

    @staticmethod
    def _anchor_positions(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        positions: dict[str, dict[str, Any]] = {}
        for batch in _complete_batches(messages):
            anchor_id = batch["anchor_id"]
            if anchor_id in positions:
                raise OverlayError("native history has ambiguous duplicate complete tool-batch anchors")
            positions[anchor_id] = batch
        return positions

    @staticmethod
    def _processed_anchor_ids(state: dict[str, Any]) -> set[str]:
        return {
            str(item["anchor_id"])
            for item in state["processed_batches"]
            if isinstance(item, dict) and isinstance(item.get("anchor_id"), str)
        }

    @staticmethod
    def _mark_processed_batch(
        state: dict[str, Any],
        *,
        anchor: dict[str, Any],
        decision: str,
        attempt_ordinal: int,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        result = {
            "anchor_id": anchor["anchor_id"],
            "anchor": deepcopy(anchor),
            "decision": decision,
            "first_seen_attempt_ordinal": attempt_ordinal,
            "recorded_at_utc": utc_now(),
        }
        if metadata:
            result["selection_metadata"] = deepcopy(metadata)
        state["processed_batches"].append(result)

    def _select_task_once(self, state: dict[str, Any], messages: list[dict[str, Any]], record: dict[str, Any]) -> None:
        if not self.enable_task or state["task"] is not None:
            return
        index = self._initial_user_index(messages)
        if index is None:
            raise OverlayAnchorEvidenceError("native history has no original task user message")
        original = messages[index]
        selected = _selection(self.task_selector(original, messages) if self.task_selector else None, phase="task")
        skills = selected["skills"]
        keys = [{"skill_id": skill_id, "version": version} for skill_id, version in (_skill_key(skill) for skill in skills)]
        state["task"] = {
            "initial_native_message_fingerprint": _message_fingerprint(original),
            "initial_native_message_index_at_selection": index,
            "skills": skills,
            "block": _task_block(skills) if skills else None,
            "bank_snapshot": selected["bank_snapshot"],
            "query": selected["query"],
            "relocation": None,
        }
        state["selected_skill_versions"].extend(keys)
        task_block = _task_block(skills) if skills else None
        record["task_selection"] = {
            "selected": keys,
            "bank_snapshot": selected["bank_snapshot"],
            "query": selected["query"],
            "block": task_block,
            "block_sha256": sha256_text(task_block) if task_block is not None else None,
            "injected_skills": [
                {
                    "skill": deepcopy(skill),
                    "rendered_skill_sha256": sha256_text(render_skill(skill)),
                    "phase": "task",
                }
                for skill in skills
            ],
        }

    @staticmethod
    def _active_event_keys(state: dict[str, Any]) -> set[tuple[str, int]]:
        return {
            _skill_key(event["skill"])
            for event in state["events"]
            if isinstance(event, dict) and event.get("status", "active") == "active" and isinstance(event.get("skill"), dict)
        }

    def _select_new_events(
        self,
        state: dict[str, Any],
        messages: list[dict[str, Any]],
        record: dict[str, Any],
        *,
        payload: dict[str, Any],
        carried: list[dict[str, Any]],
        attempt_ordinal: int,
        first_native_history: bool,
    ) -> None:
        if not self.enable_event:
            return
        existing_anchors = self._processed_anchor_ids(state)
        active_event_keys = self._active_event_keys(state)
        selections: list[dict[str, Any]] = []
        for anchor in _complete_batches(messages):
            if anchor["anchor_id"] in existing_anchors:
                continue
            selection_record: dict[str, Any] = {"anchor": anchor}
            # A durable proxy must start before the trial's first model call.
            # If it is deliberately attached to a pre-existing history, do not
            # manufacture an event which the native solver already passed.
            if first_native_history:
                selection_record["decision"] = "preexisting_before_overlay"
                self._mark_processed_batch(
                    state, anchor=anchor, decision=selection_record["decision"], attempt_ordinal=attempt_ordinal
                )
                existing_anchors.add(anchor["anchor_id"])
                selections.append(selection_record)
                continue
            # An event may only be injected at the immediate next decision.
            # A later native assistant message means this batch is historical,
            # and a later request must not backfill an event before it.
            if anchor["after_native_index"] != len(messages) - 1:
                selection_record["decision"] = "historical_batch_not_at_request_boundary"
                self._mark_processed_batch(
                    state, anchor=anchor, decision=selection_record["decision"], attempt_ordinal=attempt_ordinal
                )
                existing_anchors.add(anchor["anchor_id"])
                selections.append(selection_record)
                continue
            prefix = messages[: anchor["after_native_index"] + 1]
            selected = _selection(self.event_selector(anchor, prefix) if self.event_selector else None, phase="event")
            skills = selected["skills"]
            if len(skills) > 1:
                if self.event_selection_settings is None:
                    raise OverlayError(
                        "multiple event skills require explicit EventSelectionSettings from a development profile"
                    )
                if len(skills) > self.event_selection_settings.max_matching_skills:
                    raise OverlayError(
                        f"selector returned {len(skills)} event skills above configured cap {self.event_selection_settings.max_matching_skills}"
                    )
            selection_record.update(
                {
                    "bank_snapshot": selected["bank_snapshot"],
                    "query": selected["query"],
                    "selector_prefix_native_message_count": len(prefix),
                    "event_selection_settings": self.event_selection_settings.evidence() if self.event_selection_settings else None,
                }
            )
            if not skills:
                selection_record["decision"] = "no_skill_selected"
            else:
                candidates: list[dict[str, Any]] = []
                injected: list[dict[str, Any]] = []
                seen_in_selection: set[tuple[str, int]] = set()
                new_events: list[tuple[dict[str, Any], str]] = []
                for skill in skills:
                    skill_id, version = _skill_key(skill)
                    candidate = {"skill_id": skill_id, "version": version}
                    key = (skill_id, version)
                    if key in seen_in_selection:
                        candidate["decision"] = "deduplicated_same_selector_result"
                    elif key in active_event_keys:
                        candidate["decision"] = "deduplicated_prior_version"
                    else:
                        block = _event_block(skill)
                        new_events.append((skill, block))
                        candidate["decision"] = "selected_pending_event_skill_budget"
                    seen_in_selection.add(key)
                    candidates.append(candidate)
                for skill, block in new_events:
                    skill_id, version = _skill_key(skill)
                    key = (skill_id, version)
                    candidate = next(item for item in candidates if item["skill_id"] == skill_id and item["version"] == version)
                    injection = {
                        "phase": "event",
                        "anchor_id": anchor["anchor_id"],
                        "attempt_ordinal": attempt_ordinal,
                        "forwarded_request_ordinal": int(state["request_count"]) + 1,
                        "block_sha256": sha256_text(block),
                    }
                    event = {
                        "anchor": anchor,
                        "skill": deepcopy(skill),
                        "block": block,
                        "bank_snapshot": selected["bank_snapshot"],
                        "query": selected["query"],
                        "status": "active",
                        "injection": injection,
                    }
                    state["events"].append(event)
                    state["selected_skill_versions"].append(
                        {"skill_id": skill_id, "version": version, "phase": "event", "anchor_id": anchor["anchor_id"]}
                    )
                    active_event_keys.add(key)
                    candidate["decision"] = "injected_after_complete_batch"
                    injected.append({"skill": deepcopy(skill), **injection})
                selection_record["candidate_decisions"] = candidates
                selection_record["injected_skills"] = injected
                if injected:
                    selection_record["decision"] = "injected_after_complete_batch"
                elif all(item["decision"] == "deduplicated_prior_version" for item in candidates):
                    selection_record["decision"] = "deduplicated_prior_version"
                else:
                    selection_record["decision"] = "deduplicated_selector_result"
                if len(candidates) == 1:
                    selection_record["selected_skill_id"] = candidates[0]["skill_id"]
                    selection_record["selected_skill_version"] = candidates[0]["version"]
            self._mark_processed_batch(
                state,
                anchor=anchor,
                decision=selection_record["decision"],
                attempt_ordinal=attempt_ordinal,
                metadata={
                    key: selection_record[key]
                    for key in ("bank_snapshot", "query", "event_selection_settings", "candidate_decisions", "injected_skills")
                    if key in selection_record
                },
            )
            existing_anchors.add(anchor["anchor_id"])
            selections.append(selection_record)
        record["event_selection"] = selections

    def _enforce_active_event_skill_budget(
        self,
        state: dict[str, Any],
        payload: dict[str, Any],
        native_messages: list[dict[str, Any]],
        carried: list[dict[str, Any]],
        record: dict[str, Any],
        *,
        attempt_ordinal: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Measure every active event block in this exact forwarded payload.

        The frozen R013 scope is an engineering accounting rule, not a hidden
        relevance policy: compare the full request with all active event blocks
        against the identical task/carry payload with no event blocks.  It runs
        on every request, including retries that select no new event.
        """
        with_events = deepcopy(payload)
        with_events["messages"] = self._render_overlay(
            state, native_messages, None, {}, attempt_ordinal=attempt_ordinal, carried=carried
        )
        settings = self.event_selection_settings
        if settings is None or settings.skill_token_budget is None:
            return with_events["messages"], with_events
        without_event_state = deepcopy(state)
        without_event_state["events"] = []
        without_events = deepcopy(payload)
        without_events["messages"] = self._render_overlay(
            without_event_state, native_messages, None, {}, attempt_ordinal=attempt_ordinal, carried=carried
        )
        without_tokens = int(self.token_counter(without_events))
        with_tokens = int(self.token_counter(with_events))
        active_events = [
            {"skill_id": event["skill"]["skill_id"], "version": event["skill"]["version"], "anchor_id": event["anchor"]["anchor_id"]}
            for event in state["events"]
            if event.get("status", "active") == "active"
        ]
        injected_skill_tokens = with_tokens - without_tokens
        record["event_skill_token_budget"] = {
            "budget": settings.skill_token_budget,
            "scope": settings.skill_token_budget_scope,
            "without_active_event_blocks_complete_payload_tokens": without_tokens,
            "with_active_event_blocks_complete_payload_tokens": with_tokens,
            "active_event_block_tokens": injected_skill_tokens,
            "active_event_identities": active_events,
        }
        if injected_skill_tokens > settings.skill_token_budget:
            raise OverlayEventSkillBudgetError(
                "active event skill blocks require "
                f"{injected_skill_tokens} complete-payload tokens above the frozen event budget {settings.skill_token_budget}"
            )
        return with_events["messages"], with_events

    @staticmethod
    def _valid_compaction_evidence(
        value: dict[str, Any] | None,
        *,
        before_forwarded_request_ordinal: int,
        after_attempt_ordinal: int,
    ) -> bool:
        """Accept only native evidence bound to this disappearance transition.

        A stale compaction marker cannot justify an unrelated later anchor
        loss.  The proxy writes the before/after ordinals and the native
        session evidence reference into the attempt artifact for review.
        """
        return (
            isinstance(value, dict)
            and value.get("kind") == "native_compaction"
            and value.get("confirmed") is True
            and isinstance(value.get("compaction_id"), str)
            and bool(value["compaction_id"])
            and isinstance(value.get("native_session_event_ref"), str)
            and bool(value["native_session_event_ref"])
            and isinstance(value.get("observed_at_utc"), str)
            and bool(value["observed_at_utc"])
            and value.get("before_forwarded_request_ordinal") == before_forwarded_request_ordinal
            and value.get("after_proxy_attempt_ordinal") == after_attempt_ordinal
        )

    def _apply_relocations(
        self,
        state: dict[str, Any],
        messages: list[dict[str, Any]],
        compaction_evidence: dict[str, Any] | None,
        record: dict[str, Any],
        *,
        attempt_ordinal: int,
    ) -> list[dict[str, Any]]:
        anchors = self._anchor_positions(messages)
        carried: list[dict[str, Any]] = []
        evidence_is_current = self._valid_compaction_evidence(
            compaction_evidence,
            before_forwarded_request_ordinal=int(state["request_count"]),
            after_attempt_ordinal=attempt_ordinal,
        )
        recovered: list[dict[str, str]] = []
        continued: list[dict[str, str]] = []
        retired_events: list[dict[str, Any]] = []
        task = state.get("task")
        if isinstance(task, dict) and task.get("block"):
            task_present = any(_message_fingerprint(message) == task["initial_native_message_fingerprint"] for message in messages)
            if task_present:
                if task.get("relocation") is not None:
                    recovered.append({"kind": "task", "anchor": "initial_native_user"})
                    task["relocation"] = None
            else:
                # The first loss needs evidence which is bound to this
                # request.  Once recorded, the same compacted native history
                # legitimately persists through later decisions and must keep
                # carrying the prior without demanding a fictitious new
                # compaction transition.
                if task.get("relocation") is not None:
                    continued.append({"kind": "task", "anchor": "initial_native_user"})
                elif not evidence_is_current:
                    raise OverlayAnchorEvidenceError("initial task anchor disappeared without current, explicit native compaction evidence")
                else:
                    relocation = {"reason": "native_compaction_removed_initial_anchor", "compaction": deepcopy(compaction_evidence)}
                    task["relocation"] = relocation
                    task.setdefault("relocation_history", []).append({"at_attempt_ordinal": attempt_ordinal, **relocation})
                carried.append({"role": "user", "content": "[CODESKILL CARRIED TASK PRIOR]\n" + task["block"]})
        for event in state["events"]:
            anchor_id = event["anchor"]["anchor_id"]
            status = event.get("status", "active")
            if status == "retired_native_compaction":
                # R012 deliberately does not resurrect an old event block if
                # a native history happens to reappear.  A later matching
                # batch may retrieve and inject the same ID/version anew.
                continue
            if status != "active":
                raise OverlayError(f"event {anchor_id} has an invalid status {status!r}")
            if anchor_id in anchors:
                continue
            if not evidence_is_current:
                raise OverlayAnchorEvidenceError(f"event anchor {anchor_id} disappeared without current, explicit native compaction evidence")
            retirement = {
                "reason": "native_compaction_removed_tool_batch_anchor",
                "anchor_id": anchor_id,
                "compaction": deepcopy(compaction_evidence),
            }
            event["status"] = "retired_native_compaction"
            event["retirement"] = retirement
            event.setdefault("retirement_history", []).append({"at_attempt_ordinal": attempt_ordinal, **retirement})
            retired_events.append(
                {
                    "anchor_id": anchor_id,
                    "skill_id": event["skill"].get("skill_id"),
                    "version": event["skill"].get("version"),
                    "injection": deepcopy(event.get("injection")),
                    "retirement": deepcopy(retirement),
                }
            )
        if recovered:
            record["recovered_anchors"] = recovered
        if continued:
            record["continued_carried_priors"] = {
                "count": len(continued),
                "anchors": continued,
                "reason": "previously_confirmed_native_compaction_relocation",
            }
        if retired_events:
            record["retired_event_priors"] = {
                "count": len(retired_events),
                "events": retired_events,
                "current_transition_evidence": deepcopy(compaction_evidence),
                "reason": "r012_verified_native_compaction_retires_event_blocks_without_carry",
            }
        if carried:
            relocation_evidence: list[dict[str, Any]] = []
            if isinstance(task, dict) and task.get("relocation") is not None:
                relocation_evidence.append({"kind": "task", "compaction": deepcopy(task["relocation"].get("compaction"))})
            record["carried_priors"] = {
                "count": len(carried),
                "current_transition_evidence": deepcopy(compaction_evidence) if evidence_is_current else None,
                "relocation_evidence": relocation_evidence,
                "reason": "confirmed_native_compaction_task_anchor_absent_or_persisting",
            }
        return carried

    def _render_overlay(
        self,
        state: dict[str, Any],
        native_messages: list[dict[str, Any]],
        compaction_evidence: dict[str, Any] | None,
        record: dict[str, Any],
        *,
        attempt_ordinal: int,
        carried: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if carried is None:
            carried = self._apply_relocations(
                state, native_messages, compaction_evidence, record, attempt_ordinal=attempt_ordinal
            )
        current_anchors = self._anchor_positions(native_messages)
        anchor_to_events: dict[int, list[dict[str, Any]]] = {}
        for event in state["events"]:
            current_anchor = current_anchors.get(event["anchor"]["anchor_id"])
            if event.get("status", "active") == "active" and current_anchor is not None:
                # Do not reuse the selection-time list index: native context
                # may have been compacted/reframed before this unchanged batch.
                anchor_to_events.setdefault(int(current_anchor["after_native_index"]), []).append(event)
        task = state.get("task")
        initial_fingerprint = task.get("initial_native_message_fingerprint") if isinstance(task, dict) else None
        output: list[dict[str, Any]] = []
        inserted_carried = False
        inserted_task = False
        for index, message in enumerate(native_messages):
            if not inserted_carried and message.get("role") != "system":
                output.extend(deepcopy(carried))
                inserted_carried = True
            if isinstance(task, dict) and task.get("block") and not inserted_task and _message_fingerprint(message) == initial_fingerprint:
                output.append(_append_to_user_message(message, task["block"]))
                inserted_task = True
            else:
                output.append(deepcopy(message))
            for event in anchor_to_events.get(index, []):
                output.append({"role": "user", "content": event["block"]})
        if not inserted_carried:
            output.extend(deepcopy(carried))
        return output

    def prepare(self, payload: dict[str, Any], *, compaction_evidence: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build one actual upstream payload and persist a request-level record.

        The caller forwards only the returned payload. A context error is
        raised before forwarding and records the unmodified native request.
        """
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list) or not all(isinstance(item, dict) for item in payload["messages"]):
            raise OverlayError("OpenAI-compatible payload requires a messages object list")
        native_messages = payload["messages"]
        next_state = deepcopy(self.state)
        attempt_ordinal = int(next_state["attempt_count"]) + 1
        next_state["attempt_count"] = attempt_ordinal
        forwarded_request_ordinal = int(next_state["request_count"]) + 1
        record: dict[str, Any] = {
            "schema_version": 3,
            "kind": "r012_actual_upstream_request",
            "trial_id": self.trial_id,
            "attempt_ordinal": attempt_ordinal,
            "forwarded_request_ordinal": forwarded_request_ordinal,
            "created_at_utc": utc_now(),
            "native_request": deepcopy(payload),
            "compaction_evidence": compaction_evidence,
            "event_selection_settings": self.event_selection_settings.evidence() if self.event_selection_settings else None,
        }
        try:
            first_native_history = not bool(next_state["overlay_initialized"])
            self._select_task_once(next_state, native_messages, record)
            # Retirement precedes fresh event selection.  Therefore a skill
            # that was truly removed by this native compaction is no longer
            # active and may be selected again at a later matching boundary.
            carried = self._apply_relocations(
                next_state,
                native_messages,
                compaction_evidence,
                record,
                attempt_ordinal=attempt_ordinal,
            )
            self._select_new_events(
                next_state,
                native_messages,
                record,
                payload=payload,
                carried=carried,
                attempt_ordinal=attempt_ordinal,
                first_native_history=first_native_history,
            )
            next_state["overlay_initialized"] = True
            forwarded_messages, forwarded = self._enforce_active_event_skill_budget(
                next_state,
                payload,
                native_messages,
                carried,
                record,
                attempt_ordinal=attempt_ordinal,
            )
            exact_tokens = int(self.token_counter(forwarded))
            record["forwarded_request"] = deepcopy(forwarded)
            record["exact_forwarded_input_tokens"] = exact_tokens
            record["max_input_tokens"] = self.max_input_tokens
            record["token_count_scope"] = "complete_forwarded_openai_payload"
            if exact_tokens > self.max_input_tokens:
                # The candidate was necessary to measure the complete prompt,
                # but it was never sent.  Do not durably claim that a newly
                # selected task/event skill was injected, deduplicated, or
                # charged to the overlay budget.  Existing prior relocations
                # are different: they may already be durable state and need
                # to survive this context rejection for its retry.
                uncommitted: dict[str, Any] = {}
                if "task_selection" in record:
                    uncommitted["task"] = record.pop("task_selection")
                if "event_selection" in record:
                    uncommitted["event"] = record.pop("event_selection")
                record["uncommitted_selection"] = uncommitted
                record["preflight_candidate_forwarded_request"] = record.pop("forwarded_request")

                preserved_state = deepcopy(self.state)
                preserved_state["attempt_count"] = attempt_ordinal
                # Render only already-durable priors to persist a relocation
                # caused by the current native compaction.  It intentionally
                # sees none of ``next_state``'s just-selected task/event
                # candidates.
                self._render_overlay(
                    preserved_state,
                    native_messages,
                    compaction_evidence,
                    record,
                    attempt_ordinal=attempt_ordinal,
                )
                preserved_state["updated_at_utc"] = utc_now()
                preserved_state["last_attempt_outcome"] = "context_or_overlay_error"
                self._save_state(preserved_state)
                self.state = preserved_state
                record["outcome"] = "context_or_overlay_error"
                record["error_type"] = "OverlayInputLimitError"
                record["error"] = f"forwarded input {exact_tokens} exceeds configured input budget {self.max_input_tokens}"
                record["state_path"] = str(self.state_path)
                self._record_attempt(attempt_ordinal, record)
                raise OverlayInputLimitError(record["error"])
            next_state["request_count"] = forwarded_request_ordinal
            next_state["updated_at_utc"] = utc_now()
            self._save_state(next_state)
            self.state = next_state
            record["state_path"] = str(self.state_path)
            record["outcome"] = "forwardable"
            self._record_attempt(attempt_ordinal, record)
            return forwarded, record
        except OverlayInputLimitError:
            # The bounded branch above has already persisted only existing
            # relocations plus an explicit uncommitted-candidate record.
            # Saving ``next_state`` here would incorrectly commit a skill
            # which never reached the upstream solver.
            raise
        except OverlayEventSkillBudgetError as error:
            # A budget rejection must not make a newly selected event appear
            # supplied. Preserve only previously durable priors/relocations;
            # a retry re-evaluates this request under the same frozen policy.
            uncommitted: dict[str, Any] = {}
            if "task_selection" in record:
                uncommitted["task"] = record.pop("task_selection")
            if "event_selection" in record:
                uncommitted["event"] = record.pop("event_selection")
            record["uncommitted_selection"] = uncommitted
            preserved_state = deepcopy(self.state)
            preserved_state["attempt_count"] = attempt_ordinal
            self._render_overlay(
                preserved_state,
                native_messages,
                compaction_evidence,
                record,
                attempt_ordinal=attempt_ordinal,
            )
            preserved_state["updated_at_utc"] = utc_now()
            preserved_state["last_attempt_outcome"] = "event_skill_budget_error"
            self._save_state(preserved_state)
            self.state = preserved_state
            record["outcome"] = "event_skill_budget_error"
            record["error_type"] = "OverlayEventSkillBudgetError"
            record["error"] = str(error)
            record["state_path"] = str(self.state_path)
            self._record_attempt(attempt_ordinal, record)
            raise
        except BaseException as error:
            # Preserve all completed selector decisions and the monotonically
            # increasing attempt ID even when the upstream call is blocked.
            # A retry may re-overlay a prior, but may never query an old batch
            # again or overwrite this failure's evidence.
            next_state["updated_at_utc"] = utc_now()
            next_state["last_attempt_outcome"] = "context_or_overlay_error"
            self._save_state(next_state)
            self.state = next_state
            record["outcome"] = "context_or_overlay_error"
            record["error_type"] = type(error).__name__
            record["error"] = str(error)
            record["state_path"] = str(self.state_path)
            self._record_attempt(attempt_ordinal, record)
            raise
