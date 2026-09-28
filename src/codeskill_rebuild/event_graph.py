"""Durable Event evidence and one-candidate-per-event LangGraph."""

from __future__ import annotations

import hashlib
from contextlib import ExitStack
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.store.sqlite import SqliteStore

from .task_graph import STOP_STATUSES, TaskState, _stage_node
from .types import canonical_json


def event_thread_id(identity: dict[str, Any]) -> str:
    required = {"round_id", "trial_id", "task_id", "trace_sha256", "kind",
                "historical_thinking_policy", "graph_version", "event_settings"}
    if not required <= set(identity) or identity["kind"] != "event":
        raise ValueError("Event graph thread identity is incomplete")
    if identity["graph_version"] != 3:
        raise ValueError("Event graph version is unsupported")
    return "event-v3-" + hashlib.sha256(canonical_json(identity).encode()).hexdigest()


class EventState(TaskState, total=False):
    event_index: int
    event_results: list[dict[str, Any]]
    event_candidates: list[dict[str, Any]]


def build_event_graph(service: Any, *, checkpointer: SqliteSaver, store: SqliteStore):
    service.bind_store(store)
    graph = StateGraph(EventState)
    for name in ("evidence_plan", "generate", "save_candidate", "advance"):
        graph.add_node(name, _stage_node(service, name))
    graph.add_node("stopped", lambda state: {"stage": "stopped"})
    graph.add_edge(START, "evidence_plan")
    graph.add_conditional_edges("evidence_plan", lambda s:
        "stopped" if s["outcomes"]["evidence_plan"]["status"] in STOP_STATUSES
        else END if s["outcomes"]["evidence_plan"]["status"] == "skip"
        else "generate")
    graph.add_conditional_edges("generate", lambda s:
        "save_candidate" if s["outcomes"]["generate"]["status"] == "ok"
        else "advance" if s["outcomes"]["generate"]["status"] in {"skip", "rejected", "length"}
        else "stopped")
    graph.add_conditional_edges("save_candidate", lambda s:
        "generate" if s["outcomes"]["save_candidate"]["status"] == "ok"
        and s["evidence_index"] < len(s["evidence_segments"])
        else END if s["outcomes"]["save_candidate"]["status"] == "ok"
        else "stopped")
    graph.add_conditional_edges("advance", lambda s:
        "generate" if s["evidence_index"] < len(s["evidence_segments"])
        else END)
    graph.add_edge("stopped", END)
    return graph.compile(checkpointer=checkpointer, store=store)


class EventGraphRunner:
    """Open the same official SQLite components used by Task on each restart."""

    def __init__(self, *, directory: Path, service: Any, store_path: Path):
        self.directory = Path(directory)
        self.service = service
        self.store_path = Path(store_path)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.store_path.parent.mkdir(parents=True, exist_ok=True)

    def invoke(self, *, identity: dict[str, Any], initial: dict[str, Any]) -> dict[str, Any]:
        thread_id = event_thread_id(identity)
        if initial.get("thread_id") != thread_id or initial.get("schema_version") != 3:
            raise ValueError("initial Event state identity/schema differs")
        with ExitStack() as stack:
            saver = stack.enter_context(SqliteSaver.from_conn_string(str(self.directory / "checkpoints.sqlite")))
            store = stack.enter_context(SqliteStore.from_conn_string(str(self.store_path)))
            store.setup()
            graph = build_event_graph(self.service, checkpointer=saver, store=store)
            config = {"configurable": {"thread_id": thread_id}}
            existing = graph.get_state(config)
            if existing.values:
                for key in ("schema_version", "thread_id", "identity", "source_ref"):
                    if existing.values.get(key) != initial.get(key):
                        raise ValueError(f"existing Event thread differs at {key}")
                if existing.next:
                    graph.invoke(None, config=config, durability="sync")
            else:
                graph.invoke(initial, config=config, durability="sync")
            return dict(graph.get_state(config).values)

    def verify_candidate(self, wrapper: dict[str, Any], state: dict[str, Any]) -> None:
        with SqliteStore.from_conn_string(str(self.store_path)) as store:
            store.setup()
            self.service.bind_store(store)
            self.service.verify_candidate(wrapper, state)
