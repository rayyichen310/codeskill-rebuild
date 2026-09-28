"""Interpreter restarts at fixture and production Task recovery boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codeskill_rebuild.types import sha256_file, write_json


def test_task_graph_crash_windows_recover_across_processes(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    helper = repo / "tests" / "support_task_graph_process.py"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(repo), str(repo / "src")])
    phases = [
        ("start", 71),                # whole-input plan, before checkpoint/generation
        ("response_crash", 72),       # raw generation response written, before checkpoint
        ("store_crash", 75),          # Store put, before save_candidate checkpoint
        ("resume_event", 0),         # same candidate, no second model request or Store conflict
        ("event", 0),                # first handoff and Task bank operation staged
        ("publish_crash", 73),       # durable bank/journal save, before Graph confirmation
        ("confirm_crash", 74),       # Graph confirmation, before outer finish
        ("finish", 0),               # checkpoint replay, no second bank publication
    ]
    for phase, expected in phases:
        process = subprocess.run(
            [sys.executable, str(helper), str(tmp_path), phase],
            cwd=repo, env=env, capture_output=True, text=True, timeout=40,
        )
        assert process.returncode == expected, (
            phase, process.returncode, process.stdout, process.stderr)
    counts = json.loads((tmp_path / "calls" / "model-calls.json").read_text(encoding="utf-8"))
    assert counts["generation-000"] == 1
    assert sum(counts.values()) == 1
    assert json.loads((tmp_path / "store-puts.json").read_text(encoding="utf-8")) == 1
    assert json.loads((tmp_path / "finish.json").read_text(encoding="utf-8"))["stage"] == "publication_confirmed"


def test_production_task_call_and_protocol_publication_recover_across_processes(tmp_path):
    """Only the external HTTP response is controlled; recovery uses production APIs."""
    repo = Path(__file__).resolve().parents[1]
    helper = repo / "tests" / "support_task_graph_real_process.py"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(repo), str(repo / "src")])
    phases = [
        ("start", 71),           # whole-input plan, before graph checkpoint
        ("response_crash", 72),  # real TaskChatBoundary response saved, before checkpoint
        ("store_crash", 75),     # candidate saved in real SQLite Store, before checkpoint
        ("resume_event", 0),    # call and Store replay without another send/write
        ("event", 0),           # real durable extraction and first Graph handoff
        ("publish_crash", 73),  # real protocol publish/save, before Graph confirmation
        ("confirm_crash", 74),  # real Graph confirmation, before protocol finish
        ("finish", 0),          # real finish_task/save after a fresh process restart
    ]
    for phase, expected in phases:
        process = subprocess.run(
            [sys.executable, str(helper), str(tmp_path), phase],
            cwd=repo, env=env, capture_output=True, text=True, timeout=50,
        )
        assert process.returncode == expected, (
            phase, process.returncode, process.stdout, process.stderr)
    assert json.loads((tmp_path / "http-sends.json").read_text(encoding="utf-8")) == {
        "d01": 1, "generation": 1,
    }
    assert json.loads((tmp_path / "store-puts.json").read_text(encoding="utf-8")) == 1
    call_dirs = list((tmp_path / "calls" / "task-calls").iterdir())
    assert len(call_dirs) == 2
    assert all((path / "wire-request.json").is_file() and
               (path / "wire-response.json").is_file() and
               (path / "http.json").is_file() for path in call_dirs)
    durable = json.loads((tmp_path / "protocol.json").read_text(encoding="utf-8"))
    round_state = durable["rounds"]["1"]
    journal_ids = [item["operation_id"] for item in round_state["operations"]]
    assert journal_ids.count("publish:r1:build-pmars") == 1
    assert journal_ids.count("complete:r1:build-pmars") == 1
    assert round_state["bank"]["sequence"] == 1
    assert [item["operation_id"] for item in round_state["bank"]["operations"]] == ["event-op"]
    assert list(round_state["completed_tasks"]) == ["build-pmars"]


def test_d01_summary_saved_response_and_bundle_resume_without_resend(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    helper = repo / "tests" / "support_task_d01_summary_process.py"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(repo), str(repo / "src")])
    for phase, expected in (("start", 76), ("bundle_crash", 77), ("finish", 0)):
        process = subprocess.run([sys.executable, str(helper), str(tmp_path), phase],
                                 cwd=repo, env=env, capture_output=True, text=True, timeout=50)
        assert process.returncode == expected, (
            phase, process.returncode, process.stdout, process.stderr)
    sends = json.loads((tmp_path / "http-sends.json").read_text(encoding="utf-8"))
    assert sends["d01_summary"] == 2
    assert sends["d01"] == 1
    summary_calls = [path for path in (tmp_path / "calls" / "task-calls").iterdir()
                     if json.loads((path / "identity.json").read_text(encoding="utf-8"))
                     .get("stage") == "d01-summary-000"]
    assert len(summary_calls) == 1
    assert (summary_calls[0] / "wire-response.json").is_file()
    assert len([path for path in (tmp_path / "calls" / "task-calls").iterdir()
                if json.loads((path / "identity.json").read_text(encoding="utf-8"))
                .get("stage") == "d01-summary-001"]) == 1


def test_d01_uncertain_summary_is_not_automatically_resent(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    helper = repo / "tests" / "support_task_d01_summary_process.py"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(repo), str(repo / "src")])
    for phase in ("uncertain_start", "uncertain_resume"):
        process = subprocess.run([sys.executable, str(helper), str(tmp_path), phase],
                                 cwd=repo, env=env, capture_output=True, text=True, timeout=50)
        assert process.returncode == 0, (
            phase, process.returncode, process.stdout, process.stderr)
    assert json.loads((tmp_path / "http-sends.json").read_text(encoding="utf-8")) == {
        "d01_summary": 1}


@pytest.mark.parametrize("mutation", ["request_body", "stage", "source", "incomplete_response"])
def test_d01_bundle_rejects_changed_saved_summary_input(tmp_path, mutation):
    from tests.support_task_d01_summary_process import SummaryCrashStages
    from tests.support_task_graph_real_process import _setup

    _setup(tmp_path)
    service = SummaryCrashStages(tmp_path, None)
    state = service.initial_state()
    plan = service.d01_plan(state)
    state.update({key: value for key, value in plan.items() if key != "outcome"})
    while state["description_index"] < len(state["description_segments"]):
        result = service.d01_segment(state)
        assert result["outcome"]["status"] == "ok"
        state.update({key: value for key, value in result.items() if key != "outcome"})
    ref = state["description_results"][0]
    part_path = Path(ref["path"])
    part = json.loads(part_path.read_text(encoding="utf-8"))
    call_dir = Path(part["call"]["request"]["path"]).parent
    if mutation == "request_body":
        wire_path = call_dir / "wire-request.json"
        wire = json.loads(wire_path.read_text(encoding="utf-8"))
        body = json.loads(wire["messages"][-1]["content"])
        body["segment_steps"] = []
        body["required_covered_step_ids"] = []
        wire["messages"][-1]["content"] = json.dumps(body)
        write_json(wire_path, wire)
        part["call"]["request"]["sha256"] = sha256_file(wire_path)
    elif mutation == "stage":
        identity_path = call_dir / "identity.json"
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        identity["stage"] = "different-stage"
        write_json(identity_path, identity)
    elif mutation == "source":
        service.projected_trace["steps"][0]["content"] = "a different source"
    else:
        response_path = call_dir / "wire-response.json"
        response = json.loads(response_path.read_text(encoding="utf-8"))
        response["choices"][0]["finish_reason"] = "length"
        write_json(response_path, response)
        http_path = call_dir / "http.json"
        http_record = json.loads(http_path.read_text(encoding="utf-8"))
        http_record["response_sha256"] = sha256_file(response_path)
        write_json(http_path, http_record)
        part["call"]["response"]["sha256"] = sha256_file(response_path)
        part["call"]["finish_reason"] = "length"
    if mutation in {"request_body", "incomplete_response"}:
        write_json(part_path, part)
        ref["sha256"] = sha256_file(part_path)
    result = service.d01_bundle(state)
    assert result["outcome"]["status"] == "rejected"
