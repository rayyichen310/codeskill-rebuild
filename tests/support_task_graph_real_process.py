"""Crash the production Task call, graph, and C-only publication boundaries."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
from langgraph.store.sqlite import SqliteStore

from codeskill_rebuild.bank import SkillBank
from codeskill_rebuild.c_only_protocol import COnlyProtocol
from codeskill_rebuild.task_graph import TaskGraphRunner, read_task_graph_state
from codeskill_rebuild.task_graph_model import TaskChatBoundary
from codeskill_rebuild.task_graph_stages import TaskGraphStages, _candidate_namespace
from codeskill_rebuild.types import canonical_json, sha256_file, sha256_text, write_json
from scripts.run_r015_c_only import _confirm_task_graph_publication
from tests.test_task_graph_runtime import _trace


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "r015-c-only-coding.json"
BASELINE = ROOT / "docs" / "baselines" / "r015-legacy-coding-baseline-20260913.json"
TASK_ID = "build-pmars"


def _paths(root: Path) -> tuple[Path, Path, Path]:
    return root / "protocol.json", root / "graph", root / "trajectory.json"


def _setup(root: Path) -> None:
    state_path, _graph_dir, trace_path = _paths(root)
    protocol = COnlyProtocol.initialize(CONFIG, BASELINE, state_path)
    protocol.authorize_start()
    assignment = protocol.freeze_task(TASK_ID)
    trace = _trace()
    trace["source"].update({"task_name": f"terminal-bench/{TASK_ID}", "instance_id": TASK_ID})
    trace["text_manager_eligible"] = True
    trace["r015_binding"] = {"round_id": 1, "task_id": TASK_ID,
                              "trial_id": assignment["trial_id"], "session_id": "offline-session"}
    write_json(trace_path, trace)
    ref = {"round_id": 1, "task_id": TASK_ID, "trial_id": assignment["trial_id"],
           "session_id": "offline-session", "complete": True,
           "path": str(trace_path), "sha256": sha256_file(trace_path)}
    protocol.record_trial(TASK_ID, outcome="completed", trajectory=ref,
                          raw_evidence={"official_harbor_trial": True,
                                        "trial_id": assignment["trial_id"],
                                        "session_id": "offline-session",
                                        "historical_baseline_used": False})
    protocol.save(state_path)


def _response(root: Path, request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    user = json.loads(payload["messages"][-1]["content"])
    if "segment_steps" in user and "required_covered_step_ids" not in user:
        stage = "generation"
        action = next(step["source_entry_id"] for step in user["segment_steps"]
                      if step["role"] == "assistant")
        value = {"action": "generate",
                 "skill": {"title": "Controlled repair SOP", "granularity": "general",
                           "when_to_apply": "When a bounded repair needs validation.",
                           "rules": ["Inspect the failure and verify the repair."]},
                 "candidate_context": {"task_goal": "Complete the repair",
                                       "whole_task_outcome": "completed",
                                       "hard_constraints": [], "environment_assumptions": [],
                                       "observed_results": [], "known_limitations": []},
                 "evidence": {"step_ids": [action]}}
    else:
        stage, value = "d01", {}  # Rejected model output still uses the real D01 call.
    count_path = root / "http-sends.json"
    counts = json.loads(count_path.read_text(encoding="utf-8")) if count_path.is_file() else {}
    counts[stage] = counts.get(stage, 0) + 1
    write_json(count_path, counts)
    return httpx.Response(200, json={"id": "offline-call", "object": "chat.completion",
                                     "model": "offline", "choices": [{"index": 0,
                                     "finish_reason": "stop", "message": {"role": "assistant",
                                     "content": canonical_json(value)}}],
                                     "usage": {"prompt_tokens": 10,
                                               "completion_tokens": 5, "total_tokens": 15}})


class CountingStore:
    def __init__(self, delegate, count_path: Path):
        self.delegate = delegate
        self.count_path = count_path

    def get(self, *args, **kwargs):
        return self.delegate.get(*args, **kwargs)

    def put(self, *args, **kwargs):
        count = json.loads(self.count_path.read_text(encoding="utf-8")) if self.count_path.is_file() else 0
        write_json(self.count_path, count + 1)
        return self.delegate.put(*args, **kwargs)


class CrashStages(TaskGraphStages):
    """Delegate every stage to production; exit after selected side effects."""

    def __init__(self, root: Path, *, crash: str | None):
        state_path, _graph_dir, trace_path = _paths(root)
        protocol = COnlyProtocol.load(state_path, CONFIG, BASELINE)
        assignment = protocol._assignment(TASK_ID)
        ref = assignment["trial_evidence"]["trajectory"]
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        chat = TaskChatBoundary(root=root / "calls", base_url="http://offline.local/v1",
                                model="offline", token_counter=lambda messages, *, request_options: 10,
                                transport_factory=lambda: httpx.MockTransport(
                                    lambda request: _response(root, request)))
        super().__init__(context={"task_id": TASK_ID},
                         input_value={"state": {"path": str(state_path)},
                                      "round_material": {"task_candidate_pool": []}},
                         trace=trace, current_ref=ref, traces=[(TASK_ID, trace, ref)],
                         frozen_bank=protocol.frozen_bank(TASK_ID), chat=chat,
                         artifact_root=root / "artifacts", prompt_root=ROOT / "prompts",
                         require_durable_extraction=True)
        self.root = root
        self.crash = crash

    def bind_store(self, store):
        self.store = CountingStore(store, self.root / "store-puts.json")

    def evidence_plan(self, state):
        result = super().evidence_plan(state)
        if self.crash == "plan":
            assert result["outcome"]["status"] == "ok", result
            os._exit(71)
        return result

    def generate(self, state):
        result = super().generate(state)
        if self.crash == "response":
            assert result["outcome"]["status"] == "ok", result
            os._exit(72)
        return result

    def save_candidate(self, state):
        result = super().save_candidate(state)
        if self.crash == "store":
            assert result["outcome"]["status"] == "ok", result
            os._exit(75)
        return result


def _event_operation(root: Path) -> dict:
    skill = {"title": "Controlled Event repair", "granularity": "event",
             "when_to_apply": "When a reproducible terminal repair needs validation.",
             "rules": ["Inspect the failure and verify the repair."],
             "benchmark": "terminal-bench",
             "provenance": {"source_instance_ids": [TASK_ID],
                            "source_instance_ids_raw": [TASK_ID],
                            "parent_skill_ids": []}}
    response_path = root / "fig9-response.json"
    if not response_path.exists():
        write_json(response_path, {"action": "add", "reason": "controlled Event decision"})
    response_hash = sha256_file(response_path)
    fingerprint = sha256_text(canonical_json(skill))
    return {"operation_id": "event-op", "source_kind": "extraction",
            "candidate_id": "event-candidate", "original_candidate": skill,
            "candidate": skill, "decision": "add", "source_instance_ids": [TASK_ID],
            "evidence": {"manager_response_sha256": response_hash,
                         "manager_response_path": str(response_path),
                         "fig9_response_sha256": response_hash,
                         "fig9_response_path": str(response_path),
                         "original_candidate_fingerprint": fingerprint,
                         "merged_candidate_fingerprint": fingerprint,
                         "extraction_candidate_fingerprint": fingerprint}}


def _event_handoff(root: Path, service: CrashStages, state: dict) -> dict:
    state_path, graph_dir, _trace_path = _paths(root)
    protocol = COnlyProtocol.load(state_path, CONFIG, BASELINE)
    operation = _event_operation(root)
    marker = {"kind": "r015_task_graph_v1", "thread_id": state["thread_id"],
              "identity": state["identity"], "directory": str(graph_dir)}
    protocol.extract_after_task(TASK_ID,
                                candidates=[{"candidate_id": "event-candidate",
                                             "skill": operation["candidate"]}],
                                trajectory_ref=protocol._assignment(TASK_ID)["trial_evidence"]["trajectory"],
                                extraction_evidence={"task": {"graph": marker}})
    protocol.save(state_path)
    ack_path = root / "extraction-ack.json"
    write_json(ack_path, {"task_id": TASK_ID, "status": "saved"})
    bank = SkillBank.from_dict(service.frozen_bank.to_dict())
    service._replay(bank, [operation])
    return {"thread_id": state["thread_id"], "H0": service.frozen_bank.snapshot()["state_sha256"],
            "HE": bank.snapshot()["state_sha256"], "event_operations": [operation],
            "extraction_ref": {"durable_state_path": str(state_path),
                               "durable_state_sha256": sha256_file(state_path),
                               "ack_path": str(ack_path), "ack_sha256": sha256_file(ack_path)}}


def main() -> None:
    root, phase = Path(sys.argv[1]), sys.argv[2]
    root.mkdir(parents=True, exist_ok=True)
    state_path, graph_dir, _trace_path = _paths(root)
    if phase == "start":
        _setup(root)
    crash = {"start": "plan", "response_crash": "response",
             "store_crash": "store"}.get(phase)
    service = CrashStages(root, crash=crash)
    initial = service.initial_state()
    identity = initial["identity"]
    runner = TaskGraphRunner(directory=graph_dir, service=service)
    if phase in {"start", "response_crash", "store_crash", "resume_event"}:
        state = runner.invoke(identity=identity, initial=initial)
        assert phase == "resume_event" and state["stage"] == "rank", state
        assert state["outcomes"]["rank"]["status"] == "no_eligible_sources", state
        with SqliteStore.from_conn_string(str(graph_dir / "store.sqlite")) as store:
            store.setup()
            saved = store.get(_candidate_namespace(identity), state["candidate"]["candidate_id"])
            assert saved is not None and saved.value == state["candidate"]
        return
    if phase == "event":
        state = read_task_graph_state(directory=graph_dir, identity=identity)
        receipt = _event_handoff(root, service, state)
        staged = runner.invoke(identity=identity, receipt=receipt)
        assert staged["stage"] == "fig9" and staged["outcomes"]["fig9"]["status"] == "no_op", staged
        assert [op["operation_id"] for op in staged["task_receipt"]["event_operations"]] == ["event-op"]
        return
    protocol = COnlyProtocol.load(state_path, CONFIG, BASELINE)
    assignment = protocol._assignment(TASK_ID)
    output = {"publication": {"operations": [_event_operation(root)]}}
    if phase == "publish_crash":
        protocol.publish_after_task(TASK_ID, operations=output["publication"]["operations"])
        protocol.save(state_path)
        os._exit(73)
    _confirm_task_graph_publication(protocol=protocol, assignment=assignment,
                                    output=output, state_path=state_path)
    if phase == "confirm_crash":
        os._exit(74)
    assert phase == "finish"
    protocol.finish_task(TASK_ID)
    protocol.save(state_path)
    durable = COnlyProtocol.load(state_path, CONFIG, BASELINE)
    round_state = durable.state["rounds"]["1"]
    assert TASK_ID in round_state["completed_tasks"]
    assert [item["operation_id"] for item in round_state["bank"]["operations"]] == ["event-op"]
    assert read_task_graph_state(directory=graph_dir, identity=identity)["stage"] == "publication_confirmed"


if __name__ == "__main__":
    main()
