"""Fresh interpreters must reuse Event wire, Store, and protocol records."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_event_graph_and_protocol_recover_across_processes(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    helper = repo / "tests" / "support_event_graph_real_process.py"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(repo), str(repo / "src")])
    phases = [("start", 71), ("response_crash", 72), ("store_crash", 75),
              ("resume_event", 0), ("publish_crash", 73), ("finish", 0)]
    for phase, expected in phases:
        process = subprocess.run([sys.executable, str(helper), str(tmp_path), phase],
                                 cwd=repo, env=env, capture_output=True, text=True,
                                 timeout=50)
        assert process.returncode == expected, (
            phase, process.returncode, process.stdout, process.stderr)
    assert json.loads((tmp_path / "http-sends.json").read_text(encoding="utf-8")) == {
        "generation": 1}
    assert json.loads((tmp_path / "store-puts.json").read_text(encoding="utf-8")) == 1
    calls = list((tmp_path / "calls" / "task-calls").iterdir())
    assert len(calls) == 1
    assert all((path / "wire-request.json").is_file() and
               (path / "wire-response.json").is_file() for path in calls)
    durable = json.loads((tmp_path / "protocol.json").read_text(encoding="utf-8"))
    round_state = durable["rounds"]["1"]
    journal = [item["operation_id"] for item in round_state["operations"]]
    assert journal.count("publish:r1:build-pmars") == 1
    assert journal.count("complete:r1:build-pmars") == 1
    assert round_state["bank"]["sequence"] == 1
    assert [item["operation_id"] for item in round_state["bank"]["operations"]] == ["event-op"]
