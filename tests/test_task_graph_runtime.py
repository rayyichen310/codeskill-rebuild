"""Offline Task graph and source contract checks; no model or Harbor access."""

from __future__ import annotations

import tempfile
import json
from pathlib import Path

import httpx
import pytest

from codeskill_rebuild.task_graph import TaskGraphRunner, task_thread_id
from codeskill_rebuild.task_graph_model import TaskChatBoundary
from codeskill_rebuild.task_graph_stages import TaskGraphStages, PublicationReceiptService
from codeskill_rebuild.event_graph_stages import EventGraphStages
from codeskill_rebuild.segment_candidates import (
    EVENT_SEGMENT_SCHEMA, TASK_SEGMENT_SCHEMA, validate_segment_candidate,
)
from codeskill_rebuild.task_sop import (
    TASK_SOP_MERGE_SCHEMA, task_sop_merge_messages, task_sop_pairing_messages,
    validate_task_sop_merge,
)
from codeskill_rebuild.bank import SkillBank
from codeskill_rebuild.types import write_json, canonical_json, sha256_text


def _trace():
    steps = [
        {"source_entry_id": "user-1", "role": "user", "content": "Repair the project"},
        {"source_entry_id": "action-1", "role": "assistant",
         "assistant": {"tool_calls": [{"tool_call_id": "call-1", "tool_name": "exec",
                                        "arguments": {"command": "run check"}}]}},
        {"source_entry_id": "observe-1", "role": "toolResult",
         "tool_result": {"tool_call_id": "call-1", "tool_name": "exec",
                         "content": "check passed", "is_error": False}},
    ]
    return {"source": {"canonical_instance_id": "build-pmars",
                       "instance_id": "build-pmars", "task_name": "terminal-bench/build-pmars"},
            "instruction": "Repair the project", "outcome": {"official_reward": "1"},
            "text_manager_eligible": True, "steps": steps,
            "historical_compaction": {"raw_message_step_ids": [step["source_entry_id"] for step in steps],
                                      "control_event_ids": []}}


def test_full_original_request_above_old_cap_stays_whole_for_task_and_event(tmp_path):
    trace = _trace()
    seen_options = []

    def count(_messages, *, request_options):
        seen_options.append(request_options)
        return 70000

    chat = TaskChatBoundary(root=tmp_path / "calls", base_url="http://offline.local/v1",
        model="offline", token_counter=count, context_tokens=524288,
        output_tokens=65536, safety_tokens=4096)
    common = dict(context={"task_id": "build-pmars"}, input_value={}, trace=trace,
        current_ref={"round_id": 1, "trial_id": "trial", "session_id": "session"},
        traces=[], frozen_bank=SkillBank.empty("terminal-bench"), chat=chat,
        artifact_root=tmp_path, prompt_root=Path(__file__).resolve().parents[1] / "prompts")
    task = TaskGraphStages(**common)
    event = EventGraphStages(**common)
    assert chat.allowance == 454656
    assert len(task.prepare_segments()) == len(event.prepare_segments()) == 1
    assert task.prepare_segments()[0]["step_ids"] == ["user-1", "action-1", "observe-1"]
    assert event.prepare_segments()[0]["step_ids"] == task.prepare_segments()[0]["step_ids"]
    assert all(options["max_tokens"] == 65536 for options in seen_options)

    chat.token_counter = lambda _messages, *, request_options: 454657
    blocked = TaskGraphStages(**common).evidence_plan({})
    assert blocked["outcome"]["status"] == "context_blocked"
    assert blocked["evidence_segments"] == []


def test_skill_level_source_ids_cover_multiple_rules_and_must_be_visible():
    trace = _trace()
    value = {"action": "generate", "skill": {
        "title": "Inspect and verify repairs", "granularity": "general",
        "when_to_apply": "When a bounded repair needs validation",
        "rules": ["Inspect the failure.", "Verify the repair result."]},
        "candidate_context": {"task_goal": "Repair the project",
            "whole_task_outcome": "completed", "hard_constraints": [],
            "environment_assumptions": [], "observed_results": [],
            "known_limitations": []},
        "evidence": {"step_ids": ["action-1"]}}
    checked = validate_segment_candidate(value, kind="task", trace=trace,
        visible_ids=["user-1", "action-1"])
    assert checked["evidence"] == {
        "canonical_instance_id": "build-pmars", "step_ids": ["action-1"]}
    value["evidence"]["step_ids"] = ["observe-1"]
    with pytest.raises(ValueError, match="absent from its request"):
        validate_segment_candidate(value, kind="task", trace=trace,
            visible_ids=["user-1", "action-1"])


def test_segment_schemas_require_generate_evidence_but_allow_skip():
    for kind, schema in (("task", TASK_SEGMENT_SCHEMA), ("event", EVENT_SEGMENT_SCHEMA)):
        generate, skip = schema["schema"]["oneOf"]
        assert generate["properties"]["action"]["enum"] == ["generate"]
        assert {"action", "skill", "evidence"} <= set(generate["required"])
        assert generate["properties"]["evidence"]["properties"]["step_ids"]["minItems"] == 1
        assert skip["properties"]["action"]["enum"] == ["skip"]
        assert skip["required"] == ["action", "reason"]
        assert "skill" not in skip["properties"] and "evidence" not in skip["properties"]
        if kind == "task":
            context = generate["properties"]["candidate_context"]
            assert "candidate_context" in generate["required"]
            assert set(context["required"]) == {
                "task_goal", "whole_task_outcome", "hard_constraints",
                "environment_assumptions", "observed_results", "known_limitations"}
    trace = _trace()
    for kind, granularity in (("task", "general"), ("event", "event-driven")):
        valid = {"action": "generate", "skill": {"title": "Inspect a repair",
                 "granularity": granularity, "when_to_apply": "When a repair needs checking",
                 "rules": ["Check the observed result."]},
                 "evidence": {"step_ids": ["observe-1"]}}
        if kind == "task":
            valid["candidate_context"] = {"task_goal": "Repair the project",
                "whole_task_outcome": "completed", "hard_constraints": [],
                "environment_assumptions": [], "observed_results": [],
                "known_limitations": []}
        assert validate_segment_candidate(valid, kind=kind, trace=trace,
            visible_ids=["user-1", "action-1", "observe-1"])["action"] == "generate"
        without_evidence = {key: item for key, item in valid.items() if key != "evidence"}
        with pytest.raises(ValueError, match="evidence.step_ids"):
            validate_segment_candidate(without_evidence, kind=kind, trace=trace,
                visible_ids=["user-1", "action-1", "observe-1"])
        assert validate_segment_candidate({"action": "skip", "reason": "No reusable lesson"},
            kind=kind, trace=trace, visible_ids=[])["action"] == "skip"


def test_sop_requests_exclude_raw_trajectory_and_validate_conditional_sources():
    selected = []
    for name in ("alpha", "beta", "gamma"):
        selected.append({"canonical_instance_id": name, "candidate_id": f"sop-{name}-full-id",
            "skill": {"title": f"SOP {name}", "granularity": "task",
                      "when_to_apply": f"When {name} applies", "rules": [f"Use {name} conditionally."]},
            "candidate_context": {"task_goal": f"Goal {name}", "whole_task_outcome": "unknown",
                                  "hard_constraints": [], "environment_assumptions": [],
                                  "observed_results": [], "known_limitations": []},
            "official_task_outcome": {"official_reward": 0},
            "evidence": {"step_ids": ["RAW_ONLY_MARKER"]},
            "trajectory_ref": {"path": "/RAW_ONLY_MARKER/trace.json"}})
    pair = task_sop_pairing_messages(selected[0], selected[1:], prompt="pair SOPs")
    merge = task_sop_merge_messages(selected, prompt="merge SOPs")
    for messages in (pair, merge):
        payload = messages[1]["content"]
        assert "RAW_ONLY_MARKER" not in payload
        assert "trajectory_ref" not in payload and "rule_evidence" not in payload
        assert "sop-alpha-full-id" in payload and "Goal alpha" in payload
        assert "official_task_outcome" in payload
    assert len(merge[1]["content"]) < 2500
    generated, skipped = TASK_SOP_MERGE_SCHEMA["schema"]["oneOf"]
    assert {"action", "skill", "evidence"} <= set(generated["required"])
    assert skipped["required"] == ["action", "reason"]
    value = {"action": "generate", "skill": {"title": "Conditional shared method",
             "granularity": "general", "when_to_apply": "When either condition is present",
             "rules": ["Use alpha under its condition.", "Use beta under its condition."]},
             "evidence": {"source_candidate_ids": ["sop-alpha-full-id", "sop-beta-full-id"]}}
    checked = validate_task_sop_merge(value, selected)
    assert checked["evidence"] == value["evidence"]
    assert validate_task_sop_merge({"action": "skip", "reason": "No shared method"}, selected)["action"] == "skip"
    with pytest.raises(ValueError, match="source_candidate_ids"):
        validate_task_sop_merge({key: item for key, item in value.items() if key != "evidence"}, selected)
    unknown = json.loads(json.dumps(value))
    unknown["evidence"]["source_candidate_ids"][1] = "sop-bet"
    with pytest.raises(ValueError, match="unknown source SOP"):
        validate_task_sop_merge(unknown, selected)


class _NoCandidateService:
    def d01_plan(self, state):
        return {"description_segments": [], "outcome": {"status": "ok", "mode": "full"}}

    def d01(self, state):
        return {"description_records": [], "outcome": {"status": "rejected"}}

    def evidence_plan(self, state):
        return {"evidence_segments": [{"steps": [], "step_ids": []}], "evidence_index": 0,
                "generation_results": [], "task_candidate_records": [], "outcome": {"status": "ok"}}

    def generate(self, state):
        return {"generation_results": [{"status": "skip", "segment_index": 0}],
                "outcome": {"status": "skip"}}

    def save_candidate(self, state):
        raise AssertionError("candidate must not be saved")

    def rank(self, state):
        raise AssertionError("rank must not run")

    def pair(self, state):
        raise AssertionError("pair must not run")

    def merge(self, state):
        raise AssertionError("merge must not run")

    def check_event_receipt(self, state, receipt):
        assert receipt["H0"] == state["frozen_bank_hash"]
        return {"receipt": receipt, "outcome": {"status": "ok"}}

    def fig9(self, state):
        assert state["event_receipt"]["HE"] == "he"
        return {"task_receipt": {"HE": "he", "HT": "he", "task_operations": []},
                "outcome": {"status": "no_op"}}

    def check_publication_receipt(self, state, receipt):
        assert receipt["HM"] == "hm"
        return {"receipt": receipt, "outcome": {"status": "ok"}}


def test_two_interrupts_survive_fresh_process_objects():
    identity = {"round_id": 1, "trial_id": "r1:test", "task_id": "build-pmars",
                "trace_sha256": "trace", "historical_thinking_policy": "keep",
                "graph_version": 2}
    initial = {"schema_version": 2, "thread_id": task_thread_id(identity),
               "identity": identity, "frozen_bank_hash": "h0", "outcomes": {},
               "verifier_status": "not_run"}
    with tempfile.TemporaryDirectory() as root:
        first = TaskGraphRunner(directory=Path(root), service=_NoCandidateService())
        state = first.invoke(identity=identity, initial=initial)
        assert state["stage"] == "generate"
        assert state["outcomes"]["d01"]["status"] == "rejected"
        second = TaskGraphRunner(directory=Path(root), service=_NoCandidateService())
        state = second.invoke(identity=identity, receipt={"H0": "h0", "HE": "he"})
        assert state["task_receipt"]["task_operations"] == []
        third = TaskGraphRunner(directory=Path(root), service=_NoCandidateService())
        state = third.invoke(identity=identity, receipt={"HM": "hm"})
        assert state["stage"] == "publication_confirmed"
        assert state["verifier_status"] == "not_run"


def _operation(operation_id, granularity, source_kind):
    source_id = "build-pmars"
    return {"operation_id": operation_id, "decision": "add",
            "candidate": {"title": operation_id, "granularity": granularity,
                          "when_to_apply": "When repairing a task",
                          "rules": ["Inspect and verify."],
                          "benchmark": "terminal-bench",
                          "provenance": {"source_instance_ids": [source_id],
                                         "source_instance_ids_raw": [source_id],
                                         "parent_skill_ids": []}},
            "source_instance_ids": [source_id], "evidence": {},
            "source_kind": source_kind}


def _publication_state(*, h0, hm, operations, bank):
    published = {"before_bank_state_sha256": h0,
                 "after_bank_state_sha256": hm,
                 "operations": [{"operation_id": op["operation_id"]} for op in operations],
                 "manager_decisions": []}
    payload = {"round_id": 1, "task_id": "build-pmars",
               "operations": operations, "manager_decisions": []}
    journal = {"operation_id": "publish:r1:build-pmars",
               "payload_sha256": sha256_text(canonical_json(payload)),
               "result": published}
    return {"rounds": {"1": {
        "assignments": {"build-pmars": {"publication": published}},
        "operations": [journal], "bank": bank.to_dict()}}}


class _ReceiptService(_NoCandidateService):
    def __init__(self, root):
        self.artifact_root = root
        self.frozen_bank = SkillBank.empty("terminal-bench")
        self.require_durable_extraction = False

    check_event_receipt = TaskGraphStages.check_event_receipt
    fig9 = TaskGraphStages.fig9
    check_publication_receipt = TaskGraphStages.check_publication_receipt
    _artifact = TaskGraphStages._artifact
    _read = staticmethod(TaskGraphStages._read)
    _replay = staticmethod(TaskGraphStages._replay)


def test_multiple_event_operations_and_saved_publication_resume():
    identity = {"round_id": 1, "trial_id": "r1:C:build-pmars",
                "task_id": "build-pmars", "trace_sha256": "trace",
                "historical_thinking_policy": "keep", "graph_version": 2}
    thread_id = task_thread_id(identity)
    with tempfile.TemporaryDirectory() as root_name:
        root = Path(root_name)
        graph_dir = root / "task-graph"
        service = _ReceiptService(root)
        h0 = service.frozen_bank.snapshot()["state_sha256"]
        initial = {"schema_version": 2, "thread_id": thread_id,
                   "identity": identity, "frozen_bank_hash": h0,
                   "outcomes": {}, "verifier_status": "not_run"}
        first = TaskGraphRunner(directory=graph_dir, service=service)
        assert first.invoke(identity=identity, initial=initial)["stage"] == "generate"
        event_ops = [_operation("event-1", "event", "extraction"),
                     _operation("event-2", "event", "extraction")]
        event_bank = SkillBank.from_dict(service.frozen_bank.to_dict())
        service._replay(event_bank, event_ops)
        he = event_bank.snapshot()["state_sha256"]
        second = TaskGraphRunner(directory=graph_dir, service=_ReceiptService(root))
        staged = second.invoke(identity=identity, receipt={
            "thread_id": thread_id, "H0": h0, "HE": he,
            "event_operations": event_ops})
        assert staged["task_receipt"]["HT"] == he
        assert staged["task_receipt"]["task_operations"] == []
        assert staged["outcomes"]["fig9"]["status"] == "no_op"
        state_path = root / "durable-state.json"
        write_json(state_path, _publication_state(
            h0=h0, hm=he, operations=event_ops, bank=event_bank))
        receipt = {"thread_id": thread_id, "H0": h0, "HE": he, "HT": he,
                   "HM": he, "maintenance_operations": [],
                   "ordered_operation_ids": [item["operation_id"] for item in event_ops],
                   "durable_state_path": str(state_path)}
        third = TaskGraphRunner(directory=graph_dir,
                                service=PublicationReceiptService())
        confirmed = third.invoke(identity=identity, receipt=receipt)
        assert confirmed["stage"] == "publication_confirmed"
        assert len(confirmed["publication_receipt"]["ordered_operation_ids"]) == 2
        assert third.invoke(identity=identity, receipt=receipt) == confirmed


def test_receipt_checks_ordered_event_task_maintenance_and_durable_hash():
    with tempfile.TemporaryDirectory() as root_name:
        root = Path(root_name)
        service = _ReceiptService(root)
        h0 = service.frozen_bank.snapshot()["state_sha256"]
        event_ops = [_operation("e1", "event", "extraction"),
                     _operation("e2", "event", "extraction")]
        task_ops = [_operation("t1", "task", "extraction"),
                    _operation("t2", "task", "extraction")]
        maintenance = [_operation("m1", "task", "maintenance")]
        bank = SkillBank.from_dict(service.frozen_bank.to_dict())
        service._replay(bank, event_ops)
        he = bank.snapshot()["state_sha256"]
        service._replay(bank, task_ops)
        ht = bank.snapshot()["state_sha256"]
        task_bank = service._artifact("task-staged-bank.json", bank.to_dict())
        service._replay(bank, maintenance)
        hm = bank.snapshot()["state_sha256"]
        state_path = root / "durable-state.json"
        all_ops = event_ops + task_ops + maintenance
        write_json(state_path, _publication_state(
            h0=h0, hm=hm, operations=all_ops, bank=bank))
        state = {"identity": {"round_id": 1, "task_id": "build-pmars"},
                 "thread_id": "thread", "task_receipt": {
                     "H0": h0, "HE": he, "HT": ht,
                     "event_operations": event_ops, "task_operations": task_ops,
                     "task_bank": task_bank}}
        receipt = {"thread_id": "thread", "H0": h0, "HE": he, "HT": ht,
                   "HM": hm, "maintenance_operations": maintenance,
                   "ordered_operation_ids": [op["operation_id"] for op in all_ops],
                   "durable_state_path": str(state_path)}
        checker = PublicationReceiptService()
        assert checker.check_publication_receipt(state, receipt)["outcome"]["status"] == "ok"
        with pytest.raises(ValueError, match="order differs"):
            checker.check_publication_receipt(state, {
                **receipt, "ordered_operation_ids": list(reversed(receipt["ordered_operation_ids"]))})
        with pytest.raises(ValueError, match="final hash differs"):
            checker.check_publication_receipt(state, {**receipt, "HM": h0})
        corrupted = _publication_state(h0=h0, hm=hm, operations=all_ops, bank=bank)
        corrupted["rounds"]["1"]["operations"][0]["payload_sha256"] = "0" * 64
        write_json(state_path, corrupted)
        with pytest.raises(ValueError, match="journal differs"):
            checker.check_publication_receipt(state, receipt)


def test_official_chat_adapter_keeps_exact_wire_and_replays_without_resend():
    sends = []

    def respond(request):
        sends.append(request.content)
        body = {"id": "offline-call", "object": "chat.completion", "model": "local",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": '{"action":"skip","reason":"no method"}',
                    "reasoning_content": "not a visible answer"}}],
                "usage": {"prompt_tokens": 31, "completion_tokens": 5, "total_tokens": 36}}
        return httpx.Response(200, json=body)

    schema = {"name": "offline", "schema": {"type": "object", "properties": {
        "action": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["action", "reason"], "additionalProperties": False}}
    with tempfile.TemporaryDirectory() as root:
        boundary = TaskChatBoundary(
            root=Path(root), base_url="http://offline.local/v1", model="local",
            token_counter=lambda messages, *, request_options: 31,
            transport_factory=lambda: httpx.MockTransport(respond),
        )
        args = {"thread_id": "thread", "stage": "generation",
                "messages": [{"role": "user", "content": "test"}],
                "schema": schema, "identity": {"trace_sha256": "t", "policy": "keep"}}
        first = boundary.call(**args)
        assert first.status == "ok", first.reason
        assert first.value == {"action": "skip", "reason": "no method"}
        payload = json.loads(sends[0])
        assert payload["max_tokens"] == 16384
        assert "max_completion_tokens" not in payload
        assert payload["reasoning_effort"] == "max"
        assert payload["response_format"]["type"] == "json_schema"
        assert json.loads((first.call_dir / "wire-response.json").read_text())[
            "choices"][0]["message"]["reasoning_content"] == "not a visible answer"
        second = boundary.call(**args)
        assert second.status == "ok" and len(sends) == 1


def test_sent_without_response_is_uncertain_and_never_resent():
    sends = []

    def fail_after_send(request):
        sends.append(request.content)
        raise RuntimeError("connection lost after request handoff")

    schema = {"name": "offline", "schema": {"type": "object",
              "properties": {"action": {"type": "string"}},
              "required": ["action"], "additionalProperties": False}}
    with tempfile.TemporaryDirectory() as root:
        boundary = TaskChatBoundary(
            root=Path(root), base_url="http://offline.local/v1", model="local",
            token_counter=lambda messages, *, request_options: 10,
            transport_factory=lambda: httpx.MockTransport(fail_after_send),
        )
        args = {"thread_id": "thread", "stage": "generation",
                "messages": [{"role": "user", "content": "test"}],
                "schema": schema, "identity": {"trace_sha256": "t"}}
        first = boundary.call(**args)
        assert first.status == "transport_uncertain"
        assert (first.call_dir / "sent.json").is_file()
        assert not (first.call_dir / "wire-response.json").exists()
        second = boundary.call(**args)
        assert second.status == "transport_uncertain"
        assert len(sends) == 1
