"""Restart a source-cited Task D01 summary after saved HTTP and bundle effects."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

from codeskill_rebuild.task_graph import TaskGraphRunner
from codeskill_rebuild.types import canonical_json, write_json
from tests.support_task_graph_real_process import CrashStages, _response, _setup


def _controlled_response(root: Path, request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    user = json.loads(payload["messages"][-1]["content"])
    if "required_covered_step_ids" not in user:
        return _response(root, request)
    count_path = root / "http-sends.json"
    counts = json.loads(count_path.read_text(encoding="utf-8")) if count_path.exists() else {}
    counts["d01_summary"] = counts.get("d01_summary", 0) + 1
    write_json(count_path, counts)
    if (root / "summary-uncertain").exists():
        raise httpx.ReadTimeout("controlled D01 summary sent without a response")
    ids = [step["source_entry_id"] for step in user["segment_steps"]]
    result = next((step["source_entry_id"] for step in reversed(user["segment_steps"])
                   if step["role"] == "toolResult"), ids[0])
    value = {"summary": "The controlled task had a local action and observed result.",
             "covered_step_ids": ids, "verbatim_evidence_step_ids": [result]}
    return httpx.Response(200, json={"id": "offline-summary", "object": "chat.completion",
        "model": "offline", "choices": [{"index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": canonical_json(value)}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})


class SummaryCrashStages(CrashStages):
    def __init__(self, root: Path, crash: str | None):
        super().__init__(root, crash=crash)

        def count(messages, *, request_options):
            del request_options
            user = json.loads(messages[-1]["content"])
            if "steps" in user or ("required_covered_step_ids" in user
                                   and len(user["segment_steps"]) > 2):
                return self.chat.allowance + 1
            return 10

        self.chat.token_counter = count
        self.chat.transport_factory = lambda: httpx.MockTransport(
            lambda request: _controlled_response(root, request))

    def d01_segment(self, state):
        result = super().d01_segment(state)
        if self.crash == "summary_response":
            assert result["outcome"]["status"] == "ok", result
            os._exit(76)
        return result

    def d01_bundle(self, state):
        result = super().d01_bundle(state)
        if self.crash == "summary_bundle":
            assert result["outcome"]["status"] == "ok", result
            os._exit(77)
        return result


def main() -> None:
    root, phase = Path(sys.argv[1]), sys.argv[2]
    root.mkdir(parents=True, exist_ok=True)
    if phase in {"start", "uncertain_start"}:
        _setup(root)
    if phase == "uncertain_start":
        (root / "summary-uncertain").write_text("sent", encoding="utf-8")
    crash = {"start": "summary_response", "bundle_crash": "summary_bundle"}.get(phase)
    service = SummaryCrashStages(root, crash)
    initial = service.initial_state()
    runner = TaskGraphRunner(directory=root / "graph", service=service)
    state = runner.invoke(identity=initial["identity"], initial=initial)
    if phase in {"uncertain_start", "uncertain_resume"}:
        assert state["stage"] == "stopped", state
        assert state["outcomes"]["d01_segment"]["status"] == "transport_uncertain"
        return
    assert phase == "finish" and state["stage"] == "rank", state
    assert state["outcomes"]["d01_plan"]["mode"] == "segmented"
    assert state["outcomes"]["d01_bundle"]["summary_count"] == 2
    assert state["outcomes"]["d01_segment"]["status"] == "ok"


if __name__ == "__main__":
    main()
