"""One direct Event extraction call per shared original segment."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .manager_projection import HISTORICAL_THINKING_POLICY_VERSION
from .segment_candidates import EVENT_SEGMENT_SCHEMA, validate_segment_candidate
from .shared_segments import EVENT_PROMPT, segment_messages, prepare_original_segments
from .task_graph_stages import TaskGraphStages, _hash, _method_identity, _provenance
from .types import sha256_file


def _event_candidate_namespace(identity: dict[str, Any]) -> tuple[str, ...]:
    return ("event", "candidate", str(identity["round_id"]),
            _hash({**_method_identity(identity), "event_settings": identity["event_settings"]}))


class EventGraphStages(TaskGraphStages):
    graph_folder = "event-graph"

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.evidence_input_token_limit = self.chat.allowance

    def prepare_segments(self) -> list[dict[str, Any]]:
        if self.prepared_segments is None:
            self.prepared_segments = prepare_original_segments(self.projected_trace,
                chat=self.chat, prompt=self._prompt(EVENT_PROMPT),
                schema=EVENT_SEGMENT_SCHEMA)
        return self.prepared_segments

    def initial_state(self) -> dict[str, Any]:
        from .event_graph import event_thread_id

        identity = {"round_id": self.current_ref["round_id"],
            "trial_id": self.current_ref["trial_id"], "task_id": self.context["task_id"],
            "trace_sha256": _hash(self.trace), "kind": "event",
            "historical_thinking_policy": self.policy,
            "historical_thinking_policy_version": HISTORICAL_THINKING_POLICY_VERSION,
            "graph_version": 3, "model": self.chat.model,
            "tokenizer_identity": self.tokenizer_identity,
            "model_boundary": {"base_url": self.chat.base_url,
                               "output_tokens": self.chat.output_tokens,
                               "allowance": self.chat.allowance},
            "event_settings": {"input_allowance_tokens": self.evidence_input_token_limit},
            "schema_sha256": {"generation": _hash(EVENT_SEGMENT_SCHEMA)},
            "prompt_sha256": {EVENT_PROMPT: sha256_file(self.prompt_root / EVENT_PROMPT)}}
        return {"schema_version": 3, "thread_id": event_thread_id(identity),
                "identity": identity, "source_ref": deepcopy(self.current_ref),
                "outcomes": {}, "evidence_index": 0,
                "event_results": [], "event_candidates": [],
                "verifier_status": "not_run"}

    def _generation_messages(self, steps: list[dict[str, Any]]) -> list[dict[str, str]]:
        return segment_messages(self.projected_trace, steps, self._prompt(EVENT_PROMPT))

    def generate(self, state: dict[str, Any]) -> dict[str, Any]:
        index = state["evidence_index"]
        segment = state["evidence_segments"][index]
        call = self._call(state, f"event-generate-{index:03d}",
                          self._generation_messages(segment["steps"]), EVENT_SEGMENT_SCHEMA)
        model_ref = self._model_ref(call)
        result: dict[str, Any] = {"event_id": f"event-{index:03d}",
                                  "event_index": index, "status": call.status,
                                  "call": model_ref, "reason": call.reason}
        if call.status != "ok":
            return {"event_results": [*state["event_results"], result],
                    "outcome": {"status": call.status, "call": model_ref, "reason": call.reason}}
        try:
            checked = validate_segment_candidate(call.value, kind="event", trace=self.trace,
                visible_ids=[str(step["source_entry_id"]) for step in segment["steps"]])
        except (TypeError, ValueError) as error:
            result.update(status="rejected", reason=str(error))
            return {"event_results": [*state["event_results"], result],
                    "outcome": {"status": "rejected", "reason": str(error)}}
        if checked["action"] == "skip":
            result.update(status="skip", reason=checked["reason"])
            return {"event_results": [*state["event_results"], result],
                    "outcome": {"status": "skip", "reason": checked["reason"]}}
        ref = self._artifact(f"generation-{index:03d}.json", {
            "raw_output": call.value, "checked": checked, "call": model_ref,
            "segment_index": index})
        result.update(status="validated", generation=ref, reason=None)
        return {"event_results": [*state["event_results"], result],
                "outcome": {"status": "ok", "artifact": ref}}

    def save_candidate(self, state: dict[str, Any]) -> dict[str, Any]:
        result = state["event_results"][-1]
        generation = self._read(result["generation"])
        skill = _provenance(generation["checked"]["skill"], [str(state["identity"]["task_id"])])
        fingerprint = _hash(skill)
        candidate_id = f"event-{result['event_index']:03d}-{fingerprint[:12]}"
        wrapper = {"candidate_id": candidate_id, "candidate_fingerprint": fingerprint,
            "skill": skill, "evidence": deepcopy(generation["checked"]["evidence"]),
            "raw": {"kind": "event_graph_candidate_v3", "event_graph": {
                "generation": result["generation"], "call": generation["call"],
                "segment_index": result["event_index"],
                "producing_identity": deepcopy(state["identity"])}}}
        if self.store is None:
            raise RuntimeError("Event graph Store was not bound")
        namespace = _event_candidate_namespace(state["identity"])
        existing = self.store.get(namespace, candidate_id)
        if existing is not None and existing.value != wrapper:
            raise ValueError("Event candidate Store identity conflict")
        if existing is None:
            self.store.put(namespace, candidate_id, wrapper)
        saved = {**result, "status": "generated", "candidate_id": candidate_id,
                 "candidate_fingerprint": fingerprint}
        return {"event_results": [*state["event_results"][:-1], saved],
                "event_candidates": [*state["event_candidates"], wrapper],
                "evidence_index": state["evidence_index"] + 1,
                "outcome": {"status": "ok", "candidate_id": candidate_id}}

    def advance(self, state: dict[str, Any]) -> dict[str, Any]:
        return {"evidence_index": state["evidence_index"] + 1,
                "outcome": {"status": "ok"}}

    def verify_candidate(self, wrapper: dict[str, Any], state: dict[str, Any]) -> None:
        initial = self.initial_state()
        raw = wrapper.get("raw", {})
        graph = raw.get("event_graph", {}) if isinstance(raw, dict) else {}
        if (state.get("thread_id") != initial["thread_id"] or
                raw.get("kind") != "event_graph_candidate_v3" or
                graph.get("producing_identity") != initial["identity"] or
                wrapper not in state.get("event_candidates", [])):
            raise ValueError("Event candidate has no matching completed graph")
        stored = (self.store.get(_event_candidate_namespace(initial["identity"]),
                                 wrapper["candidate_id"]) if self.store is not None else None)
        if stored is None or stored.value != wrapper:
            raise ValueError("Event candidate differs from Store")
        index = graph["segment_index"]
        segment = state["evidence_segments"][index]
        generation = self._read(graph["generation"])
        saved = self._verified_saved_call(graph["call"],
            thread_id=initial["thread_id"], identity=initial["identity"],
            stage=f"event-generate-{index:03d}",
            messages=self._generation_messages(segment["steps"]), schema=EVENT_SEGMENT_SCHEMA)
        checked = validate_segment_candidate(saved, kind="event", trace=self.trace,
            visible_ids=[str(step["source_entry_id"]) for step in segment["steps"]])
        skill = _provenance(checked["skill"], [str(initial["identity"]["task_id"])])
        if (saved != generation["raw_output"] or checked != generation["checked"] or
                skill != wrapper["skill"] or checked["evidence"] != wrapper["evidence"] or
                _hash(skill) != wrapper["candidate_fingerprint"]):
            raise ValueError("Event candidate differs from its saved response")
