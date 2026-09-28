"""Replay saved R015 manager JSON against the current full validators.

No manager request is sent. The historical response and trace files are only
read; derived evidence layouts and the comparison are written under --output.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codeskill_rebuild.pipeline import (  # noqa: E402
    normalize_extraction_evidence,
    validate_event_extraction_with_evidence,
    validate_task_candidate_with_evidence,
)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate(value: dict, trace: dict, kind: str) -> dict:
    validator = validate_task_candidate_with_evidence if kind == "task" else validate_event_extraction_with_evidence
    try:
        checked = validator(deepcopy(value), trace, benchmark="terminal-bench")
    except (TypeError, ValueError) as error:
        return {"status": "rejected", "error_type": type(error).__name__, "error": str(error)}
    return {"status": "passed", "action": checked["action"],
            "evidence_normalization": checked.get("evidence_normalization")}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnosis", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bash-bin", type=Path, help="directory containing a working bash executable for syntax checks")
    args = parser.parse_args()
    if args.bash_bin is not None:
        candidate = args.bash_bin / "bash.exe"
        if not candidate.is_file():
            raise ValueError(f"bash executable is absent: {candidate}")
        os.environ["PATH"] = str(args.bash_bin) + os.pathsep + os.environ["PATH"]
    diagnosis = read_json(args.diagnosis)
    if diagnosis.get("repo_revision") != "51d9521" or len(diagnosis.get("rows", [])) != 20:
        raise ValueError("unexpected historical diagnosis identity or row count")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    normalized_dir = output / "normalized-model-outputs"
    normalized_dir.mkdir(exist_ok=True)
    rows: list[dict] = []
    for historical in diagnosis["rows"]:
        response_path = Path(historical["response_path"])
        request_path = response_path.with_name("request.json")
        if sha256(response_path) != historical["response_sha256"]:
            raise ValueError(f"{historical['id']} response hash changed")
        if sha256(request_path) != historical["request_sha256"]:
            raise ValueError(f"{historical['id']} request hash changed")
        request = read_json(request_path)
        payload = json.loads(request["request"]["messages"][1]["content"])
        task_id = payload.get("trajectory", payload)["source"]["canonical_instance_id"]
        if task_id != historical["task"]:
            raise ValueError(f"{historical['id']} request task changed")
        source_path = args.diagnosis.parent.parent / "2026-09-20-codeskill-thinking-ab" / "source-traces" / f"{task_id}.json"
        if sha256(source_path) != historical["source_sha256"]:
            raise ValueError(f"{historical['id']} source trace hash changed")
        response = read_json(response_path)
        visible = response["parsed_response"]["choices"][0]["message"].get("content") or ""
        row = {key: historical[key] for key in ("id", "arm", "task", "kind", "response_path", "response_sha256", "request_sha256", "source_sha256", "finish_reason")}
        row["old_first_result"] = historical["original_validation"]
        row["visible_text_sha256"] = hashlib.sha256(visible.encode("utf-8")).hexdigest()
        try:
            model_output = json.loads(visible)
        except ValueError:
            row["classification"] = "length_or_incomplete_json"
            row["new_first_result"] = {"status": "no_complete_json"}
            rows.append(row)
            continue
        row["action"] = model_output.get("action")
        row["classification"] = model_output.get("action", "unknown")
        trace = read_json(source_path)
        row["new_first_result"] = validate(model_output, trace, historical["kind"])
        if model_output.get("action") == "generate":
            try:
                normalized, record = normalize_extraction_evidence(model_output, granularity=historical["kind"])
            except (TypeError, ValueError) as error:
                row["normalization_error"] = str(error)
            else:
                if record is not None:
                    normalized_path = normalized_dir / f"{row['id']}.json"
                    normalized_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    row["normalized_model_output"] = {"path": str(normalized_path), "sha256": sha256(normalized_path), "record": record}
        old = row["old_first_result"]
        new = row["new_first_result"]
        if old.get("status") == "rejected" and new.get("status") == "rejected" and old.get("error") != new.get("error"):
            row["changed_first_error"] = new["error"]
            if new["error"].startswith(("event outcome must follow a matching", "event response must be an assistant")):
                row["change_origin"] = "new_event_role_or_pairing_guard"
            else:
                row["change_origin"] = "existing_later_validation_unmasked"
                row["newly_exposed_error"] = new["error"]
        rows.append(row)
    classifications = {name: sum(row["classification"] == name for row in rows) for name in ("generate", "skip", "length_or_incomplete_json")}
    if classifications != {"generate": 18, "skip": 1, "length_or_incomplete_json": 1}:
        raise ValueError(f"unexpected replay classifications: {classifications}")
    result = {"schema_version": 1, "kind": "r015_saved_manager_validator_replay",
              "source_revision": "51d95219854227425b960339b6b07c0688689439",
              "current_checkout": str(ROOT), "diagnosis_path": str(args.diagnosis),
              "diagnosis_sha256": sha256(args.diagnosis), "classifications": classifications,
              "bash_executable": str(args.bash_bin / "bash.exe") if args.bash_bin is not None else None,
              "boundary": "Offline deterministic replay only; no model calls, semantic verifier, bank publication, or solver efficacy claim.",
              "rows": rows}
    destination = output / "replay-results.json"
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for row in rows:
        print(json.dumps({key: row.get(key) for key in ("id", "classification", "old_first_result", "new_first_result", "change_origin", "newly_exposed_error")}, ensure_ascii=False))
    print(f"Saved {destination}")


if __name__ == "__main__":
    main()
