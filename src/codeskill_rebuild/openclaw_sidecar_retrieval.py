"""Frozen R012 bank retrieval for the standalone OpenClaw sidecar.

This module is deliberately an adapter around the existing ``SkillBank`` and
``MiniLMEncoder`` interfaces.  It does not construct a bank, update a bank,
or choose a missing relevance policy.  A running sidecar gets exactly one
already-frozen lifecycle assignment and rejects a changed profile, trial, or
bank snapshot before it can inject a skill.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .bank import SkillBank
from .r012_execution import profile_sha256, validate_execution_profile
from .relevance_judge import JevJudge
from .retrieval import MiniLMEncoder, rank, score_candidates
from .retrieval_query import QUERY_CONSTRUCTION, event_query_fields, judge_state, task_query_fields
from .types import canonical_instance_id, canonical_json, read_json, sha256_file, sha256_text


class SidecarRetrievalError(ValueError):
    """The configured R012 assignment cannot safely supply a sidecar."""


class QueryEncoder(Protocol):
    def load(self) -> dict[str, Any]: ...

    def index_skill(self, skill: dict[str, Any]) -> tuple[list[float], dict[str, Any]]: ...

    def encode_query(self, query_type: str, fields: dict[str, str]) -> tuple[list[float], dict[str, Any]]: ...


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SidecarRetrievalError(f"{field} must be an object")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SidecarRetrievalError(f"{field} must be a nonempty string")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SidecarRetrievalError(f"{field} must be a positive integer")
    return value


def _score(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not -1.0 <= float(value) <= 1.0:
        raise SidecarRetrievalError(f"{field} must be a number from -1 through 1")
    return float(value)


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        values: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                values.append(item["text"])
        return "\n".join(values)
    return ""


def _first_user_text(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") == "user":
            return _message_text(message)
    return ""


@dataclass(frozen=True)
class RetrievalRules:
    task_threshold: float
    task_limit: int
    task_rule_ref: str
    event_threshold: float
    event_rule_ref: str
    event_max_matching_skills: int
    event_skill_token_budget: int
    event_profile_ref: str
    event_skill_token_budget_scope: str
    enable_task: bool
    enable_event: bool


@dataclass
class FrozenBankSelectors:
    """Select task/event skills from one immutable R012 assignment only."""

    trial_id: str
    instance_id: str
    bank: SkillBank
    bank_snapshot: dict[str, Any]
    lifecycle_state_path: Path
    lifecycle_state_sha256: str
    profile_sha256: str
    rules: RetrievalRules
    encoder: QueryEncoder
    # P2 (docs/DECISIONS.md): absent for every run frozen before 2026-09-28,
    # which keeps the R013 query and threshold-only selection byte-identical.
    judge: JevJudge | None = None

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        *,
        encoder: QueryEncoder | None = None,
        judge: JevJudge | None = None,
    ) -> "FrozenBankSelectors":
        retrieval = _mapping(config.get("retrieval"), "retrieval")
        state_path = Path(_text(retrieval.get("lifecycleStatePath"), "retrieval.lifecycleStatePath"))
        if not state_path.is_file():
            raise SidecarRetrievalError("retrieval.lifecycleStatePath does not exist or is not a file")
        stated_hash = _text(retrieval.get("lifecycleStateSha256"), "retrieval.lifecycleStateSha256")
        actual_hash = sha256_file(state_path)
        # The sidecar is a live request boundary and must always bind its
        # configured lifecycle digest to the bytes it reads.  Manager-only
        # recovery does not launch the sidecar, so it must not weaken this
        # check with a caller-controlled escape hatch.
        if actual_hash != stated_hash:
            raise SidecarRetrievalError("retrieval.lifecycleStateSha256 differs from the lifecycle state file")
        state = _mapping(read_json(state_path), "R012 lifecycle state")
        state_kind = state.get("kind")
        if state_kind not in {"r012_instance_lifecycle_state", "r015_c_only_protocol_state", "swe_pilot_protocol_state"}:
            raise SidecarRetrievalError(
                "retrieval lifecycle state must be an official R012, C-only, or SWE pilot protocol state"
            )
        if state_kind == "r012_instance_lifecycle_state":
            profile_value = state.get("profile")
        else:
            # C-only and SWE pilot rounds carry the frozen R012 selection
            # profile inside their versioned protocol state.
            profile_value = state.get("retrieval_profile")
        profile = validate_execution_profile(profile_value)
        computed_profile_hash = profile_sha256(profile)
        if state.get("profile_sha256") != computed_profile_hash:
            raise SidecarRetrievalError("lifecycle state profile hash differs from its profile")
        requested_profile_hash = _text(retrieval.get("profileSha256"), "retrieval.profileSha256")
        if requested_profile_hash != computed_profile_hash:
            raise SidecarRetrievalError("retrieval.profileSha256 differs from the frozen lifecycle profile")
        trial_id = _text(config.get("trialId"), "trialId")
        requested_trial = _text(retrieval.get("trialId"), "retrieval.trialId")
        if requested_trial != trial_id:
            raise SidecarRetrievalError("retrieval.trialId must equal the sidecar trialId")
        matching: dict[str, Any] | None = None
        if state_kind == "r012_instance_lifecycle_state":
            coordinator = _mapping(state.get("coordinator"), "lifecycle coordinator")
            instances = _mapping(coordinator.get("instances"), "lifecycle coordinator.instances")
            for group in instances.values():
                if not isinstance(group, dict):
                    continue
                assignments = group.get("assignments")
                if isinstance(assignments, dict) and isinstance(assignments.get(trial_id), dict):
                    matching = assignments[trial_id]
                    break
        else:
            rounds = _mapping(state.get("rounds"), "C-only protocol rounds")
            for round_state in rounds.values():
                if not isinstance(round_state, dict):
                    continue
                assignments = round_state.get("assignments")
                if isinstance(assignments, dict):
                    # C-only assignments are keyed by canonical task ID for
                    # sequential cursor updates, while the public sidecar is
                    # bound by the immutable trial ID.  Search the keyed
                    # records and require each record's own trial_id to match.
                    for assignment in assignments.values():
                        if isinstance(assignment, dict) and assignment.get("trial_id") == trial_id:
                            matching = assignment
                            break
                    if matching is not None:
                        break
        if matching is None:
            raise SidecarRetrievalError("retrieval.trialId is not a frozen lifecycle assignment")
        assignment = _mapping(matching, "frozen assignment")
        if assignment.get("status") != "pending":
            raise SidecarRetrievalError("sidecar retrieval requires a pending frozen assignment")
        if state_kind == "r012_instance_lifecycle_state":
            arm = _text(assignment.get("arm"), "frozen assignment.arm")
            instance_id = canonical_instance_id(_text(assignment.get("instance_id"), "frozen assignment.instance_id"))
        else:
            # Every C-only/SWE pilot assignment is its own full-lifecycle C
            # trial; the canonical instance is the exclusion identity.
            arm = "C"
            instance_id = canonical_instance_id(_text(assignment.get("task_id"), "frozen assignment.task_id"))
        configured_instance = canonical_instance_id(_text(retrieval.get("instanceId"), "retrieval.instanceId"))
        if configured_instance != instance_id:
            raise SidecarRetrievalError("retrieval.instanceId differs from the frozen assignment")
        snapshot = _mapping(assignment.get("frozen_bank"), "frozen assignment.frozen_bank")
        expected_snapshot_hash = sha256_text(canonical_json({
            "benchmark": snapshot.get("benchmark"),
            "sequence": snapshot.get("sequence"),
            "skills": snapshot.get("skills"),
        }))
        if snapshot.get("state_sha256") != expected_snapshot_hash:
            raise SidecarRetrievalError("frozen bank snapshot has an invalid state_sha256")
        stated_snapshot_hash = _text(retrieval.get("bankSnapshotSha256"), "retrieval.bankSnapshotSha256")
        if stated_snapshot_hash != expected_snapshot_hash:
            raise SidecarRetrievalError("retrieval.bankSnapshotSha256 differs from the frozen assignment")
        bank = SkillBank.from_dict({
            "benchmark": snapshot.get("benchmark"),
            "sequence": snapshot.get("sequence"),
            "skills": snapshot.get("skills"),
            "operations": [],
            "states": [snapshot],
        })
        rules = _rules(retrieval, profile, arm=arm)
        configured_judge = _p2_judge(retrieval, profile, rules, judge=judge)
        configured_encoder = encoder or _encoder(retrieval)
        return cls(
            trial_id=trial_id,
            instance_id=instance_id,
            bank=bank,
            bank_snapshot=deepcopy(snapshot),
            lifecycle_state_path=state_path,
            lifecycle_state_sha256=actual_hash,
            profile_sha256=computed_profile_hash,
            rules=rules,
            encoder=configured_encoder,
            judge=configured_judge,
        )

    def load_encoder(self) -> dict[str, Any]:
        return self.encoder.load()

    def _metadata(
        self,
        *,
        phase: str,
        threshold: float,
        limit: int,
        rule_ref: str,
        query: dict[str, Any],
        scored: list[dict[str, Any]],
        ranked: list[dict[str, Any]],
        index_records: dict[tuple[str, int], dict[str, Any]],
        eligibility: dict[str, Any],
    ) -> dict[str, Any]:
        selected_keys = {(item["skill"]["skill_id"], item["skill"]["version"]) for item in ranked}
        scored_by_key = {
            (item["skill"]["skill_id"], item["skill"]["version"]): (position, item)
            for position, item in enumerate(scored, start=1)
        }
        candidates = []
        for candidate in eligibility["candidates"]:
            value = deepcopy(candidate)
            key = (candidate["skill_id"], candidate["version"])
            scored_value = scored_by_key.get(key)
            if scored_value is None:
                value["selection_decision"] = "excluded_before_scoring"
                value["score"] = None
                value["score_rank"] = None
                value["index_record"] = None
            else:
                position, item = scored_value
                value["score"] = item["score"]
                value["score_rank"] = position
                value["index_record"] = deepcopy(index_records[key])
                if key in selected_keys:
                    value["selection_decision"] = "selected"
                elif item["score"] < threshold:
                    value["selection_decision"] = "excluded_below_threshold"
                    value["exclusion_reasons"].append("below_threshold")
                else:
                    value["selection_decision"] = "excluded_by_rank_limit"
                    value["exclusion_reasons"].append("rank_limit")
            candidates.append(value)
        budget = (
            {
                "applicable": True,
                "configured_tokens": self.rules.event_skill_token_budget,
                "scope": self.rules.event_skill_token_budget_scope,
                "applied_at_retrieval": False,
                "outcome_recorded_by": "durable_overlay_complete_payload_check",
            }
            if phase == "event"
            else {"applicable": False, "applied_at_retrieval": False}
        )
        return {
            "kind": "r013_frozen_bank_minilm_retrieval",
            "phase": phase,
            "trial_id": self.trial_id,
            "instance_id": self.instance_id,
            "lifecycle_state": {"path": str(self.lifecycle_state_path), "sha256": self.lifecycle_state_sha256},
            "profile_sha256": self.profile_sha256,
            "bank_snapshot": {
                "benchmark": self.bank_snapshot["benchmark"],
                "sequence": self.bank_snapshot["sequence"],
                "state_sha256": self.bank_snapshot["state_sha256"],
            },
            "selection_rule": {"ref": rule_ref, "threshold": threshold, "limit": limit},
            "query": deepcopy(query),
            "diagnostics": {
                "scope": "frozen_bank_before_threshold",
                "candidate_count": len(candidates),
                "eligible_before_scoring_count": len(scored),
                "deduplication": {
                    "applied_at_retrieval": False,
                    "outcome_recorded_by": "durable_overlay_selected_version_check",
                },
                "skill_token_budget": budget,
                "candidates": candidates,
            },
            "ranked": [
                {
                    "skill_id": item["skill"]["skill_id"],
                    "version": item["skill"]["version"],
                    "score": item["score"],
                    "index_text": item["index_text"],
                    "index_record": deepcopy(index_records[(item["skill"]["skill_id"], item["skill"]["version"])]),
                }
                for item in ranked
            ],
        }

    def _select(
        self,
        *,
        phase: str,
        fields: dict[str, str],
        threshold: float,
        limit: int,
        rule_ref: str,
        situation: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        eligibility = self.bank.eligibility_report(instance_id=self.instance_id, granularity=phase)
        eligible = eligibility["eligible_skills"]
        query_vector, query = self.encoder.encode_query(phase, fields)
        vectors: list[list[float]] = []
        index_records: dict[tuple[str, int], dict[str, Any]] = {}
        for skill in eligible:
            vector, index_record = self.encoder.index_skill(skill)
            vectors.append(vector)
            index_records[(skill["skill_id"], skill["version"])] = deepcopy(index_record)
        scored = score_candidates(query_vector, eligible, vectors)
        # With P2 the threshold is a prefilter and MiniLM only shortlists; the
        # judge makes the final pick of at most one skill.
        shortlist_size = self.judge.shortlist if self.judge is not None else limit
        ranked = rank(query_vector, eligible, vectors, threshold=threshold, limit=shortlist_size)
        metadata = self._metadata(
            phase=phase,
            threshold=threshold,
            limit=limit,
            rule_ref=rule_ref,
            query=query,
            scored=scored,
            ranked=ranked,
            index_records=index_records,
            eligibility=eligibility,
        )
        if self.judge is None:
            return {"skills": [deepcopy(item["skill"]) for item in ranked], "bank_snapshot": metadata["bank_snapshot"], "query": metadata}
        if not ranked:
            metadata["relevance_judge"] = {"status": "not_called", "reason": "no_candidate_above_prefilter"}
            return {"skills": [], "bank_snapshot": metadata["bank_snapshot"], "query": metadata}
        if situation is None:
            raise SidecarRetrievalError("P2 selection needs the judge situation")
        decision = self.judge.decide(phase, situation, [item["skill"] for item in ranked])
        picked = [] if decision["choice_index"] is None else [ranked[decision["choice_index"]]]
        picked_keys = {(item["skill"]["skill_id"], item["skill"]["version"]) for item in picked}
        for candidate in metadata["diagnostics"]["candidates"]:
            if candidate["selection_decision"] == "selected" and (candidate["skill_id"], candidate["version"]) not in picked_keys:
                candidate["selection_decision"] = "rejected_by_relevance_judge"
                candidate["exclusion_reasons"].append("relevance_judge")
        decision["shortlist"] = [{"skill_id": item["skill"]["skill_id"], "version": item["skill"]["version"], "score": item["score"]} for item in ranked]
        metadata["relevance_judge"] = decision
        return {"skills": [deepcopy(item["skill"]) for item in picked], "bank_snapshot": metadata["bank_snapshot"], "query": metadata}

    def select_task(self, original: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.rules.enable_task:
            raise SidecarRetrievalError("frozen assignment arm disables task skill injection")
        if self.judge is not None:
            text = _message_text(original)
            return self._select(
                phase="task",
                fields=task_query_fields(text, self.instance_id),
                threshold=self.rules.task_threshold,
                limit=self.rules.task_limit,
                rule_ref=self.rules.task_rule_ref,
                situation=judge_state(text, None, []),
            )
        context = "\n".join(_message_text(item) for item in messages if item.get("role") == "system")
        return self._select(
            phase="task",
            fields={"goal_problem": _message_text(original), "repo_context": context},
            threshold=self.rules.task_threshold,
            limit=self.rules.task_limit,
            rule_ref=self.rules.task_rule_ref,
        )

    def select_event(self, anchor: dict[str, Any], prefix: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.rules.enable_event:
            raise SidecarRetrievalError("frozen assignment arm disables event skill injection")
        assistant_index = anchor.get("assistant_index")
        result_indexes = anchor.get("tool_result_indices")
        if not isinstance(assistant_index, int) or not isinstance(result_indexes, list) or not all(isinstance(item, int) for item in result_indexes):
            raise SidecarRetrievalError("event anchor has an invalid OpenClaw tool-batch shape")
        if assistant_index < 0 or assistant_index >= len(prefix) or any(item < 0 or item >= len(prefix) for item in result_indexes):
            raise SidecarRetrievalError("event anchor indexes are outside the native request history")
        assistant = prefix[assistant_index]
        if self.judge is not None:
            results = [prefix[item] for item in result_indexes]
            first_user = _first_user_text(prefix)
            return self._select(
                phase="event",
                fields=event_query_fields(first_user, assistant, results),
                threshold=self.rules.event_threshold,
                limit=self.rules.event_max_matching_skills,
                rule_ref=self.rules.event_rule_ref,
                situation=judge_state(first_user, assistant, results),
            )
        recent_action = _message_text(assistant)
        calls = assistant.get("tool_calls")
        if calls is not None:
            recent_action += "\n" + canonical_json(calls)
        observations = "\n".join(_message_text(prefix[item]) for item in result_indexes)
        return self._select(
            phase="event",
            fields={
                "observation_errors_tests": observations,
                "recent_action": recent_action,
                "public_reasoning": _message_text(assistant),
                "task_context": _first_user_text(prefix),
            },
            threshold=self.rules.event_threshold,
            limit=self.rules.event_max_matching_skills,
            rule_ref=self.rules.event_rule_ref,
        )


def _rules(retrieval: dict[str, Any], profile: dict[str, Any], *, arm: str) -> RetrievalRules:
    task = _mapping(retrieval.get("taskSelection"), "retrieval.taskSelection")
    event = _mapping(retrieval.get("eventSelection"), "retrieval.eventSelection")
    profile_event = _mapping(profile.get("event_selection"), "frozen profile.event_selection")
    profile_ref = _text(profile_event.get("profile_ref"), "frozen profile.event_selection.profile_ref")
    if _text(event.get("profileRef"), "retrieval.eventSelection.profileRef") != profile_ref:
        raise SidecarRetrievalError("eventSelection.profileRef differs from the frozen R012 profile")
    if _text(event.get("selectionRuleRef"), "retrieval.eventSelection.selectionRuleRef") != _text(profile_event.get("selection_rule_ref"), "frozen profile.event_selection.selection_rule_ref"):
        raise SidecarRetrievalError("eventSelection.selectionRuleRef differs from the frozen R012 profile")
    configured_budget = _positive_int(event.get("skillTokenBudget"), "retrieval.eventSelection.skillTokenBudget")
    if configured_budget != _positive_int(profile_event.get("skill_token_budget"), "frozen profile.event_selection.skill_token_budget"):
        raise SidecarRetrievalError("eventSelection.skillTokenBudget differs from the frozen R012 profile")
    configured_maximum = _positive_int(event.get("maxMatchingSkills"), "retrieval.eventSelection.maxMatchingSkills")
    if configured_maximum != _positive_int(profile_event.get("max_matching_skills"), "frozen profile.event_selection.max_matching_skills"):
        raise SidecarRetrievalError("eventSelection.maxMatchingSkills differs from the frozen R012 profile")
    profile_injection = _mapping(profile.get("sidecar_injection"), "frozen profile.sidecar_injection")
    scope = _text(profile_injection.get("event_skill_token_budget_scope"), "frozen profile.sidecar_injection.event_skill_token_budget_scope")
    if scope != "complete_payload_active_event_blocks_delta":
        raise SidecarRetrievalError("frozen profile sidecar event budget scope is not the supported active-event complete-payload delta")
    if _text(event.get("budgetScope"), "retrieval.eventSelection.budgetScope") != scope:
        raise SidecarRetrievalError("eventSelection.budgetScope differs from the frozen R012 profile")
    arm_controls = _mapping(profile_injection.get("arms"), "frozen profile.sidecar_injection.arms")
    controls = _mapping(arm_controls.get(arm), f"frozen profile.sidecar_injection.arms.{arm}")
    enable_task = controls.get("enable_task")
    enable_event = controls.get("enable_event")
    if not isinstance(enable_task, bool) or not isinstance(enable_event, bool):
        raise SidecarRetrievalError(f"frozen profile sidecar injection controls for arm {arm} need boolean enable_task and enable_event")
    return RetrievalRules(
        task_threshold=_score(task.get("threshold"), "retrieval.taskSelection.threshold"),
        task_limit=_positive_int(task.get("maxMatchingSkills"), "retrieval.taskSelection.maxMatchingSkills"),
        task_rule_ref=_text(task.get("selectionRuleRef"), "retrieval.taskSelection.selectionRuleRef"),
        event_threshold=_score(event.get("threshold"), "retrieval.eventSelection.threshold"),
        event_rule_ref=_text(event.get("selectionRuleRef"), "retrieval.eventSelection.selectionRuleRef"),
        event_max_matching_skills=configured_maximum,
        event_skill_token_budget=configured_budget,
        event_profile_ref=profile_ref,
        event_skill_token_budget_scope=scope,
        enable_task=enable_task,
        enable_event=enable_event,
    )


def _p2_judge(
    retrieval: dict[str, Any],
    profile: dict[str, Any],
    rules: RetrievalRules,
    *,
    judge: JevJudge | None,
) -> JevJudge | None:
    """Bind the P2 query and judge to the frozen profile, or keep the R013 path when neither has it."""
    configured = retrieval.get("p2Selection")
    frozen = profile.get("p2_selection")
    if configured is None and frozen is None:
        if judge is not None:
            raise SidecarRetrievalError("a relevance judge was supplied without a frozen P2 selection profile")
        return None
    if configured is None or frozen is None or canonical_json(configured) != canonical_json(frozen):
        raise SidecarRetrievalError("retrieval.p2Selection must equal the frozen profile p2_selection")
    if configured.get("queryConstruction") != QUERY_CONSTRUCTION:
        raise SidecarRetrievalError(f"p2Selection.queryConstruction must be {QUERY_CONSTRUCTION}")
    if rules.task_limit != 1 or rules.event_max_matching_skills != 1:
        raise SidecarRetrievalError("P2 selection injects at most one skill per phase; set both maxMatchingSkills to 1")
    return judge or JevJudge.from_config(_mapping(configured.get("judge"), "retrieval.p2Selection.judge"))


def _encoder(retrieval: dict[str, Any]) -> MiniLMEncoder:
    config = _mapping(retrieval.get("encoder"), "retrieval.encoder")
    if config.get("kind") != "minilm":
        raise SidecarRetrievalError("retrieval.encoder.kind must be minilm")
    return MiniLMEncoder(
        repo_id=_text(config.get("repoId"), "retrieval.encoder.repoId"),
        revision=_text(config.get("revision"), "retrieval.encoder.revision"),
    )
