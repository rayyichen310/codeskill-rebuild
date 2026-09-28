"""Bounded Task stages used by the durable graph, without driver phase logic."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .arm_banks import same_granularity_top5
from .bank import SkillBank, validate_skill_candidate
from .compaction import (
    EvidenceCompactionError, action_observation_segments, expand_evidence_fragments,
)
from .manager_projection import (
    HISTORICAL_THINKING_POLICY_VERSION, project_historical_thinking,
    validate_historical_thinking_policy,
)
from .pipeline import (
    description_messages, maintenance_from_skills_messages,
    validate_description, validate_maintenance_from_skills, validate_pairing, validate_budget_summary,
)
from .retrieval import MiniLMEncoder, cosine
from .segment_candidates import TASK_SEGMENT_SCHEMA, validate_segment_candidate
from .shared_segments import TASK_PROMPT, segment_messages, whole_original_segment
from .task_sop import (
    TASK_SOP_MERGE_SCHEMA, task_sop_merge_messages, task_sop_pairing_messages,
    validate_task_sop_merge,
)
from .task_graph_model import TaskCallResult, TaskChatBoundary
from .types import canonical_instance_id, canonical_json, sha256_file, sha256_text, write_json


GENERATION_SCHEMA = TASK_SEGMENT_SCHEMA
D01_SUMMARY_SCHEMA = {"name": "task_d01_summary_v1", "schema": {"type": "object",
    "properties": {"summary": {"type": "string"},
                   "covered_step_ids": {"type": "array", "items": {"type": "string"}},
                   "verbatim_evidence_step_ids": {"type": "array", "items": {"type": "string"}}},
    "required": ["summary", "covered_step_ids", "verbatim_evidence_step_ids"],
    "additionalProperties": False}}
DECISION_SCHEMA = {
    "name": "task_stage_decision_v1",
    "schema": {"type": "object", "additionalProperties": True},
}


def _hash(value: Any) -> str:
    return sha256_text(canonical_json(value))


METHOD_IDENTITY_FIELDS = (
    "historical_thinking_policy", "historical_thinking_policy_version",
    "graph_version", "model", "tokenizer_identity", "model_boundary",
    "schema_sha256", "prompt_sha256",
)


def _method_identity(identity: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(identity, dict) or any(field not in identity for field in METHOD_IDENTITY_FIELDS):
        raise ValueError("Task candidate method identity is incomplete")
    return {field: deepcopy(identity[field]) for field in METHOD_IDENTITY_FIELDS}


def _candidate_namespace(identity: dict[str, Any]) -> tuple[str, ...]:
    return ("task", "candidate", str(identity["round_id"]), _hash(_method_identity(identity)))


def _provenance(skill: dict[str, Any], source_ids: list[str]) -> dict[str, Any]:
    checked = validate_skill_candidate(skill)
    old = checked.get("provenance")
    checked["provenance"] = {
        "source_instance_ids": sorted({canonical_instance_id(item) for item in source_ids}),
        "source_instance_ids_raw": sorted(set(source_ids)),
        "parent_skill_ids": list(old.get("parent_skill_ids", [])) if isinstance(old, dict) else [],
    }
    return checked


class TaskGraphStages:
    """One real stage per graph node; every model call uses TaskChatBoundary."""

    graph_folder = "task-graph"

    def __init__(self, *, context: dict[str, Any], input_value: dict[str, Any],
                 trace: dict[str, Any], current_ref: dict[str, Any],
                 traces: list[tuple[str, dict[str, Any], dict[str, Any]]],
                 frozen_bank: SkillBank, chat: TaskChatBoundary,
                 artifact_root: Path, prompt_root: Path,
                 encoder: Any | None = None,
                 tokenizer_identity: dict[str, Any] | None = None,
                 require_durable_extraction: bool = False):
        self.context = context
        self.input_value = input_value
        self.trace = trace
        self.current_ref = current_ref
        self.traces = traces
        self.frozen_bank = frozen_bank
        self.chat = chat
        self.artifact_root = Path(artifact_root)
        self.prompt_root = Path(prompt_root)
        self.encoder = encoder
        self.tokenizer_identity = tokenizer_identity or {}
        self.require_durable_extraction = require_durable_extraction
        self.prepared_segments: list[dict[str, Any]] | None = None
        self.store: Any = None
        policy = validate_historical_thinking_policy(
            context.get("driver_config", {}).get("historical_thinking_policy", "keep")
        )
        self.policy = policy
        self.projected_trace = project_historical_thinking(trace, policy=policy)["manager_trace"]
        self.description_trace = self.projected_trace
        # Event may already have paid for a validated, source-cited compact
        # view in this trial. D01 can reuse that view; evidence selection still
        # scans every original visible action/result handle below.
        for cached in context.get("trajectory_summary_cache", {}).values():
            compacted = cached.get("trace") if isinstance(cached, dict) else None
            source_identity = cached.get("source_identity") if isinstance(cached, dict) else None
            if not isinstance(source_identity, dict) or \
                    source_identity.get("immutable_trace_sha256") != _hash(self.trace) or \
                    source_identity.get("historical_thinking_policy") != self.policy or \
                    not isinstance(compacted, dict) or \
                    compacted.get("source") != self.projected_trace.get("source"):
                continue
            retained = compacted.get("steps")
            original_ids = {step.get("source_entry_id") for step in self.projected_trace.get("steps", [])
                            if isinstance(step, dict)}
            if not isinstance(retained, list) or any(not isinstance(step, dict) or
                step.get("source_entry_id") not in original_ids for step in retained):
                continue
            self.description_trace = compacted
            break

    def prepare_segments(self) -> list[dict[str, Any]]:
        if self.prepared_segments is None:
            self.prepared_segments = [whole_original_segment(self.projected_trace,
                chat=self.chat, prompt=self._prompt(TASK_PROMPT), schema=TASK_SEGMENT_SCHEMA)]
        return self.prepared_segments

    def initial_state(self) -> dict[str, Any]:
        from .task_graph import task_thread_id
        identity = {"round_id": self.current_ref["round_id"],
                    "trial_id": self.current_ref["trial_id"],
                    "task_id": self.context["task_id"],
                    "trace_sha256": _hash(self.trace),
                    "historical_thinking_policy": self.policy,
                    "historical_thinking_policy_version": HISTORICAL_THINKING_POLICY_VERSION,
                    "graph_version": 2, "model": self.chat.model,
                    "tokenizer_identity": self.tokenizer_identity,
                    "model_boundary": {"base_url": self.chat.base_url,
                                       "output_tokens": self.chat.output_tokens,
                                       "allowance": self.chat.allowance},
                    "schema_sha256": {"d01_summary": _hash(D01_SUMMARY_SCHEMA),
                                      "generation": _hash(GENERATION_SCHEMA),
                                      "decision": _hash(DECISION_SCHEMA)},
                    "prompt_sha256": {name: sha256_file(self.prompt_root / name)
                                      for name in (
                                          "custom/m2_description.md",
                                          "custom/r015_task_description_summary.md",
                                          TASK_PROMPT,
                          "custom/r015_task_pairing_from_sops.md",
                          "custom/r015_task_merge_from_sops.md",
                                          "custom/r015_fig09_maintenance_preserve_code_examples.md")}}
        return {"schema_version": 2, "thread_id": task_thread_id(identity),
                "identity": identity, "source_ref": deepcopy(self.current_ref),
                "frozen_bank_hash": self.frozen_bank.snapshot()["state_sha256"],
                "outcomes": {}, "description_index": 0, "evidence_index": 0,
                "description_results": [], "description_records": [],
                "task_candidate_records": [], "generation_results": [],
                "merge_candidate": None,
                "verifier_status": "not_run"}

    def bind_store(self, store: Any) -> None:
        self.store = store

    def _prompt(self, name: str) -> str:
        return (self.prompt_root / name).read_text(encoding="utf-8")

    def _artifact(self, name: str, value: Any) -> dict[str, str]:
        path = self.artifact_root / getattr(self, "graph_folder", "task-graph") / name
        if path.exists():
            if json.loads(path.read_text(encoding="utf-8")) != value:
                raise ValueError(f"Task artifact changed across restart: {path}")
        else:
            write_json(path, value)
        return {"path": str(path), "sha256": sha256_file(path)}

    @staticmethod
    def _read(ref: dict[str, str]) -> Any:
        path = Path(ref["path"])
        if not path.is_file() or sha256_file(path) != ref["sha256"]:
            raise ValueError("Task graph artifact path/hash changed")
        return json.loads(path.read_text(encoding="utf-8"))

    def _call(self, state: dict[str, Any], stage: str,
              messages: list[dict[str, str]], schema: dict[str, Any]) -> TaskCallResult:
        return self.chat.call(thread_id=state["thread_id"], stage=stage,
                              messages=messages, schema=schema,
                              identity={**state["identity"], "stage": stage,
                                        "prompt_sha256": _hash(messages),
                                        "schema_sha256": _hash(schema)})

    def _verified_saved_call(self, ref: dict[str, Any], *, thread_id: str,
                             identity: dict[str, Any], stage: str,
                             messages: list[dict[str, str]],
                             schema: dict[str, Any]) -> dict[str, Any]:
        """Bind a complete saved response to the source-derived request, not its mutable hash."""
        call_key = ref["call_key"]
        call_dir = self.chat.root / "task-calls" / call_key
        for field, filename in (("request", "wire-request.json"),
                                ("response", "wire-response.json")):
            if Path(ref[field]["path"]) != call_dir / filename:
                raise ValueError(f"{stage} {field} is outside its call identity")
        wire_request = json.loads(self._read_bytes_ref(ref["request"]))
        wire_response = json.loads(self._read_bytes_ref(ref["response"]))
        requested = json.loads((call_dir / "identity.json").read_text(encoding="utf-8"))
        expected_identity = {**identity, "stage": stage,
                             "prompt_sha256": _hash(messages),
                             "schema_sha256": _hash(schema)}
        expected_request = {"thread_id": thread_id, "stage": stage,
            "messages": messages, "schema": schema, "identity": expected_identity,
            "model": self.chat.model, "base_url": self.chat.base_url,
            "temperature": 0, "reasoning_effort": "max",
            "max_tokens": self.chat.output_tokens}
        if requested != expected_request or _hash(requested) != call_key:
            raise ValueError(f"{stage} saved call identity differs from source-derived request")
        expected_wire = {"messages": messages, "model": self.chat.model,
            "temperature": 0, "reasoning_effort": "max",
            "max_tokens": self.chat.output_tokens, "stream": False,
            "response_format": {"type": "json_schema", "json_schema": schema}}
        if wire_request != expected_wire:
            raise ValueError(f"{stage} wire request differs from source-derived request")
        choices = wire_response.get("choices") if isinstance(wire_response, dict) else None
        if (not isinstance(choices, list) or len(choices) != 1 or
                choices[0].get("finish_reason") != "stop" or
                ref.get("finish_reason") != "stop"):
            raise ValueError(f"{stage} saved model response is incomplete")
        saved = TaskChatBoundary._read_result(call_key, call_dir)
        if saved.status != "ok" or saved.value != json.loads(choices[0]["message"]["content"]):
            raise ValueError(f"{stage} saved model response is invalid")
        return saved.value

    @staticmethod
    def _model_ref(call: TaskCallResult) -> dict[str, Any]:
        response = call.call_dir / "wire-response.json"
        request = call.call_dir / "wire-request.json"
        preflight = call.call_dir / "preflight.json"
        http = call.call_dir / "http.json"
        response_value = json.loads(response.read_text(encoding="utf-8")) if response.is_file() else {}
        preflight_value = json.loads(preflight.read_text(encoding="utf-8")) if preflight.is_file() else {}
        http_value = json.loads(http.read_text(encoding="utf-8")) if http.is_file() else {}
        choices = response_value.get("choices", []) if isinstance(response_value, dict) else []
        finish = choices[0].get("finish_reason") if choices and isinstance(choices[0], dict) else None
        return {"call_key": call.call_key,
                "request": {"path": str(request), "sha256": sha256_file(request)} if request.is_file() else None,
                "response": {"path": str(response), "sha256": sha256_file(response)} if response.is_file() else None,
                "preflight_input_tokens": preflight_value.get("input_tokens"),
                "usage": response_value.get("usage") if isinstance(response_value, dict) else None,
                "finish_reason": finish,
                "latency_ms": http_value.get("latency_ms"),
                "reason": call.reason}

    def _encoder(self) -> Any:
        if self.encoder is None:
            runtime = self.context["profile"]["runtime"]
            self.encoder = MiniLMEncoder(
                repo_id=str(runtime.get("minilm_repo_id", "sentence-transformers/all-MiniLM-L6-v2")),
                revision=str(runtime.get("minilm_revision")),
            )
            self.encoder.load()
        return self.encoder

    def _d01_summary_messages(self, steps: list[dict[str, Any]], *, final: bool) -> list[dict[str, str]]:
        return [{"role": "system", "content": self._prompt("custom/r015_task_description_summary.md")},
                {"role": "user", "content": canonical_json({
                    "task_context": self.projected_trace["instruction"],
                    "source": self.projected_trace["source"],
                    "outcome": self.projected_trace.get("outcome"),
                    "segment_steps": steps, "final_segment": final,
                    "required_covered_step_ids": [str(step["source_entry_id"]) for step in steps],
                })}]

    def d01_plan(self, state: dict[str, Any]) -> dict[str, Any]:
        """Use the full D01 request unless its exact preflight exceeds allowance."""
        if self.trace.get("text_manager_eligible") is not True:
            return {"description_segments": [], "outcome": {"status": "skip", "reason": "multimodal"}}
        options = {"model": self.chat.model, "temperature": 0,
                   "max_tokens": self.chat.output_tokens, "reasoning_effort": "max",
                   "response_format": {"type": "json_schema", "json_schema": DECISION_SCHEMA}}
        try:
            full_tokens = self.chat.token_counter(
                description_messages(self.description_trace,
                                     custom_prompt=self._prompt("custom/m2_description.md")),
                request_options=options)
        except Exception as error:
            return {"description_segments": [], "outcome": {"status": "context_blocked",
                "reason": f"D01 tokenizer failed: {type(error).__name__}: {error}"}}
        if full_tokens <= self.chat.allowance:
            return {"description_segments": [], "outcome": {"status": "ok",
                "mode": "full", "full_input_tokens": full_tokens}}
        summary_options = {**options,
            "response_format": {"type": "json_schema", "json_schema": D01_SUMMARY_SCHEMA}}

        def count(messages: list[dict[str, str]]) -> int:
            return self.chat.token_counter(messages, request_options=summary_options)

        try:
            segments = action_observation_segments(self.projected_trace["steps"],
                message_count=count,
                messages_for_segment=lambda steps: self._d01_summary_messages(steps, final=True),
                max_input_tokens=self.chat.allowance)
        except (EvidenceCompactionError, KeyError, TypeError, ValueError) as error:
            return {"description_segments": [], "outcome": {"status": "context_blocked",
                "reason": f"D01 source segmentation failed: {error}"}}
        return {"description_segments": segments, "description_index": 0,
            "description_results": [], "outcome": {"status": "ok", "mode": "segmented",
                "full_input_tokens": full_tokens, "segment_count": len(segments)}}

    def d01_segment(self, state: dict[str, Any]) -> dict[str, Any]:
        index = state["description_index"]
        segments = state["description_segments"]
        segment = segments[index]
        call = self._call(state, f"d01-summary-{index:03d}",
            self._d01_summary_messages(segment["steps"], final=index == len(segments) - 1),
            D01_SUMMARY_SCHEMA)
        model_ref = self._model_ref(call)
        if call.status != "ok":
            return {"outcome": {"status": call.status, "call": model_ref, "reason": call.reason}}
        try:
            summary = validate_budget_summary(call.value, segment_step_ids=segment["step_ids"])
            fragments = expand_evidence_fragments(segment["steps"],
                                                  summary["verbatim_evidence_step_ids"])
            if index == len(segments) - 1:
                final_result = next((str(step["source_entry_id"]) for step in reversed(segment["steps"])
                                     if step.get("role") == "toolResult"), None)
                if final_result is not None and final_result not in {
                        step["source_entry_id"] for step in fragments}:
                    raise ValueError("D01 final tool result lacks original cited evidence")
        except (ValueError, TypeError, EvidenceCompactionError) as error:
            return {"outcome": {"status": "rejected", "call": model_ref, "reason": str(error)}}
        ref = self._artifact(f"d01-summary-{index:03d}.json", {
            "summary": summary, "fragments": fragments,
            "segment_step_ids": segment["step_ids"], "call": model_ref,
            "source_trace_sha256": _hash(self.trace)})
        return {"description_index": index + 1,
            "description_results": [*state.get("description_results", []), ref],
            "outcome": {"status": "ok", "call": model_ref, "artifact": ref}}

    def d01_bundle(self, state: dict[str, Any]) -> dict[str, Any]:
        segments = state["description_segments"]
        parts = [self._read(ref) for ref in state.get("description_results", [])]
        try:
            if [step for segment in segments for step in segment["steps"]] != self.projected_trace["steps"]:
                raise ValueError("D01 segments differ from original projected source steps")
            for index, (part, segment) in enumerate(zip(parts, segments, strict=True)):
                call = part["call"]
                if segment["step_ids"] != [str(step["source_entry_id"]) for step in segment["steps"]]:
                    raise ValueError("D01 segment IDs differ from original source steps")
                saved = self._verified_saved_call(call,
                    thread_id=state["thread_id"], identity=state["identity"],
                    stage=f"d01-summary-{index:03d}",
                    messages=self._d01_summary_messages(segment["steps"],
                        final=index == len(segments) - 1), schema=D01_SUMMARY_SCHEMA)
                if validate_budget_summary(
                        saved, segment_step_ids=segment["step_ids"]) != part["summary"] or \
                        expand_evidence_fragments(segment["steps"],
                            part["summary"]["verbatim_evidence_step_ids"]) != part["fragments"]:
                    raise ValueError(f"D01 summary {index} differs from saved model response")
        except (KeyError, TypeError, ValueError, OSError, EvidenceCompactionError) as error:
            return {"outcome": {"status": "rejected", "reason": str(error)}}
        source_ids = [str(step["source_entry_id"]) for step in self.projected_trace["steps"]]
        covered = [step for part in parts for step in part["summary"]["covered_step_ids"]]
        if len(parts) != len(segments) or covered != source_ids or any(
                part["source_trace_sha256"] != _hash(self.trace) or
                part["segment_step_ids"] != segment["step_ids"]
                for part, segment in zip(parts, segments, strict=True)):
            return {"outcome": {"status": "rejected", "reason": "D01 summaries lack exact source coverage"}}
        selected = {str(step["source_entry_id"]) for part in parts for step in part["fragments"]}
        first_user = next((str(step["source_entry_id"]) for step in self.projected_trace["steps"]
                           if step.get("role") == "user"), None)
        if first_user is not None:
            selected.add(first_user)
        compacted = deepcopy(self.projected_trace)
        compacted["steps"] = [deepcopy(step) for step in self.projected_trace["steps"]
                              if str(step["source_entry_id"]) in selected]
        compacted.pop("entries", None)
        compacted["trajectory_input_mode"] = "evidence_compacted"
        compacted["evidence_summaries"] = [part["summary"] for part in parts]
        ref = self._artifact("d01-bundle.json", {
            "trace": compacted, "visible_step_ids": sorted(selected),
            "covered_step_ids": covered, "summary_refs": state["description_results"],
            "source_trace_sha256": _hash(self.trace),
            "historical_thinking_policy": self.policy,
            "method_sha256": _hash(_method_identity(state["identity"]))})
        return {"description_bundle": ref, "outcome": {"status": "ok",
            "summary_count": len(parts), "visible_step_count": len(selected), "artifact": ref}}

    def d01(self, state: dict[str, Any]) -> dict[str, Any]:
        if self.trace.get("text_manager_eligible") is not True:
            return {"description_records": [], "outcome": {"status": "skip", "reason": "multimodal"}}
        description_trace = self.description_trace
        visible_step_ids = None
        if state.get("description_bundle"):
            bundle = self._read(state["description_bundle"])
            if bundle["source_trace_sha256"] != _hash(self.trace) or \
                    bundle["historical_thinking_policy"] != self.policy or \
                    bundle["method_sha256"] != _hash(_method_identity(state["identity"])):
                return {"description_records": [], "outcome": {"status": "rejected",
                    "reason": "D01 context bundle changed source or method"}}
            description_trace = bundle["trace"]
            visible_step_ids = set(bundle["visible_step_ids"])
        messages = description_messages(description_trace,
                                        custom_prompt=self._prompt("custom/m2_description.md"))
        call = self._call(state, "d01", messages, DECISION_SCHEMA)
        if call.status != "ok":
            return {"description_records": [], "outcome": {"status": call.status,
                    "call": self._model_ref(call), "reason": call.reason}}
        try:
            checked = validate_description(call.value, self.trace)
            if visible_step_ids is not None and not set(checked["source_step_ids"]) <= visible_step_ids:
                raise ValueError("D01 description cites a source step absent from its compacted input")
        except (TypeError, ValueError) as error:
            return {"description_records": [], "outcome": {"status": "rejected",
                    "call": self._model_ref(call), "reason": str(error)}}
        response = self._model_ref(call)["response"]
        record = {"task_id": state["identity"]["task_id"],
                  "round_id": state["identity"]["round_id"],
                  "path": response["path"], "sha256": response["sha256"],
                  "response_sha256": response["sha256"], "value": checked,
                  "value_sha256": _hash(checked),
                  "trajectory_ref": deepcopy(self.current_ref),
                  "task_graph_call": self._model_ref(call)}
        return {"description_records": [record], "outcome": {"status": "ok",
                "call": self._model_ref(call)}}

    def evidence_plan(self, state: dict[str, Any]) -> dict[str, Any]:
        if self.trace.get("text_manager_eligible") is not True:
            return {"evidence_segments": [], "outcome": {"status": "skip", "reason": "multimodal"}}
        try:
            segments = self.prepare_segments()
        except (ValueError, KeyError, TypeError) as error:
            return {"evidence_segments": [], "outcome": {
                "status": "context_blocked", "reason": str(error)}}
        return {"evidence_segments": segments, "evidence_index": 0,
                "generation_results": [], "outcome": {
                    "status": "ok", "segment_count": len(segments)}}

    def _generation_messages(self, steps: list[dict[str, Any]]) -> list[dict[str, str]]:
        return segment_messages(self.projected_trace, steps, self._prompt(TASK_PROMPT))

    def generate(self, state: dict[str, Any]) -> dict[str, Any]:
        index = state["evidence_index"]
        segment = state["evidence_segments"][index]
        call = self._call(state, f"generation-{index:03d}",
                          self._generation_messages(segment["steps"]), GENERATION_SCHEMA)
        model_ref = self._model_ref(call)
        result: dict[str, Any] = {"segment_index": index, "status": call.status,
                                  "call": model_ref, "reason": call.reason}
        if call.status != "ok":
            return {"generation_results": [*state["generation_results"], result],
                    "outcome": {"status": call.status, "call": model_ref, "reason": call.reason}}
        try:
            checked = validate_segment_candidate(call.value, kind="task", trace=self.trace,
                visible_ids=[str(step["source_entry_id"]) for step in segment["steps"]])
        except (TypeError, ValueError) as error:
            result.update(status="rejected", reason=str(error))
            return {"generation_results": [*state["generation_results"], result],
                    "outcome": {"status": "rejected", "reason": str(error)}}
        if checked["action"] == "skip":
            result.update(status="skip", reason=checked["reason"])
            return {"generation_results": [*state["generation_results"], result],
                    "outcome": {"status": "skip", "reason": checked["reason"]}}
        ref = self._artifact(f"generation-{index:03d}.json", {
            "raw_output": call.value, "checked": checked, "call": model_ref,
            "segment_index": index})
        result.update(status="validated", generation=ref, reason=None)
        return {"generation_results": [*state["generation_results"], result],
                "outcome": {"status": "ok", "artifact": ref}}

    def save_candidate(self, state: dict[str, Any]) -> dict[str, Any]:
        result = state["generation_results"][-1]
        generation = self._read(result["generation"])
        checked = generation["checked"]
        source_id = str(state["identity"]["task_id"])
        skill = _provenance(checked["skill"], [source_id])
        fingerprint = _hash(skill)
        index = result["segment_index"]
        record = {
            "candidate_id": f"task-sop-{index:03d}-{fingerprint[:12]}",
            "candidate_fingerprint": fingerprint,
            "status": "validated", "source": "current_round_c_only",
            "round_id": state["identity"]["round_id"],
            "task_id": source_id, "trial_id": state["identity"]["trial_id"],
            "session_id": self.current_ref["session_id"],
            "trajectory_ref": deepcopy(self.current_ref), "skill": skill,
            "candidate_context": deepcopy(checked["candidate_context"]),
            "evidence": deepcopy(checked["evidence"]),
            "official_task_outcome": deepcopy(self.trace.get("outcome")),
            "raw": {"kind": "task_graph_candidate_v2", "task_graph": {
                "generation": result["generation"], "call": generation["call"],
                "segment_index": index, "producing_identity": deepcopy(state["identity"])}},
        }
        if self.store is None:
            raise RuntimeError("Task graph Store was not bound")
        namespace = _candidate_namespace(state["identity"])
        existing = self.store.get(namespace, record["candidate_id"])
        if existing is not None and existing.value != record:
            raise ValueError("Task candidate Store identity conflict")
        if existing is None:
            self.store.put(namespace, record["candidate_id"], record)
        saved = {**result, "status": "generated", "candidate_id": record["candidate_id"]}
        return {"task_candidate_records": [*state["task_candidate_records"], record],
                "candidate": state.get("candidate") or record,
                "generation_results": [*state["generation_results"][:-1], saved],
                "evidence_index": index + 1,
                "outcome": {"status": "ok", "candidate_id": record["candidate_id"]}}

    def advance(self, state: dict[str, Any]) -> dict[str, Any]:
        return {"evidence_index": state["evidence_index"] + 1,
                "outcome": {"status": "ok"}}

    def _verified_candidate(self, record: dict[str, Any], trace: dict[str, Any],
                            ref: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
        from .task_graph import task_thread_id

        if record.get("status") != "validated" or record.get("source") != "current_round_c_only":
            raise ValueError("Task pool contains an unvalidated candidate")
        if record.get("round_id") != identity["round_id"] or record.get("trajectory_ref") != ref:
            raise ValueError("Task pool candidate is outside its source round/trajectory")
        raw = record.get("raw", {})
        graph = raw.get("task_graph") if isinstance(raw, dict) else None
        if not isinstance(graph, dict) or raw.get("kind") != "task_graph_candidate_v2":
            raise ValueError("Task pool candidate lacks a current graph derivation")
        produced = graph["producing_identity"]
        if _method_identity(produced) != _method_identity(identity):
            raise ValueError("Task pool candidate changed producing method identity")
        if (produced["trace_sha256"] != _hash(trace) or
                produced["task_id"] != record.get("task_id") or
                produced["trial_id"] != record.get("trial_id") or
                record.get("session_id") != ref.get("session_id")):
            raise ValueError("Task pool candidate changed producing source identity")
        projected = project_historical_thinking(
            trace, policy=produced["historical_thinking_policy"])["manager_trace"]
        segments = [whole_original_segment(projected, chat=self.chat,
            prompt=self._prompt(TASK_PROMPT), schema=TASK_SEGMENT_SCHEMA)]
        index = graph["segment_index"]
        if type(index) is not int or not 0 <= index < len(segments):
            raise ValueError("Task pool candidate segment is absent")
        segment = segments[index]
        generation = self._read(graph["generation"])
        messages = segment_messages(projected, segment["steps"], self._prompt(TASK_PROMPT))
        saved = self._verified_saved_call(graph["call"],
            thread_id=task_thread_id(produced), identity=produced,
            stage=f"generation-{index:03d}", messages=messages, schema=GENERATION_SCHEMA)
        checked = validate_segment_candidate(saved, kind="task", trace=trace,
            visible_ids=[str(step["source_entry_id"]) for step in segment["steps"]])
        expected = _provenance(checked["skill"], [record["task_id"]])
        if (saved != generation["raw_output"] or checked != generation["checked"] or
                generation["segment_index"] != index or generation["call"] != graph["call"] or
                record["skill"] != expected or
                record["candidate_fingerprint"] != _hash(expected) or
                record["candidate_context"] != checked["candidate_context"] or
                record["evidence"] != checked["evidence"] or
                record["official_task_outcome"] != trace.get("outcome")):
            raise ValueError("Task pool candidate differs from its saved source response")
        return record

    @staticmethod
    def _read_bytes_ref(ref: dict[str, str]) -> bytes:
        path = Path(ref["path"])
        if not path.is_file():
            raise ValueError("Task call artifact is absent")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != ref["sha256"]:
            raise ValueError("Task call artifact hash changed")
        return data

    def _sources(self, state: dict[str, Any]) -> dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
        current_id = str(state["identity"]["task_id"])
        trace_by_id = {task_id: (trace, ref) for task_id, trace, ref in self.traces}
        current_trace, current_ref = trace_by_id[current_id]
        current = self._verified_candidate(state["candidate"], current_trace,
                                           current_ref, state["identity"])
        sources = {current_id: (current, current_trace, current_ref)}
        material = self.input_value.get("round_material", {})
        pool = material.get("task_candidate_pool", [])
        if not isinstance(pool, list):
            raise ValueError("same-round Task candidate pool is not a list")
        state_path = Path(self.input_value["state"]["path"])
        durable = json.loads(state_path.read_text(encoding="utf-8"))
        round_state = durable["rounds"][str(state["identity"]["round_id"])]
        durable_pool = [item for item in round_state["task_candidate_pool"]
                        if not isinstance(item, dict) or item.get("task_id") != current_id]
        if canonical_json(pool) != canonical_json(durable_pool):
            raise ValueError("Task pool differs from prior durable round state")
        completed = round_state["completed_tasks"]
        namespace = _candidate_namespace(state["identity"])
        for record in pool:
            if not isinstance(record, dict):
                raise ValueError("Task pool contains a non-object")
            source_id = canonical_instance_id(str(record.get("task_id", "")))
            if source_id == current_id or source_id in sources:
                raise ValueError("Task pool contains current/duplicate source")
            if source_id not in completed or completed[source_id].get("outcome") != "completed":
                raise ValueError("Task pool source is not an earlier completed task")
            if source_id not in trace_by_id:
                raise ValueError("Task pool source trace is unavailable")
            trace, ref = trace_by_id[source_id]
            checked = self._verified_candidate(record, trace, ref, state["identity"])
            stored = self.store.get(namespace, checked["candidate_id"])
            if stored is None or stored.value != checked:
                raise ValueError("Task Store candidate differs from protocol pool")
            sources[source_id] = (checked, trace, ref)
        return sources

    def rank(self, state: dict[str, Any]) -> dict[str, Any]:
        try:
            sources = self._sources(state)
            current_id = str(state["identity"]["task_id"])
            prior = [(source_id, value) for source_id, value in sources.items()
                     if source_id != current_id]
            if not prior:
                return {"ranked": [], "outcome": {"status": "no_eligible_sources"}}
            encoder = self._encoder()
            anchor = sources[current_id][0]
            anchor_vector, anchor_index = encoder.index_skill(anchor["skill"])
            ranked = []
            for source_id, (record, trace, ref) in prior:
                vector, index = encoder.index_skill(record["skill"])
                ranked.append({"task_id": source_id,
                               "score": cosine(anchor_vector, vector),
                               "candidate_id": record["candidate_id"],
                               "candidate_fingerprint": record["candidate_fingerprint"],
                               "skill": record["skill"],
                               "candidate_context": record["candidate_context"],
                               "official_task_outcome": record["official_task_outcome"],
                               "evidence": record["evidence"],
                               "trajectory_ref": ref, "index": index})
            ranked.sort(key=lambda item: (-float(item["score"]), str(item["task_id"])))
            ranked = ranked[:12]
            ranking = {"kind": "r015_c_only_d02_minilm_task_candidate_ranking",
                       "encoder": {"repo_id": getattr(encoder, "repo_id", None),
                                   "resolved_revision": getattr(encoder, "resolved_revision", None)},
                       "max_candidates": 12,
                       "anchor": {"task_id": current_id, "candidate_id": anchor["candidate_id"],
                                  "candidate_fingerprint": anchor["candidate_fingerprint"],
                                  "trajectory_ref": self.current_ref, "index": anchor_index},
                       "ranked_candidates": ranked}
            ref = self._artifact("ranking.json", ranking)
            return {"ranked": ranked, "ranking_ref": ref,
                    "outcome": {"status": "ok", "artifact": ref}}
        except Exception as error:
            return {"outcome": {"status": "infra_blocked", "reason": str(error)}}

    def pair(self, state: dict[str, Any]) -> dict[str, Any]:
        current_id = str(state["identity"]["task_id"])
        anchor = state["candidate"]
        ranked = state["ranked"]
        anchor_payload = {"canonical_instance_id": current_id, **anchor}
        candidates = [{"canonical_instance_id": item["task_id"], **item}
                      for item in ranked]
        messages = task_sop_pairing_messages(anchor_payload, candidates,
            prompt=self._prompt("custom/r015_task_pairing_from_sops.md"))
        call = self._call(state, "d02_pairing", messages, DECISION_SCHEMA)
        if call.status != "ok":
            return {"outcome": {"status": call.status, "call": self._model_ref(call),
                                "reason": call.reason}}
        try:
            checked = validate_pairing(call.value, anchor_id=current_id,
                                       candidate_ids={item["task_id"] for item in ranked},
                                       require_shared_evidence=True)
        except (TypeError, ValueError) as error:
            return {"outcome": {"status": "rejected", "call": self._model_ref(call),
                                "reason": str(error)}}
        if checked["action"] == "no_related_group":
            return {"outcome": {"status": "no_related_group", "call": self._model_ref(call)}}
        selected_ids = [str(item) for item in checked["selected_instance_ids"]]
        sources = self._sources(state)
        ordered_ids = [current_id] + [item for item in selected_ids if item != current_id]
        if len(set(ordered_ids)) not in {2, 3}:
            return {"outcome": {"status": "rejected", "reason": "D02 did not choose 2-3 distinct instances"}}
        group_hash = _hash(sorted(sources[item][0]["candidate_fingerprint"] for item in ordered_ids))
        group_id = f"d02-{group_hash[:20]}"
        state_path = Path(self.input_value["state"]["path"])
        durable = json.loads(state_path.read_text(encoding="utf-8"))
        round_state = durable["rounds"][str(state["identity"]["round_id"])]
        for assignment in round_state["assignments"].values():
            previous = assignment.get("extraction", {}).get("evidence", {}).get("task", {})
            if previous.get("group_id") == group_id:
                return {"outcome": {"status": "duplicate", "group_id": group_id,
                                    "call": self._model_ref(call)}}
        pairing = {"value": checked, "call": self._model_ref(call),
                   "source_task_ids": ordered_ids, "group_id": group_id,
                   "ranking_ref": state["ranking_ref"]}
        ref = self._artifact("pairing.json", pairing)
        return {"pairing": pairing, "pairing_ref": ref,
                "outcome": {"status": "ok", "call": self._model_ref(call),
                            "artifact": ref, "group_id": group_id}}

    def merge(self, state: dict[str, Any]) -> dict[str, Any]:
        sources = self._sources(state)
        pairing = state["pairing"]
        ordered = [(task_id, *sources[task_id]) for task_id in pairing["source_task_ids"]]
        merge_candidates = [{"canonical_instance_id": task_id,
                             **record}
                            for task_id, record, _trace, _ref in ordered]
        messages = task_sop_merge_messages(merge_candidates,
            prompt=self._prompt("custom/r015_task_merge_from_sops.md"))
        call = self._call(state, f"fig6_{pairing['group_id']}", messages, TASK_SOP_MERGE_SCHEMA)
        if call.status != "ok":
            return {"outcome": {"status": call.status, "call": self._model_ref(call),
                                "reason": call.reason}}
        try:
            checked = validate_task_sop_merge(call.value, merge_candidates)
        except (TypeError, ValueError) as error:
            return {"outcome": {"status": "rejected", "call": self._model_ref(call),
                                "reason": str(error)}}
        if checked["action"] == "skip":
            return {"outcome": {"status": "skip", "call": self._model_ref(call)}}
        by_candidate_id = {record["candidate_id"]: (task_id, record, ref)
                           for task_id, record, _trace, ref in ordered}
        cited_sources = [by_candidate_id[item]
                         for item in checked["evidence"]["source_candidate_ids"]]
        skill = _provenance(checked["skill"], [task_id for task_id, _record, _ref in cited_sources])
        fingerprint = _hash(skill)
        source_ids = [task_id for task_id, _record, _trace, _ref in ordered]
        candidate = {"candidate_id": f"task-{fingerprint[:16]}", "skill": skill,
                     "evidence": deepcopy(checked["evidence"]),
                     "pairing": {"source_task_ids": source_ids,
                                 "source_candidate_ids": [record["candidate_id"] for _, record, _, _ in ordered],
                                 "source_candidate_fingerprints": [record["candidate_fingerprint"] for _, record, _, _ in ordered],
                                 "trajectory_refs": [ref for _, _, _, ref in ordered],
                                 "group_id": pairing["group_id"],
                                 "d02_pairing": pairing["value"]},
                     "raw": {"kind": "task_graph_sop_merge_v1",
                             "call": self._model_ref(call),
                             "model_output": call.value,
                             "historical_thinking_policy": state["identity"]["historical_thinking_policy"],
                             "source_sop_evidence": [{
                                 "task_id": task_id,
                                 "candidate_id": record["candidate_id"],
                                 "candidate_fingerprint": record["candidate_fingerprint"],
                                 "evidence": deepcopy(record["evidence"]),
                                 "trajectory_ref": deepcopy(ref),
                                 "generation": deepcopy(record["raw"]),
                             } for task_id, record, ref in cited_sources]}}
        ref = self._artifact("merge.json", {"candidate": candidate, "checked": checked})
        return {"merge_candidate": candidate, "merge_ref": ref,
                "outcome": {"status": "ok", "candidate_fingerprint": fingerprint,
                            "artifact": ref, "call": self._model_ref(call),
                            "group_id": pairing["group_id"]}}

    @staticmethod
    def _replay(bank: SkillBank, operations: list[dict[str, Any]]) -> None:
        seen: set[str] = set()
        for operation in operations:
            operation_id = operation["operation_id"]
            if operation_id in seen:
                raise ValueError("staged operations repeat an operation ID")
            seen.add(operation_id)
            bank.apply(operation_id=operation_id, decision=operation["decision"],
                       candidate=operation["candidate"],
                       source_instance_ids=operation["source_instance_ids"],
                       merge_target_id=operation.get("merge_target_id"),
                       evidence=operation["evidence"])

    def check_event_receipt(self, state: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(receipt, dict) or receipt.get("thread_id") != state["thread_id"]:
            raise ValueError("Event receipt is for a different Task thread")
        h0 = self.frozen_bank.snapshot()["state_sha256"]
        if state["frozen_bank_hash"] != h0 or receipt.get("H0") != h0:
            raise ValueError("Event receipt H0 differs from frozen bank")
        operations = receipt.get("event_operations")
        if not isinstance(operations, list) or any(not isinstance(op, dict) for op in operations):
            raise ValueError("Event receipt needs an ordered operation list")
        if any(op.get("source_kind") != "extraction" for op in operations):
            raise ValueError("Event receipt contains a non-extraction operation")
        staged = SkillBank.from_dict(self.frozen_bank.to_dict())
        self._replay(staged, operations)
        he = staged.snapshot()["state_sha256"]
        if receipt.get("HE") != he:
            raise ValueError("Event staged bank hash differs from replay")
        extraction_ref = receipt.get("extraction_ref")
        if self.require_durable_extraction:
            if not isinstance(extraction_ref, dict):
                raise ValueError("Event handoff lacks durable protocol extraction receipt")
            state_path = Path(extraction_ref.get("durable_state_path", ""))
            if not state_path.is_file() or sha256_file(state_path) != extraction_ref.get("durable_state_sha256"):
                raise ValueError("durable protocol extraction state changed")
            ack_path = Path(extraction_ref.get("ack_path", ""))
            if not ack_path.is_file() or sha256_file(ack_path) != extraction_ref.get("ack_sha256"):
                raise ValueError("coordinator extraction acknowledgement changed")
            durable = json.loads(state_path.read_text(encoding="utf-8"))
            assignment = durable["rounds"][str(state["identity"]["round_id"])]["assignments"][state["identity"]["task_id"]]
            marker = assignment.get("extraction", {}).get("evidence", {}).get("task", {}).get("graph")
            if not isinstance(marker, dict) or marker.get("thread_id") != state["thread_id"]:
                raise ValueError("durable protocol extraction differs from Task thread")
        bank_ref = self._artifact("event-staged-bank.json", staged.to_dict())
        checked = {"thread_id": state["thread_id"], "H0": h0, "HE": he,
                   "event_operations": deepcopy(operations), "event_bank": bank_ref,
                   "extraction_ref": receipt.get("extraction_ref")}
        return {"receipt": checked, "outcome": {"status": "ok",
                "event_operation_count": len(operations)}}

    def fig9(self, state: dict[str, Any]) -> dict[str, Any]:
        event = state["event_receipt"]
        bank = SkillBank.from_dict(self._read(event["event_bank"]))
        candidate_wrapper = state.get("merge_candidate")
        if candidate_wrapper is None:
            bank_ref = self._artifact("task-staged-bank.json", bank.to_dict())
            receipt = {"thread_id": state["thread_id"], "H0": event["H0"],
                       "HE": event["HE"], "HT": event["HE"],
                       "event_operations": event["event_operations"],
                       "task_operations": [], "task_bank": bank_ref}
            return {"task_receipt": receipt,
                    "outcome": {"status": "no_op", "task_operation_count": 0}}
        candidate = candidate_wrapper["skill"]
        original_fingerprint = _hash(candidate)
        encoder = self._encoder()
        retrieved, retrieval = same_granularity_top5(bank, candidate, encoder)
        prompt_candidate = {key: candidate[key] for key in (
            "title", "granularity", "when_to_apply", "rules", "benchmark", "code_examples")
            if key in candidate}
        messages = maintenance_from_skills_messages(
            prompt_candidate, retrieved,
            paper_prompt=self._prompt("custom/r015_fig09_maintenance_preserve_code_examples.md"),
        )
        ordinal = len(event["event_operations"]) + 1
        operation_id = "r015-c-only-extraction-" + _hash({
            "trial_id": state["identity"]["trial_id"],
            "candidate_id": candidate_wrapper["candidate_id"],
            "ordinal": ordinal})[:20]
        call = self._call(state, f"fig9_task_{ordinal:03d}", messages, DECISION_SCHEMA)
        if call.status != "ok":
            return {"outcome": {"status": call.status, "call": self._model_ref(call),
                                "reason": call.reason}}
        try:
            checked = validate_maintenance_from_skills(
                call.value, candidate=candidate,
                retrieved_skill_ids={str(item["skill_id"]) for item in retrieved},
                retrieved_skills=retrieved,
            )
        except (TypeError, ValueError) as error:
            return {"outcome": {"status": "rejected", "call": self._model_ref(call),
                                "reason": str(error)}}
        candidate_for_operation = deepcopy(checked.get("skill", candidate))
        candidate_for_operation["provenance"] = deepcopy(candidate.get("provenance", {}))
        source_ids = list(candidate["provenance"]["source_instance_ids"])
        target_id = checked.get("merge_target_skill_id")
        target = None
        if checked["action"] == "merge":
            target = deepcopy(bank._find_active(target_id))
            provenance = candidate_for_operation["provenance"]
            other = target.get("provenance", {})
            source_ids = sorted({*source_ids, *other.get("source_instance_ids", [])})
            provenance["source_instance_ids"] = source_ids
            provenance["source_instance_ids_raw"] = sorted({
                *provenance.get("source_instance_ids_raw", source_ids),
                *other.get("source_instance_ids_raw", other.get("source_instance_ids", []))})
            provenance["parent_skill_ids"] = sorted({
                *provenance.get("parent_skill_ids", []),
                *other.get("parent_skill_ids", []), str(target["skill_id"])})
        response = self._model_ref(call)["response"]
        evidence = {"kind": "r015_c_only_fig9_manager_evidence",
                    "retrieval": retrieval, "manager_call_id": call.call_key,
                    "task_graph_call": self._model_ref(call),
                    "decision_reason": checked.get("reason"),
                    "decision_references": deepcopy(checked["evidence"]),
                    "extraction_candidate_id": candidate_wrapper["candidate_id"],
                    "extraction_candidate_fingerprint": original_fingerprint,
                    "extraction_source": deepcopy(candidate_wrapper.get("raw")),
                    "manager_response_sha256": response["sha256"],
                    "manager_response_path": response["path"],
                    "fig9_response_sha256": response["sha256"],
                    "fig9_response_path": response["path"],
                    "original_candidate_fingerprint": original_fingerprint,
                    "merged_candidate_fingerprint": _hash(candidate_for_operation),
                    "merge_target_skill_fingerprint": _hash(target) if target else None,
                    "merge_target_skill": target}
        try:
            applied = bank.apply(operation_id=operation_id, decision=checked["action"],
                                 candidate=candidate_for_operation,
                                 source_instance_ids=source_ids,
                                 merge_target_id=target_id, evidence=evidence)
        except (TypeError, ValueError) as error:
            return {"outcome": {"status": "rejected", "reason": str(error),
                                "call": self._model_ref(call)}}
        operation = {"operation_id": operation_id,
                     "original_candidate": deepcopy(candidate),
                     "original_candidate_fingerprint": original_fingerprint,
                     "candidate": candidate_for_operation,
                     "decision": checked["action"], "merge_target_id": target_id,
                     "source_instance_ids": source_ids, "evidence": evidence,
                     "manager_response": response, "applied_preview": applied,
                     "source_kind": "extraction",
                     "candidate_id": candidate_wrapper["candidate_id"]}
        bank_ref = self._artifact("task-staged-bank.json", bank.to_dict())
        receipt = {"thread_id": state["thread_id"], "H0": event["H0"],
                   "HE": event["HE"], "HT": bank.snapshot()["state_sha256"],
                   "event_operations": event["event_operations"],
                   "task_operations": [operation], "task_bank": bank_ref}
        return {"task_receipt": receipt,
                "outcome": {"status": "ok", "call": self._model_ref(call),
                            "task_operation_count": 1}}

    def check_publication_receipt(self, state: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
        task = state["task_receipt"]
        if not isinstance(receipt, dict) or receipt.get("thread_id") != state["thread_id"]:
            raise ValueError("publication receipt is for a different Task thread")
        for field in ("H0", "HE", "HT"):
            if receipt.get(field) != task[field]:
                raise ValueError(f"publication receipt {field} differs from staged Task")
        maintenance = receipt.get("maintenance_operations")
        if not isinstance(maintenance, list):
            raise ValueError("publication receipt needs ordered maintenance operations")
        bank = SkillBank.from_dict(self._read(task["task_bank"]))
        self._replay(bank, maintenance)
        hm = bank.snapshot()["state_sha256"]
        if receipt.get("HM") != hm:
            raise ValueError("publication receipt final hash differs from replay")
        expected_ids = [op["operation_id"] for op in [
            *task["event_operations"], *task["task_operations"], *maintenance]]
        if receipt.get("ordered_operation_ids") != expected_ids:
            raise ValueError("publication receipt operation order differs")
        state_path = Path(receipt.get("durable_state_path", ""))
        if not state_path.is_file():
            raise ValueError("publication state was not durably saved")
        durable = json.loads(state_path.read_text(encoding="utf-8"))
        round_state = durable["rounds"][str(state["identity"]["round_id"])]
        assignment = round_state["assignments"][state["identity"]["task_id"]]
        published = assignment["publication"]
        if published["before_bank_state_sha256"] != task["H0"] or published["after_bank_state_sha256"] != hm:
            raise ValueError("durable publication before/after hash differs")
        actual_ids = [op["operation_id"] for op in published["operations"]]
        if actual_ids != expected_ids:
            raise ValueError("durable publication operation order differs")
        expected_raw_operations = [*task["event_operations"], *task["task_operations"], *maintenance]
        publication_id = f"publish:r{state['identity']['round_id']}:{state['identity']['task_id']}"
        journal = [item for item in round_state["operations"]
                   if item.get("operation_id") == publication_id]
        expected_payload = {"round_id": state["identity"]["round_id"],
                            "task_id": state["identity"]["task_id"],
                            "operations": expected_raw_operations,
                            "manager_decisions": published["manager_decisions"]}
        if len(journal) != 1 or journal[0].get("payload_sha256") != _hash(expected_payload) or \
                journal[0].get("result") != published:
            raise ValueError("durable publication journal differs from staged operations")
        durable_bank = SkillBank.from_dict(round_state["bank"])
        if durable_bank.snapshot()["state_sha256"] != hm:
            raise ValueError("durable bank differs from staged replay")
        return {"receipt": {"thread_id": state["thread_id"], "H0": task["H0"],
                            "HE": task["HE"], "HT": task["HT"], "HM": hm,
                            "ordered_operation_ids": expected_ids,
                            "durable_state_path": str(state_path),
                            "durable_state_sha256": sha256_file(state_path)},
                "outcome": {"status": "ok", "operation_count": len(expected_ids)}}


class PublicationReceiptService(TaskGraphStages):
    """Resume only the already-persisted final handoff in the coordinator."""

    def __init__(self) -> None:
        # Earlier Task nodes cannot be reached from the verified second
        # interrupt. Their implementation remains in TaskGraphStages.
        pass
