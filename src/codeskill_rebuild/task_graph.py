"""Durable Task phase graph; Event and bank publication remain external.

The graph owns Task phase order and both handoffs.  Service methods implement
one bounded stage each; none may advance another Task phase or publish a bank.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Protocol, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.store.sqlite import SqliteStore
from langgraph.types import Command, interrupt

from .types import canonical_json


STOP_STATUSES = {"context_blocked", "transport_uncertain", "infra_blocked"}
NO_MERGE_STATUSES = {"skip", "no_relevant_evidence", "no_eligible_sources",
                     "no_related_group", "duplicate", "rejected", "length"}


class TaskState(TypedDict, total=False):
    schema_version: int
    thread_id: str
    identity: dict[str, Any]
    source_ref: dict[str, Any]
    frozen_bank_hash: str
    stage: str
    outcomes: dict[str, Any]
    description_segments: list[dict[str, Any]]
    description_index: int
    description_results: list[dict[str, Any]]
    description_bundle: dict[str, Any]
    evidence_segments: list[dict[str, Any]]
    evidence_index: int
    generation_results: list[dict[str, Any]]
    generation_ref: dict[str, Any]
    description_records: list[dict[str, Any]]
    task_candidate_records: list[dict[str, Any]]
    candidate: dict[str, Any] | None
    ranked: list[dict[str, Any]]
    ranking_ref: dict[str, Any]
    pairing: dict[str, Any] | None
    pairing_ref: dict[str, Any]
    merge_candidate: dict[str, Any] | None
    merge_ref: dict[str, Any]
    event_receipt: dict[str, Any]
    task_receipt: dict[str, Any]
    publication_receipt: dict[str, Any]
    verifier_status: str


class TaskStageService(Protocol):
    def d01_plan(self, state: TaskState) -> dict[str, Any]: ...
    def d01_segment(self, state: TaskState) -> dict[str, Any]: ...
    def d01_bundle(self, state: TaskState) -> dict[str, Any]: ...
    def d01(self, state: TaskState) -> dict[str, Any]: ...
    def evidence_plan(self, state: TaskState) -> dict[str, Any]: ...
    def generate(self, state: TaskState) -> dict[str, Any]: ...
    def save_candidate(self, state: TaskState) -> dict[str, Any]: ...
    def advance(self, state: TaskState) -> dict[str, Any]: ...
    def rank(self, state: TaskState) -> dict[str, Any]: ...
    def pair(self, state: TaskState) -> dict[str, Any]: ...
    def merge(self, state: TaskState) -> dict[str, Any]: ...
    def fig9(self, state: TaskState) -> dict[str, Any]: ...
    def check_event_receipt(self, state: TaskState, receipt: dict[str, Any]) -> dict[str, Any]: ...
    def check_publication_receipt(self, state: TaskState, receipt: dict[str, Any]) -> dict[str, Any]: ...


def task_thread_id(identity: dict[str, Any]) -> str:
    required = {"round_id", "trial_id", "task_id", "trace_sha256",
                "historical_thinking_policy", "graph_version"}
    if not required <= set(identity):
        raise ValueError("Task graph thread identity is incomplete")
    if identity["graph_version"] != 2:
        raise ValueError("Task graph version is unsupported")
    return "task-v2-" + hashlib.sha256(canonical_json(identity).encode()).hexdigest()


def _status(state: TaskState, stage: str) -> str:
    item = state.get("outcomes", {}).get(stage, {})
    return str(item.get("status", "infra_blocked"))


def _stage_node(service: TaskStageService, name: str):
    # Leave the input annotation open: EventState adds per-event channels to
    # TaskState, and LangGraph otherwise narrows this node's input schema.
    def run(state) -> dict[str, Any]:
        try:
            result = getattr(service, name)(state)
        except Exception as error:
            result = {"outcome": {"status": "infra_blocked",
                                  "reason": f"{type(error).__name__}: {error}"}}
        if not isinstance(result, dict) or not isinstance(result.get("outcome"), dict):
            raise ValueError(f"Task {name} did not return a typed outcome")
        outcome = result.pop("outcome")
        if outcome.get("status") not in {
            "ok", "no_op", "no_relevant_evidence", "skip", "no_eligible_sources",
            "no_related_group", "duplicate", "rejected", "length",
            "context_blocked", "transport_uncertain", "infra_blocked",
        }:
            raise ValueError(f"Task {name} returned an unknown status")
        return {**result, "stage": name,
                "outcomes": {**state.get("outcomes", {}), name: outcome}}
    return run


def build_task_graph(service: TaskStageService, *, checkpointer: SqliteSaver,
                     store: SqliteStore):
    if hasattr(service, "bind_store"):
        service.bind_store(store)
    graph = StateGraph(TaskState)
    for name in ("d01_plan", "d01_segment", "d01_bundle", "d01",
                 "evidence_plan", "generate", "save_candidate",
                 "rank", "pair", "merge", "fig9"):
        graph.add_node(name, _stage_node(service, name))

    def await_event(state: TaskState) -> dict[str, Any]:
        receipt = interrupt({"kind": "await_event_staged_receipt",
                             "thread_id": state["thread_id"],
                             "frozen_bank_hash": state["frozen_bank_hash"],
                             "merge_candidate": state.get("merge_candidate")})
        checked = service.check_event_receipt(state, receipt)
        if checked.get("outcome", {}).get("status") != "ok":
            raise ValueError("Event staged receipt failed Task validation")
        return {"event_receipt": checked["receipt"], "stage": "event_receipt",
                "outcomes": {**state.get("outcomes", {}), "event_receipt": checked["outcome"]}}

    def await_publication(state: TaskState) -> dict[str, Any]:
        receipt = interrupt({"kind": "await_durable_publication_receipt",
                             "thread_id": state["thread_id"],
                             "task_receipt": state["task_receipt"]})
        checked = service.check_publication_receipt(state, receipt)
        if checked.get("outcome", {}).get("status") != "ok":
            raise ValueError("durable publication receipt failed Task validation")
        return {"publication_receipt": checked["receipt"], "stage": "publication_confirmed",
                "outcomes": {**state.get("outcomes", {}), "publication":
                             {"status": "publication_confirmed"}}}

    graph.add_node("await_event", await_event)
    graph.add_node("await_publication", await_publication)
    graph.add_node("stopped", lambda state: {"stage": "stopped"})
    graph.add_edge(START, "d01_plan")
    graph.add_conditional_edges("d01_plan", lambda s:
        "stopped" if _status(s, "d01_plan") in STOP_STATUSES
        else "d01_segment" if s["outcomes"]["d01_plan"].get("mode") == "segmented"
        else "d01")
    graph.add_conditional_edges("d01_segment", lambda s:
        "stopped" if _status(s, "d01_segment") != "ok"
        else "d01_segment" if s["description_index"] < len(s["description_segments"])
        else "d01_bundle")
    graph.add_conditional_edges("d01_bundle", lambda s:
        "d01" if _status(s, "d01_bundle") == "ok" else "stopped")
    graph.add_conditional_edges("d01", lambda s: "stopped" if _status(s, "d01") in STOP_STATUSES
                                else "await_event" if _status(s, "d01") == "skip"
                                else "evidence_plan")
    graph.add_conditional_edges("evidence_plan", lambda s: "stopped" if _status(s, "evidence_plan") in STOP_STATUSES
                                else "await_event" if _status(s, "evidence_plan") == "skip"
                                else "generate")
    graph.add_conditional_edges("generate", lambda s: "stopped" if _status(s, "generate") in STOP_STATUSES
                                else "save_candidate" if _status(s, "generate") == "ok"
                                else "await_event")
    graph.add_conditional_edges("save_candidate", lambda s:
                                "rank" if _status(s, "save_candidate") == "ok" and s["task_candidate_records"]
                                else "await_event" if _status(s, "save_candidate") == "ok"
                                else "stopped")
    graph.add_conditional_edges("rank", lambda s: "stopped" if _status(s, "rank") in STOP_STATUSES
                                else "pair" if _status(s, "rank") == "ok" else "await_event")
    graph.add_conditional_edges("pair", lambda s: "stopped" if _status(s, "pair") in STOP_STATUSES
                                else "merge" if _status(s, "pair") == "ok" else "await_event")
    graph.add_conditional_edges("merge", lambda s: "stopped" if _status(s, "merge") in STOP_STATUSES
                                else "await_event")
    graph.add_edge("await_event", "fig9")
    graph.add_conditional_edges("fig9", lambda s: "await_publication" if _status(s, "fig9") in {"ok", "no_op"}
                                else "stopped")
    graph.add_edge("await_publication", END)
    graph.add_edge("stopped", END)
    return graph.compile(checkpointer=checkpointer, store=store)


class TaskGraphRunner:
    """Open official SQLite persistence per invocation, including restart."""

    def __init__(self, *, directory: Path, service: TaskStageService,
                 store_path: Path | None = None):
        self.directory = Path(directory)
        self.service = service
        self.store_path = Path(store_path) if store_path is not None else self.directory / "store.sqlite"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.store_path.parent.mkdir(parents=True, exist_ok=True)

    def invoke(self, *, identity: dict[str, Any], initial: TaskState | None = None,
               receipt: dict[str, Any] | None = None) -> TaskState:
        thread_id = task_thread_id(identity)
        if (initial is None) == (receipt is None):
            raise ValueError("pass exactly one of initial Task state or handoff receipt")
        with ExitStack() as stack:
            saver = stack.enter_context(SqliteSaver.from_conn_string(str(self.directory / "checkpoints.sqlite")))
            store = stack.enter_context(SqliteStore.from_conn_string(str(self.store_path)))
            store.setup()
            graph = build_task_graph(self.service, checkpointer=saver, store=store)
            config = {"configurable": {"thread_id": thread_id}}
            if initial is not None:
                if initial.get("thread_id") != thread_id or initial.get("schema_version") != 2:
                    raise ValueError("initial Task state identity/schema differs")
                existing = graph.get_state(config)
                if existing.values:
                    for key in ("schema_version", "thread_id", "identity", "source_ref",
                                "frozen_bank_hash"):
                        if existing.values.get(key) != initial.get(key):
                            raise ValueError(f"existing Task thread differs at {key}")
                    if existing.next and existing.next[0] != "await_publication":
                        graph.invoke(None, config=config, durability="sync")
                else:
                    graph.invoke(initial, config=config, durability="sync")
            else:
                existing = graph.get_state(config)
                if not existing.values:
                    raise ValueError("Task graph handoff has no persisted thread")
                if not existing.next:
                    if existing.values.get("stage") != "publication_confirmed":
                        raise ValueError("Task graph has no handoff interrupt to resume")
                    return dict(existing.values)
                if existing.next not in {("await_event",), ("await_publication",)}:
                    raise ValueError("Task graph is not waiting at a handoff interrupt")
                graph.invoke(Command(resume=receipt), config=config, durability="sync")
            snapshot = graph.get_state(config)
            return dict(snapshot.values)


def read_task_graph_state(*, directory: Path, identity: dict[str, Any]) -> TaskState:
    """Read an existing official checkpoint without running a Task node."""
    thread_id = task_thread_id(identity)
    path = Path(directory) / "checkpoints.sqlite"
    if not path.is_file():
        raise ValueError("Task Graph checkpoint database is missing")
    with SqliteSaver.from_conn_string(str(path)) as saver:
        checkpoint = saver.get_tuple({"configurable": {"thread_id": thread_id}})
        if checkpoint is None:
            raise ValueError("Task Graph checkpoint thread is missing")
        values = checkpoint.checkpoint.get("channel_values", {})
        if not isinstance(values, dict) or values.get("thread_id") != thread_id:
            raise ValueError("Task Graph checkpoint identity differs")
        return dict(values)
