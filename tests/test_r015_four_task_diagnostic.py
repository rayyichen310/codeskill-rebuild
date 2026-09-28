"""Bounded first-round diagnostic through the production C-only coordinator."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from codeskill_rebuild.c_only_protocol import COnlyProtocol, COnlyProtocolError
from codeskill_rebuild.types import read_json, sha256_file
from scripts.run_r015_four_task_diagnostic import TASK_IDS, prepare, run


ROOT = Path(__file__).resolve().parents[1]
SOURCE_CONFIG = ROOT / "configs" / "r015-c-only-coding.json"
SOURCE_BASELINE = ROOT / "docs" / "baselines" / "r015-legacy-coding-baseline-20260913.json"


def _controlled_driver(path: Path, log: Path) -> None:
    path.write_text('''import hashlib, json, sys
from pathlib import Path
args = sys.argv
source = Path(args[args.index("--input") + 1])
output = Path(args[args.index("--output") + 1])
assignment = json.loads(source.read_text(encoding="utf-8"))["assignment"]
task_id = assignment["task_id"]
round_id = assignment["round_id"]
with Path(r"""%s""").open("a", encoding="utf-8") as stream:
    stream.write(f"{round_id}:{task_id}\\n")
session_id = f"controlled-r{round_id}-{task_id}"
trace_path = output.parent / "trajectory.json"
trace = {"r015_binding": {"round_id": round_id, "task_id": task_id,
         "trial_id": assignment["trial_id"], "session_id": session_id},
         "source": {"canonical_instance_id": task_id}}
trace_path.write_text(json.dumps(trace, sort_keys=True), encoding="utf-8")
ref = {"round_id": round_id, "task_id": task_id,
       "trial_id": assignment["trial_id"], "session_id": session_id,
       "complete": True, "path": str(trace_path),
       "sha256": hashlib.sha256(trace_path.read_bytes()).hexdigest()}
raw = {"official_harbor_trial": True, "condition": "C-only",
       "round_id": round_id, "task_id": task_id,
       "trial_id": assignment["trial_id"], "session_id": session_id,
       "historical_baseline_used": False}
trial = {"condition": "C-only", "round_id": round_id, "task_id": task_id,
         "trial_id": assignment["trial_id"], "outcome": "completed",
         "trajectory": ref, "supplied_skills": [], "raw_evidence": raw}
extraction = {"condition": "C-only", "round_id": round_id, "task_id": task_id,
              "decision": "skip", "reason": "controlled diagnostic fixture",
              "candidates": [], "trajectory_ref": ref,
              "description_records": [], "evidence": {"kind": "controlled-diagnostic"}}
publication = {"condition": "C-only", "round_id": round_id,
               "task_id": task_id, "operations": [], "manager_decisions": []}
value = {"schema_version": 1, "kind": "r015_c_only_trial_driver_output",
         "condition": "C-only", "round_id": round_id, "task_id": task_id,
         "trial": trial, "event_attempts": [], "extraction": extraction,
         "publication": publication,
         "official_process": {"classification": "controlled",
                              "official_trial_boundary_started": True},
         "historical_baseline_used": False, "evidence_mode": "controlled_fixture"}
output.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
''' % str(log).replace("\\", "\\\\"), encoding="utf-8")


def test_exact_four_first_round_trials_stop_before_second_round(tmp_path):
    run_dir = tmp_path / "diagnostic"
    prepared = prepare(source_config=SOURCE_CONFIG, source_baseline=SOURCE_BASELINE,
                       run_dir=run_dir, runtime_approval_reference="controlled-user-decision")
    assert prepared["task_order"] == list(TASK_IDS)
    assert prepared["max_trials"] == 4
    assert sha256_file(SOURCE_CONFIG) == prepared["source_config"]["sha256"]
    assert sha256_file(SOURCE_BASELINE) == prepared["source_baseline"]["sha256"]
    driver = tmp_path / "controlled-driver.py"
    log = tmp_path / "invocations.log"
    _controlled_driver(driver, log)
    with pytest.raises(COnlyProtocolError, match="explicit start"):
        run(run_dir=run_dir, trial_driver=driver, confirm_user_start=False,
            accept_runtime_deviation=True, allow_test_fixture=True)
    result = run(run_dir=run_dir, trial_driver=driver, confirm_user_start=True,
                 accept_runtime_deviation=True, allow_test_fixture=True)
    assert result["status"] == "diagnostic_first_round_complete"
    assert result["completed_tasks"] == list(TASK_IDS)
    assert result["round_2_status"] == "not_started"
    assert log.read_text(encoding="utf-8").splitlines() == [f"1:{task}" for task in TASK_IDS]
    protocol = COnlyProtocol.load(run_dir / "state.json", run_dir / "diagnostic-config.json",
                                  run_dir / "diagnostic-baseline-metadata.json")
    assert protocol.current_round_id == 1
    assert protocol.current_task_id is None
    assert protocol.state["rounds"]["2"]["status"] == "not_started"
    assert protocol.state["formal_campaign"] == "active"  # protocol field, diagnostic run not marked formal-complete
    assert read_json(run_dir / "diagnostic-completion.json")["completed_tasks"] == list(TASK_IDS)
    again = run(run_dir=run_dir, trial_driver=driver, confirm_user_start=True,
                accept_runtime_deviation=True, allow_test_fixture=True)
    assert again == result
    assert log.read_text(encoding="utf-8").splitlines() == [f"1:{task}" for task in TASK_IDS]
