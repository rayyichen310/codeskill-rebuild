"""Controlled transport checks for direct Event extraction from shared segments."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from codeskill_rebuild.bank import SkillBank
from codeskill_rebuild.event_graph import EventGraphRunner
from codeskill_rebuild.event_graph_stages import EventGraphStages
from codeskill_rebuild.task_graph_model import TaskChatBoundary
from codeskill_rebuild.types import canonical_json


ROOT = Path(__file__).resolve().parents[1]


def _trace(count: int) -> dict:
    steps = [{"source_entry_id": "u", "role": "user", "content": "Repair the requested project"}]
    for i in range(count):
        for suffix, tool_id, error in (("probe", f"probe{i}", True),
                                       ("repair", f"repair{i}", False)):
            steps.append({"source_entry_id": f"{suffix}{i}", "role": "assistant",
                          "assistant": {"tool_calls": [{"tool_call_id": tool_id,
                              "tool_name": "exec", "arguments": {"command": f"echo {tool_id}"}}]}})
            steps.append({"source_entry_id": f"{suffix}-result{i}", "role": "toolResult",
                          "tool_result": {"tool_call_id": tool_id, "tool_name": "exec",
                                          "content": "failed" if error else "repaired",
                                          "is_error": error}})
    return {"source": {"canonical_instance_id": "build-pmars", "instance_id": "build-pmars"},
            "instruction": "Repair the requested project", "outcome": {"official_reward": "0"},
            "historical_compaction": {"raw_message_step_ids": [step["source_entry_id"] for step in steps],
                                      "control_event_ids": []},
            "text_manager_eligible": True, "steps": steps}


def test_event_each_original_segment_generates_or_skips_without_loss(tmp_path):
    trace = _trace(2)
    trace["steps"].append({"source_entry_id": "summary", "role": "assistant",
                           "content": "Historical compaction control"})
    trace["historical_compaction"]["control_event_ids"] = ["summary"]
    sent = []

    def count_tokens(messages, *, request_options):
        user = json.loads(messages[-1]["content"])
        return len(user["segment_steps"]) * 40 + 10

    def respond(request):
        payload = json.loads(request.content)
        user = json.loads(payload["messages"][-1]["content"])
        steps = user["segment_steps"]
        sent.append(steps)
        ids = [step["source_entry_id"] for step in steps]
        if "probe1" in ids:
            value = {"action": "skip", "reason": "No reusable local lesson"}
        else:
            value = {"action": "generate", "skill": {
                "title": "Inspect a local result", "granularity": "event-driven",
                "when_to_apply": "When a command produces a local result",
                "rules": ["Inspect the result before proceeding.",
                          "Keep the next action tied to what the result showed."]},
                "evidence": {"step_ids": [ids[-1]]}}
        return httpx.Response(200, json={"id": "controlled", "object": "chat.completion",
            "model": "offline", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": canonical_json(value)}}],
            "usage": {"prompt_tokens": count_tokens(payload["messages"], request_options={}),
                      "completion_tokens": 5}})

    chat = TaskChatBoundary(root=tmp_path / "calls", base_url="http://offline.local/v1",
        model="offline", token_counter=count_tokens, context_tokens=16384 + 4096 + 130,
        transport_factory=lambda: httpx.MockTransport(respond))
    service = EventGraphStages(
        context={"task_id": "build-pmars"}, input_value={}, trace=trace,
        current_ref={"round_id": 1, "trial_id": "trial", "session_id": "session",
                     "task_id": "build-pmars"},
        traces=[], frozen_bank=SkillBank.empty("terminal-bench"), chat=chat,
        artifact_root=tmp_path, prompt_root=ROOT / "prompts")
    runner = EventGraphRunner(directory=tmp_path / "event-graph", service=service,
                              store_path=tmp_path / "store.sqlite")
    initial = service.initial_state()
    state = runner.invoke(identity=initial["identity"], initial=initial)
    assert state["stage"] != "stopped", state.get("outcomes")
    assert len(sent) == len(state["evidence_segments"]) == len(state["event_results"])
    assert [step for segment in sent for step in segment] == trace["steps"][:-1]
    assert [item["status"] for item in state["event_results"]] == [
        "generated", "generated", "skip", "generated"]
    assert len(state["event_candidates"]) == 3
    for candidate in state["event_candidates"]:
        assert candidate["evidence"] == {
            "canonical_instance_id": "build-pmars",
            "step_ids": [sent[candidate["raw"]["event_graph"]["segment_index"]][-1]["source_entry_id"]]}
        runner.verify_candidate(candidate, state)
