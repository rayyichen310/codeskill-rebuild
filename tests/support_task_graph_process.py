"""Fixture-only cross-process Task Graph crash windows."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from langgraph.store.sqlite import SqliteStore

from codeskill_rebuild.bank import SkillBank
from codeskill_rebuild.task_graph import TaskGraphRunner, read_task_graph_state
from codeskill_rebuild.task_graph_model import TaskCallResult
from codeskill_rebuild.task_graph_stages import TaskGraphStages, _candidate_namespace
from codeskill_rebuild.types import canonical_json, sha256_text, write_json
from tests.test_r015_c_only_driver_integration import _ControlledTaskChat
from tests.test_task_graph_runtime import _operation, _publication_state, _trace


ROOT = Path(__file__).resolve().parents[1]


class ReplayChat(_ControlledTaskChat):
    def call(self, *, thread_id, stage, messages, schema, identity):
        requested = {"thread_id": thread_id, "stage": stage, "messages": messages,
                     "schema": schema, "identity": identity, "model": self.model,
                     "base_url": self.base_url, "temperature": 0,
                     "reasoning_effort": "max", "max_tokens": self.output_tokens}
        key = sha256_text(canonical_json(requested))
        call_dir = self.root / "task-calls" / key
        response = call_dir / "wire-response.json"
        if response.is_file():
            value = json.loads(response.read_text(encoding="utf-8"))
            return TaskCallResult("ok", json.loads(value["choices"][0]["message"]["content"]),
                                  key, call_dir)
        count_path = self.root / "model-calls.json"
        counts = json.loads(count_path.read_text(encoding="utf-8")) if count_path.is_file() else {}
        counts[stage] = counts.get(stage, 0) + 1
        write_json(count_path, counts)
        return super().call(thread_id=thread_id, stage=stage, messages=messages,
                            schema=schema, identity=identity)


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
    def __init__(self, root: Path, *, crash: str | None):
        trace = _trace()
        trace["text_manager_eligible"] = True
        super().__init__(
            context={"task_id": "build-pmars"}, input_value={}, trace=trace,
            current_ref={"round_id": 1, "trial_id": "trial1", "session_id": "s1"},
            traces=[], frozen_bank=SkillBank.empty("terminal-bench"),
            chat=ReplayChat(root=root / "calls", model="controlled"),
            artifact_root=root / "artifacts", prompt_root=ROOT / "prompts",
        )
        self.root = root
        self.crash = crash

    def bind_store(self, store):
        self.store = CountingStore(store, self.root / "store-puts.json")

    def d01_plan(self, state):
        return {"description_segments": [], "outcome": {"status": "ok", "mode": "full"}}

    def d01(self, state):
        return {"description_records": [], "outcome": {"status": "rejected",
                                                "reason": "controlled D01 outside crash-window scope"}}

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
            os._exit(75)
        return result

    def rank(self, state):
        return {"ranked": [], "outcome": {"status": "no_eligible_sources"}}

    def fig9(self, state):
        event = state["event_receipt"]
        bank = SkillBank.from_dict(self._read(event["event_bank"]))
        operation = _operation("task-op", "task", "extraction")
        self._replay(bank, [operation])
        bank_ref = self._artifact("task-staged-bank.json", bank.to_dict())
        receipt = {"thread_id": state["thread_id"], "H0": event["H0"],
                   "HE": event["HE"], "HT": bank.snapshot()["state_sha256"],
                   "event_operations": [], "task_operations": [operation],
                   "task_bank": bank_ref}
        return {"task_receipt": receipt,
                "outcome": {"status": "ok", "task_operation_count": 1}}


def main() -> None:
    root, phase = Path(sys.argv[1]), sys.argv[2]
    root.mkdir(parents=True, exist_ok=True)
    graph_dir = root / "graph"
    crash = {"start": "plan", "response_crash": "response",
             "store_crash": "store"}.get(phase)
    service = CrashStages(root, crash=crash)
    initial = service.initial_state()
    identity = initial["identity"]
    runner = TaskGraphRunner(directory=graph_dir, service=service)
    if phase in {"start", "response_crash", "store_crash", "resume_event"}:
        state = runner.invoke(identity=identity, initial=initial)
        assert phase == "resume_event" and state["stage"] == "rank", state
        with SqliteStore.from_conn_string(str(graph_dir / "store.sqlite")) as store:
            store.setup()
            saved = store.get(_candidate_namespace(identity), state["candidate"]["candidate_id"])
            assert saved is not None and saved.value == state["candidate"]
        return
    if phase == "event":
        state = read_task_graph_state(directory=graph_dir, identity=identity)
        h0 = service.frozen_bank.snapshot()["state_sha256"]
        state = runner.invoke(identity=identity, receipt={"thread_id": state["thread_id"],
                              "H0": h0, "HE": h0, "event_operations": []})
        assert state["task_receipt"]["task_operations"][0]["operation_id"] == "task-op"
        return
    state = read_task_graph_state(directory=graph_dir, identity=identity)
    task = state["task_receipt"]
    bank = SkillBank.from_dict(service._read(task["task_bank"]))
    durable_path = root / "durable-state.json"
    if phase == "publish_crash":
        assert not durable_path.exists()
        write_json(durable_path, _publication_state(
            h0=task["H0"], hm=task["HT"], operations=task["task_operations"], bank=bank))
        os._exit(73)
    receipt = {"thread_id": state["thread_id"], "H0": task["H0"],
               "HE": task["HE"], "HT": task["HT"], "HM": task["HT"],
               "maintenance_operations": [], "ordered_operation_ids": ["task-op"],
               "durable_state_path": str(durable_path)}
    confirmed = runner.invoke(identity=identity, receipt=receipt)
    assert confirmed["stage"] == "publication_confirmed"
    if phase == "confirm_crash":
        os._exit(74)
    assert phase == "finish"
    durable = json.loads(durable_path.read_text(encoding="utf-8"))
    round_state = durable["rounds"]["1"]
    assert [op["operation_id"] for op in round_state["operations"]] == ["publish:r1:build-pmars"]
    assert [op["operation_id"] for op in round_state["assignments"]["build-pmars"]["publication"]["operations"]] == ["task-op"]
    assert len(bank.to_dict()["skills"]) == 1
    write_json(root / "finish.json", {"stage": confirmed["stage"],
                                      "publication_sha256": sha256_text(canonical_json(durable))})


if __name__ == "__main__":
    main()
