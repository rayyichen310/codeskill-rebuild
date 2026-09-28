"""Restart the official Event graph and C-only publication after durable side effects."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from codeskill_rebuild.bank import SkillBank
from codeskill_rebuild.c_only_protocol import COnlyProtocol
from codeskill_rebuild.event_graph import EventGraphRunner
from codeskill_rebuild.event_graph_stages import EventGraphStages
from codeskill_rebuild.task_graph_model import TaskChatBoundary
from codeskill_rebuild.types import canonical_json, sha256_file, write_json
from scripts.run_r015_c_only_harbor_driver import _fig9_operation
from tests.test_event_graph_runtime import _trace


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "r015-c-only-coding.json"
BASELINE = ROOT / "docs" / "baselines" / "r015-legacy-coding-baseline-20260913.json"
TASK_ID = "build-pmars"


def _setup(root: Path) -> None:
    state_path = root / "protocol.json"
    protocol = COnlyProtocol.initialize(CONFIG, BASELINE, state_path)
    protocol.authorize_start()
    assignment = protocol.freeze_task(TASK_ID)
    trace = _trace(1)
    trace["r015_binding"] = {"round_id": 1, "task_id": TASK_ID,
                             "trial_id": assignment["trial_id"], "session_id": "offline-session"}
    trace_path = root / "trajectory.json"
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
    if "segment_steps" in user:
        stage = "generation"
        ids = [step["source_entry_id"] for step in user["segment_steps"]]
        value = {"action": "generate", "skill": {
            "title": "Controlled local repair", "granularity": "event-driven",
            "when_to_apply": "When the local repair probe fails",
            "rules": ["Inspect the failed probe, repair it, and check the result."]},
            "evidence": {"step_ids": [ids[-1]]}}
    else:
        raise AssertionError(f"unexpected Event request: {user.keys()}")
    counts_path = root / "http-sends.json"
    counts = json.loads(counts_path.read_text(encoding="utf-8")) if counts_path.exists() else {}
    counts[stage] = counts.get(stage, 0) + 1
    write_json(counts_path, counts)
    return httpx.Response(200, json={"id": "controlled", "object": "chat.completion",
        "model": "offline", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": canonical_json(value)}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})


class CountingStore:
    def __init__(self, delegate, path: Path):
        self.delegate = delegate
        self.path = path

    def get(self, *args, **kwargs):
        return self.delegate.get(*args, **kwargs)

    def put(self, *args, **kwargs):
        count = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else 0
        write_json(self.path, count + 1)
        return self.delegate.put(*args, **kwargs)


class CrashStages(EventGraphStages):
    def __init__(self, root: Path, crash: str | None):
        protocol = COnlyProtocol.load(root / "protocol.json", CONFIG, BASELINE)
        ref = protocol._assignment(TASK_ID)["trial_evidence"]["trajectory"]
        trace = json.loads((root / "trajectory.json").read_text(encoding="utf-8"))
        chat = TaskChatBoundary(root=root / "calls", base_url="http://offline.local/v1",
            model="offline", token_counter=lambda messages, *, request_options: 10,
            transport_factory=lambda: httpx.MockTransport(lambda request: _response(root, request)))
        super().__init__(context={"task_id": TASK_ID}, input_value={},
            trace=trace, current_ref=ref, traces=[(TASK_ID, trace, ref)],
            frozen_bank=protocol.frozen_bank(TASK_ID), chat=chat,
            artifact_root=root / "artifacts", prompt_root=ROOT / "prompts")
        self.root = root
        self.crash = crash

    def bind_store(self, store):
        self.store = CountingStore(store, self.root / "store-puts.json")

    def evidence_plan(self, state):
        result = super().evidence_plan(state)
        if self.crash == "bundle":
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


class EmptyBankEncoder:
    def index_skill(self, value):
        return [1.0, 0.0], {"kind": "controlled", "granularity": value["granularity"]}


def _operation(root: Path, candidate: dict, frozen_bank: SkillBank, trial_id: str) -> dict:
    response_path = root / "fig9-response.json"
    decision = {"action": "add", "reason": "controlled local repair",
                "evidence": {"source_skill_ids": ["candidate"], "source_example_ids": []}}
    write_json(response_path, decision)

    def controlled_manager_call(*args, **kwargs):
        del args, kwargs
        return ({"call_id": "controlled-fig9", "json": decision},
                {"path": str(response_path), "sha256": sha256_file(response_path),
                 "response": {"call_id": "controlled-fig9", "path": str(response_path),
                              "sha256": sha256_file(response_path)}}, None)

    with patch("scripts.run_r015_c_only_harbor_driver._manager_call",
               side_effect=controlled_manager_call), patch(
               "scripts.run_r015_c_only_harbor_driver._finish_manager_journal"):
        operation = _fig9_operation(context={"trial_id": trial_id, "task_id": TASK_ID},
            executor=SimpleNamespace(encoder=EmptyBankEncoder()),
            bank=SkillBank.from_dict(frozen_bank.to_dict()), candidate=candidate["skill"],
            source_instance_ids=[TASK_ID], operation_id="event-op",
            phase="fig9-extraction-001", purpose_prefix="r015_c_only_fig9_after_extraction",
            extra_evidence={"extraction_candidate_id": candidate["candidate_id"],
                            "extraction_candidate_fingerprint": candidate["candidate_fingerprint"],
                            "extraction_source": candidate["raw"]})
    operation.update({"source_kind": "extraction", "candidate_id": candidate["candidate_id"]})
    return operation


def main() -> None:
    root, phase = Path(sys.argv[1]), sys.argv[2]
    root.mkdir(parents=True, exist_ok=True)
    if phase == "start":
        _setup(root)
    crash = {"start": "bundle", "response_crash": "response",
             "store_crash": "store"}.get(phase)
    service = CrashStages(root, crash)
    initial = service.initial_state()
    runner = EventGraphRunner(directory=root / "artifacts" / "event-graph", service=service,
                              store_path=root / "event-store.sqlite")
    state = runner.invoke(identity=initial["identity"], initial=initial)
    assert phase not in {"start", "response_crash", "store_crash"}
    assert state["stage"] != "stopped", state.get("outcomes")
    assert len(state["event_candidates"]) == 1
    candidate = state["event_candidates"][0]
    runner.verify_candidate(candidate, state)
    if phase == "resume_event":
        return
    state_path = root / "protocol.json"
    protocol = COnlyProtocol.load(state_path, CONFIG, BASELINE)
    assignment = protocol._assignment(TASK_ID)
    if phase == "publish_crash":
        result = state["event_results"][0]
        protocol.record_event_attempt(TASK_ID, attempt_no=1, outcome="generated",
            candidate=candidate["skill"], raw_response={"event_graph": result})
        protocol.extract_after_task(TASK_ID, candidates=[candidate],
            trajectory_ref=assignment["trial_evidence"]["trajectory"],
            extraction_evidence={"event": {"kind": "r015_event_graph_v3",
                "thread_id": state["thread_id"], "identity": state["identity"],
                "directory": str(runner.directory)}})
        protocol.save(state_path)
        operation = _operation(root, candidate, protocol.frozen_bank(TASK_ID),
                               assignment["trial_id"])
        write_json(root / "operation.json", operation)
        protocol.publish_after_task(TASK_ID, operations=[operation])
        protocol.save(state_path)
        os._exit(73)
    assert phase == "finish"
    operation = json.loads((root / "operation.json").read_text(encoding="utf-8"))
    protocol.publish_after_task(TASK_ID, operations=[operation])
    protocol.finish_task(TASK_ID)
    protocol.save(state_path)
    durable = COnlyProtocol.load(state_path, CONFIG, BASELINE)
    round_state = durable.state["rounds"]["1"]
    assert [item["operation_id"] for item in round_state["bank"]["operations"]] == ["event-op"]
    assert list(round_state["completed_tasks"]) == [TASK_ID]


if __name__ == "__main__":
    main()
