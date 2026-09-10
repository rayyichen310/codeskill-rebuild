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
from .retrieval import MiniLMEncoder, rank
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

    @classmethod
    def from_config(cls, config: dict[str, Any], *, encoder: QueryEncoder | None = None) -> "FrozenBankSelectors":
        retrieval = _mapping(config.get("retrieval"), "retrieval")
        state_path = Path(_text(retrieval.get("lifecycleStatePath"), "retrieval.lifecycleStatePath"))
        if not state_path.is_file():
            raise SidecarRetrievalError("retrieval.lifecycleStatePath does not exist or is not a file")
        stated_hash = _text(retrieval.get("lifecycleStateSha256"), "retrieval.lifecycleStateSha256")
        actual_hash = sha256_file(state_path)
        if actual_hash != stated_hash:
            raise SidecarRetrievalError("retrieval.lifecycleStateSha256 differs from the lifecycle state file")
        state = _mapping(read_json(state_path), "R012 lifecycle state")
        if state.get("kind") != "r012_instance_lifecycle_state":
            raise SidecarRetrievalError("retrieval lifecycle state must be kind=r012_instance_lifecycle_state")
        profile = validate_execution_profile(state.get("profile"))
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
        coordinator = _mapping(state.get("coordinator"), "lifecycle coordinator")
        instances = _mapping(coordinator.get("instances"), "lifecycle coordinator.instances")
        matching: dict[str, Any] | None = None
        for group in instances.values():
            if not isinstance(group, dict):
                continue
            assignments = group.get("assignments")
            if isinstance(assignments, dict) and isinstance(assignments.get(trial_id), dict):
                matching = assignments[trial_id]
                break
        if matching is None:
            raise SidecarRetrievalError("retrieval.trialId is not a frozen lifecycle assignment")
        assignment = _mapping(matching, "frozen assignment")
        if assignment.get("status") != "pending":
            raise SidecarRetrievalError("sidecar retrieval requires a pending frozen assignment")
        arm = _text(assignment.get("arm"), "frozen assignment.arm")
        instance_id = canonical_instance_id(_text(assignment.get("instance_id"), "frozen assignment.instance_id"))
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
        )

    def load_encoder(self) -> dict[str, Any]:
        return self.encoder.load()

    def _metadata(self, *, phase: str, threshold: float, limit: int, rule_ref: str, query: dict[str, Any], ranked: list[dict[str, Any]]) -> dict[str, Any]:
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
            "ranked": [
                {"skill_id": item["skill"]["skill_id"], "version": item["skill"]["version"], "score": item["score"], "index_text": item["index_text"]}
                for item in ranked
            ],
        }

    def _select(self, *, phase: str, fields: dict[str, str], threshold: float, limit: int, rule_ref: str) -> dict[str, Any]:
        eligible = self.bank.eligible(instance_id=self.instance_id, granularity=phase)
        query_vector, query = self.encoder.encode_query(phase, fields)
        vectors: list[list[float]] = []
        for skill in eligible:
            vector, _index = self.encoder.index_skill(skill)
            vectors.append(vector)
        ranked = rank(query_vector, eligible, vectors, threshold=threshold, limit=limit)
        metadata = self._metadata(phase=phase, threshold=threshold, limit=limit, rule_ref=rule_ref, query=query, ranked=ranked)
        return {"skills": [deepcopy(item["skill"]) for item in ranked], "bank_snapshot": metadata["bank_snapshot"], "query": metadata}

    def select_task(self, original: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.rules.enable_task:
            raise SidecarRetrievalError("frozen assignment arm disables task skill injection")
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


def _encoder(retrieval: dict[str, Any]) -> MiniLMEncoder:
    config = _mapping(retrieval.get("encoder"), "retrieval.encoder")
    if config.get("kind") != "minilm":
        raise SidecarRetrievalError("retrieval.encoder.kind must be minilm")
    return MiniLMEncoder(
        repo_id=_text(config.get("repoId"), "retrieval.encoder.repoId"),
        revision=_text(config.get("revision"), "retrieval.encoder.revision"),
    )
