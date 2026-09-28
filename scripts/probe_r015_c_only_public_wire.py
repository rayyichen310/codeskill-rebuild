#!/usr/bin/env python3
"""Run one bounded neutral public-adapter/sidecar wire probe.

This probe deliberately does not launch Harbor or a Terminal-Bench task.  It
creates a fresh, empty C-only assignment, renders the OpenClaw provider through
the public adapter, starts only the public R012 sidecar, and sends one small
OpenAI-compatible request to the live upstream endpoint.  The complete request,
visible response, proxy record, server usage, and immutable configuration refs
are retained under the requested evidence directory.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from codeskill_rebuild.c_only_protocol import COnlyProtocol  # noqa: E402
from codeskill_rebuild.types import canonical_json, read_json, sha256_file, sha256_text, utc_now, write_json  # noqa: E402
from scripts.run_r015_c_only import _build_config, _write_driver_input  # noqa: E402
from scripts.run_r015_c_only_harbor_driver import (  # noqa: E402
    COnlyHarborDriverError,
    _build_trial_context,
    _load_input,
    _sidecar_check,
    _task_root_and_service,
    _wait_listener,
)


class PublicWireProbeError(RuntimeError):
    """The neutral probe did not produce a complete live wire record."""


def _hash_payload(value: object) -> str:
    return sha256_text(canonical_json(value))


def _stop(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    except OSError:
        pass


def _seed_sqlite(path: Path, session_id: str) -> None:
    """Create the public native transcript table before sidecar startup."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE transcript_events (session_id TEXT NOT NULL, seq INTEGER NOT NULL, event_json TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO transcript_events(session_id, seq, event_json) VALUES (?, ?, ?)",
            (session_id, 0, json.dumps({"type": "session", "id": session_id}, separators=(",", ":"))),
        )
        connection.commit()
    finally:
        connection.close()


def _http_json(url: str, payload: dict[str, object], *, timeout: int) -> tuple[int, dict[str, str], bytes]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return int(response.status), {str(k).lower(): str(v) for k, v in response.headers.items()}, response.read()
    except Exception as error:  # retain HTTP error bodies below when available
        body = getattr(error, "read", lambda: b"")()
        status = int(getattr(error, "code", 0) or 0)
        headers = {str(k).lower(): str(v) for k, v in getattr(getattr(error, "headers", None), "items", lambda: [])()}
        if status:
            return status, headers, body
        raise PublicWireProbeError(f"public sidecar request failed: {type(error).__name__}: {error}") from error


def run(args: argparse.Namespace) -> dict[str, object]:
    output = args.output.resolve()
    evidence_root = output.with_suffix("")
    if evidence_root.exists():
        raise PublicWireProbeError(f"probe evidence directory already exists; refusing to overwrite: {evidence_root}")
    evidence_root.mkdir(parents=True, exist_ok=False)
    work = evidence_root / "work"
    work.mkdir()

    baseline = args.baseline.resolve()
    canonical_config = args.config.resolve() if args.config is not None else ROOT / "configs" / "r015-c-only-coding.json"
    config = read_json(canonical_config)
    if not isinstance(config, dict):
        raise PublicWireProbeError("C-only config is not an object")
    # A private config copy gives this bounded probe an isolated listener and
    # leaves the prepared formal config bytes untouched.
    driver = config.setdefault("driver", {})
    if not isinstance(driver, dict):
        raise PublicWireProbeError("C-only config.driver is not an object")
    driver["sidecar_port_base"] = int(args.port_base)
    probe_config = work / "config.json"
    write_json(probe_config, config)
    state_path = work / "state.json"
    protocol = COnlyProtocol.initialize(probe_config, baseline, state_path)
    assignment = protocol.freeze_task(protocol.tasks[0]["canonical_instance_id"])
    protocol.save(state_path)
    run_dir = work / "run"
    run_dir.mkdir()
    input_path = _write_driver_input(protocol, assignment, run_dir, state_path)
    output_path = run_dir / "driver-output.json"
    input_value = _load_input(input_path)
    paths, metadata, _task_path = _task_root_and_service(input_value)
    context = _build_trial_context(input_value, output_path=output_path, paths=paths, metadata=metadata)
    # OpenClaw normally creates this schema during agent bootstrap.  The probe
    # has no Harbor bootstrap, so seed only the empty native session table; no
    # solver/task/answer material is inserted.
    _seed_sqlite(context["database"], str(context["session_id"]))
    sidecar_check = _sidecar_check(context)
    stdout_path = evidence_root / "sidecar.stdout.log"
    stderr_path = evidence_root / "sidecar.stderr.log"
    sidecar: subprocess.Popen[str] | None = None
    status = 0
    headers: dict[str, str] = {}
    response_bytes = b""
    request_payload: dict[str, object] = {
        "model": context["target"]["model_id"],
        "messages": [{"role": "user", "content": "Return exactly READY."}],
        "temperature": float(context["target"]["temperature"]),
        "top_p": float(context["target"]["top_p"]),
        "extra_body": {"reasoning_effort": str(context["target"]["reasoning_effort"])},
        # Keep the neutral request bounded while recording the configured
        # provider maximum (81920) separately in effective_config.
        "max_tokens": 64,
        "stream": False,
    }
    request_started = time.monotonic()
    try:
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
            sidecar = subprocess.Popen(
                [str(context["python"]), str(context["sidecar_script"]), "--config", str(context["sidecar_config"])],
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", "")},
                stdout=stdout,
                stderr=stderr,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if not _wait_listener(sidecar, "127.0.0.1", int(context["port"]), timeout_seconds=args.startup_timeout):
                raise PublicWireProbeError("public sidecar did not become ready")
            status, headers, response_bytes = _http_json(
                f"http://127.0.0.1:{context['port']}/v1/chat/completions",
                request_payload,
                timeout=args.request_timeout,
            )
    finally:
        _stop(sidecar)
    request_elapsed = time.monotonic() - request_started

    attempt_path = context["sidecar_dir"] / "upstream_requests" / "attempt-0001.json"
    attempt = read_json(attempt_path) if attempt_path.is_file() else None
    response_text = response_bytes.decode("utf-8", errors="replace")
    try:
        response_value = json.loads(response_text)
    except json.JSONDecodeError:
        response_value = None
    effective = {
        "rendered_through": context["openclaw_binding_audit"]["rendered_through"],
        "provider_id": "codeskill-r012",
        "provider_base_url": context["openclaw_config"]["models"]["providers"]["codeskill-r012"]["baseUrl"],
        "model_id": context["target"]["model_id"],
        "thinking_default": context["openclaw_config"]["agents"]["defaults"]["thinkingDefault"],
        "model_params": context["openclaw_config"]["agents"]["defaults"]["models"][f"codeskill-r012/{context['target']['model_id']}"]["params"],
        "context_tokens": context["target"]["context_tokens"],
        "max_output_tokens": context["target"]["max_output_tokens"],
        "probe_request_max_tokens": request_payload["max_tokens"],
    }
    result: dict[str, object] = {
        "schema_version": 1,
        "kind": "r015_c_only_public_adapter_sidecar_wire_probe",
        "created_at_utc": utc_now(),
        "classification": "neutral_live_sidecar_wire; official Harbor solver not started",
        "condition": "C-only probe",
        "formal_campaign_started": False,
        "official_harbor_solver_started": False,
        "historical_baseline_used": False,
        "task_id": context["task_id"],
        "trial_id": context["trial_id"],
        "session_id": context["session_id"],
        "task_metadata": metadata,
        "effective_config": effective,
        "sidecar_check": sidecar_check,
        "request": {
            "url": f"http://127.0.0.1:{context['port']}/v1/chat/completions",
            "payload": request_payload,
            "payload_sha256": _hash_payload(request_payload),
            "elapsed_seconds": request_elapsed,
        },
        "response": {
            "http_status": status,
            "headers": headers,
            "raw": response_text,
            "raw_sha256": sha256_text(response_text),
            "json": response_value,
            "visible_text": (
                response_value.get("choices", [{}])[0].get("message", {}).get("content")
                if isinstance(response_value, dict) and isinstance(response_value.get("choices"), list) and response_value.get("choices") and isinstance(response_value["choices"][0], dict)
                else None
            ),
        },
        "proxy_attempt": {
            "path": str(attempt_path),
            "sha256": sha256_file(attempt_path) if attempt_path.is_file() else None,
            "record": attempt,
        },
        "artifacts": {
            "probe_config": {"path": str(probe_config), "sha256": sha256_file(probe_config)},
            "state": {"path": str(state_path), "sha256": sha256_file(state_path)},
            "sidecar_config": {"path": str(context["sidecar_config"]), "sha256": sha256_file(context["sidecar_config"])},
            "openclaw_host_config": {"path": str(context["host_config"]), "sha256": sha256_file(context["host_config"])},
            "native_database": {"path": str(context["database"]), "sha256": sha256_file(context["database"])},
            "sidecar_stdout": {"path": str(stdout_path), "sha256": sha256_file(stdout_path)},
            "sidecar_stderr": {"path": str(stderr_path), "sha256": sha256_file(stderr_path)},
        },
        "source_policy": "public CODESKILLHarborOpenClaw binding plus public R012 sidecar; no OpenClaw source mount, fork, dist patch, or monkeypatch",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, result)
    if status != 200 or not isinstance(attempt, dict) or attempt.get("proxy_outcome") != "stream_forwarded" or not isinstance(attempt.get("upstream_usage"), dict) or not isinstance(response_value, dict):
        raise PublicWireProbeError(f"neutral public wire probe did not produce a successful complete response; inspect {output}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port-base", type=int, default=19500)
    parser.add_argument("--startup-timeout", type=int, default=600)
    parser.add_argument("--request-timeout", type=int, default=900)
    args = parser.parse_args()
    try:
        result = run(args)
    except (PublicWireProbeError, COnlyHarborDriverError, OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise SystemExit(f"{type(error).__name__}: {error}") from error
    print(json.dumps({"status": "ok", "output": str(args.output.resolve()), "response_status": result["response"]["http_status"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
