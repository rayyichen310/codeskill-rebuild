"""Capture a secret-free, bounded readiness audit for the approved <MODEL_SERVICE_HOST> job.

This script is intended to run on <MODEL_SERVICE_HOST>.  It inspects the requested Slurm
job, its serving script/log, and the two configured HTTP endpoints.  The one
small chat completion is a readiness probe only; it is never a Terminal-Bench
trial and is recorded as such.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _run(*command: str, timeout: int = 30) -> dict[str, Any]:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        return {"command": list(command), "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"command": list(command), "returncode": None, "stdout": "", "stderr": f"{type(error).__name__}: {error}"}


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _http(method: str, url: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(url, data=body, headers={"Content-Type": "application/json"} if body is not None else {}, method=method)
    status: int | None = None
    raw = ""
    error: str | None = None
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        status = exc.code
        raw = exc.read().decode("utf-8", errors="replace")
        error = f"HTTPError: {exc}"
    except (OSError, URLError, TimeoutError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    return {"method": method, "url": url, "status": status, "body": parsed, "raw_body": raw, "error": error}


def _fields(scontrol_output: str) -> dict[str, str]:
    return {key: value for key, value in re.findall(r"(?:^|\n|\s)([A-Za-z][A-Za-z0-9]*)=([^\s]+)", scontrol_output)}


def _probe_summary(response: dict[str, Any]) -> dict[str, Any]:
    body = response.get("body")
    if not isinstance(body, dict):
        return {"status": response.get("status"), "error": response.get("error"), "body": body}
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    choices = body.get("choices") if isinstance(body.get("choices"), list) else []
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) and isinstance(choices[0].get("message"), dict) else {}
    return {
        "status": response.get("status"),
        "error": response.get("error"),
        "id": body.get("id"),
        "model": body.get("model"),
        "finish_reason": choices[0].get("finish_reason") if choices and isinstance(choices[0], dict) else None,
        "content": message.get("content"),
        "reasoning_content_present": bool(message.get("reasoning_content")),
        "reasoning_content_chars": len(message.get("reasoning_content", "")) if isinstance(message.get("reasoning_content"), str) else 0,
        "usage": usage,
        "metadata": body.get("metadata"),
    }


def _seconds_until(end_time: str | None) -> int | None:
    if not isinstance(end_time, str):
        return None
    try:
        end = datetime.fromisoformat(end_time)
    except ValueError:
        return None
    # Slurm prints this field in the host's local timezone.  The audit runs on
    # that same host, so a naive local comparison is the least assumptive one.
    return max(0, int((end - datetime.now()).total_seconds()))


def audit(job_id: str, output: Path, *, tokenizer_text: str, probe_text: str) -> dict[str, Any]:
    captured_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    job = _run("scontrol", "show", "job", job_id, "-dd")
    fields = _fields(job["stdout"])
    queue = _run("squeue", "-j", job_id, "-h", "-o", "%.18i %.10P %.24j %.10u %.2t %.12M %.12l %.12L %R")
    ps = _run("ps", "-ef")
    ports = _run("ss", "-ltnp")
    command_path = Path(fields["Command"]) if fields.get("Command") else None
    log_path = Path(fields["StdOut"]) if fields.get("StdOut") else None
    script_text = command_path.read_text(encoding="utf-8", errors="replace") if command_path and command_path.is_file() else ""
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path and log_path.is_file() else ""
    main_models = _http("GET", "http://127.0.0.1:31000/v1/models")
    main_health = _http("GET", "http://127.0.0.1:31000/health")
    summary_models = _http("GET", "http://127.0.0.1:30002/v1/models")
    summary_health = _http("GET", "http://127.0.0.1:30002/health")
    tokenizer = _http(
        "POST",
        "http://127.0.0.1:31000/tokenize",
        {"model": "deepseek-ai/DeepSeek-V4-Flash", "prompt": tokenizer_text},
    )
    completion_payload = {
        "model": "deepseek-ai/DeepSeek-V4-Flash",
        "messages": [{"role": "user", "content": probe_text}],
        "temperature": 1.0,
        "top_p": 0.95,
        "reasoning_effort": "max",
        "max_tokens": 8,
        "stream": False,
    }
    completion = _http("POST", "http://127.0.0.1:31000/v1/chat/completions", completion_payload, timeout=120)
    value = {
        "schema_version": 1,
        "kind": "r015_gpu202_preparation_audit",
        "captured_at_utc": captured_at,
        "scope": "bounded readiness probe; no Terminal-Bench trial, no Harbor solver, no verifier",
        "host": {"hostname": os.uname().nodename, "uid": os.getuid(), "user": _run("id", "-un")["stdout"].strip()},
        "slurm": {
            "job_id": job_id,
            "job_command": job["command"],
            "job_query": job,
            "job_fields": fields,
            "queue_query": queue,
            "state": fields.get("JobState"),
            "run_time": fields.get("RunTime"),
            "time_limit": fields.get("TimeLimit"),
            "end_time": fields.get("EndTime"),
            "time_left_seconds_at_capture": _seconds_until(fields.get("EndTime")),
            "node": fields.get("NodeList"),
            "port_and_process_observation": {"ps": ps, "ss": ports},
        },
        "serving": {
            "main": {"health": main_health, "models": main_models},
            "summarizer": {"health": summary_health, "models": summary_models},
            "tokenizer": {key: value for key, value in tokenizer.items() if key != "raw_body"},
            "readiness_completion_request": completion_payload,
            "readiness_completion": _probe_summary(completion),
        },
        "files": {
            "batch_script": {"path": str(command_path) if command_path else None, "sha256": _sha256(command_path) if command_path else None},
            "server_log": {"path": str(log_path) if log_path else None, "sha256": _sha256(log_path) if log_path else None, "tail": log_text.splitlines()[-80:]},
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", default="501")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tokenizer-text", default="C-only preparation tokenizer probe")
    parser.add_argument("--probe-text", default="Reply with exactly READY for a serving readiness probe.")
    args = parser.parse_args()
    value = audit(args.job_id, args.output, tokenizer_text=args.tokenizer_text, probe_text=args.probe_text)
    print(json.dumps({"output": str(args.output), "job_state": value["slurm"]["state"], "time_left": value["slurm"]["job_fields"].get("TimeLeft"), "completion_status": value["serving"]["readiness_completion"]["status"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
