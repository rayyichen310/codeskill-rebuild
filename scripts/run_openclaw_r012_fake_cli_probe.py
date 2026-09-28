"""Bounded real OpenClaw CLI / fake-transport SQLite compaction probe.

This is controlled integration evidence, not a solver, benchmark, tokenizer,
or real-model evaluation.  It runs four CLI invocations against one isolated
native SQLite session.  The fake transport returns one normal tool turn and
two complete turns, then a single genuine provider-style overflow response.
Any native compaction follow-up receives only an ordinary generic completion.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import shutil
import sys
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any

from codeskill_rebuild.openclaw_compaction import SqliteTranscriptCompactionDetector
from codeskill_rebuild.openclaw_native_summary import NativeSummaryPermitGate
from codeskill_rebuild.openclaw_overlay import DurableOverlay
from codeskill_rebuild.openclaw_proxy import DurableProxyService, UpstreamStream, serve_in_thread
from codeskill_rebuild.sqlite_compaction import _read_compactions
from codeskill_rebuild.types import read_json


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("run_root", type=Path, help="new directory for this bounded fake-only probe")
parser.add_argument(
    "--openclaw-root",
    type=Path,
    default=Path(os.environ.get("CODESKILL_OPENCLAW_ROOT", "<OPENCLAW_ROOT>")),
    help="read-only OpenClaw package root; defaults to CODESKILL_OPENCLAW_ROOT or the local source checkout",
)
parser.add_argument(
    "--node-image",
    default=os.environ.get("CODESKILL_NODE_IMAGE", "node:22-slim"),
    help="Docker Node image for this isolated CLI only",
)
parser.add_argument(
    "--openclaw-mount-root",
    type=Path,
    help="read-only host directory mounted for the package; defaults to --openclaw-root",
)
args = parser.parse_args()
if not isinstance(args.node_image, str) or not args.node_image or any(char.isspace() for char in args.node_image):
    raise SystemExit("--node-image must be a nonempty Docker image reference without whitespace")
root = args.run_root.resolve()
if root.exists():
    raise SystemExit(f"probe run_root must not already exist: {root}")
openclaw_root = args.openclaw_root.resolve()
if not (openclaw_root / "package.json").is_file() or not (openclaw_root / "openclaw.mjs").is_file():
    raise SystemExit(f"--openclaw-root is not a runnable OpenClaw package root: {openclaw_root}")
openclaw_mount_root = (args.openclaw_mount_root or openclaw_root).resolve()
if not openclaw_mount_root.is_dir():
    raise SystemExit(f"--openclaw-mount-root is not a directory: {openclaw_mount_root}")
try:
    openclaw_relative_root = openclaw_root.relative_to(openclaw_mount_root)
except ValueError as error:
    raise SystemExit("--openclaw-root must be contained by --openclaw-mount-root") from error
container_mount_root = PurePosixPath("/host-openclaw-test")
container_openclaw_root = container_mount_root / PurePosixPath(openclaw_relative_root.as_posix())
openclaw_package = json.loads((openclaw_root / "package.json").read_text(encoding="utf-8"))
if openclaw_package.get("name") != "openclaw" or not isinstance(openclaw_package.get("version"), str):
    raise SystemExit(f"--openclaw-root does not identify the official openclaw package: {openclaw_root}")
try:
    openclaw_release = tuple(int(part) for part in openclaw_package["version"].split(".", 2))
except ValueError as error:
    raise SystemExit(f"--openclaw-root has an unsupported package version format: {openclaw_package['version']!r}") from error
legacy_compaction_reserve_knobs = openclaw_release < (2026, 9, 0)
root.mkdir(parents=True, exist_ok=False)
home = root / "home"
state = home / ".openclaw"
workspace = root / "workspace"
for directory in (home, state, workspace):
    directory.mkdir(parents=True, exist_ok=True)

plugin_source = Path(__file__).resolve().parents[1] / "openclaw_plugin"
plugin_root = root / "plugin"
shutil.copytree(plugin_source, plugin_root)
(plugin_root / "node_modules").mkdir()
os.symlink(str(container_openclaw_root), plugin_root / "node_modules" / "openclaw")

session_id = "c0de0012-0000-4000-8000-000000000010"
request_count = 0
http_request_count = 0
# One deliberately long, synthetic assistant turn gives the native compactor
# actual historical content to summarize.  It is never a real model response.
controlled_history = "R012 synthetic retained history. " * 1_024


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def synthetic_count(payload: dict[str, Any]) -> int:
    """Transport-only accounting; it is never tokenizer evidence."""
    return max(1, len(json.dumps(payload)) // 4)


def session_location() -> dict[str, Any] | None:
    """Return the marker OpenClaw prints for the planned isolated CLI session.

    Current OpenClaw stores the active transcript in the agent SQLite database,
    but deliberately keeps the historical ``sessions.json`` path in its public
    ``sqlite:`` marker.  The JSON store need not exist on disk.  Constructing
    the exact planned marker before the first forwarded request lets the
    detector establish a baseline; its resolver then reads only the derived
    agent SQLite database.
    """
    store = state / "agents" / "main" / "sessions" / "sessions.json"
    return {"session_file": f"sqlite:main:{session_id}:{store}"}


def sse_response(*, request_ordinal: int, delta: dict[str, Any], finish_reason: str) -> bytes:
    initial = {
        "id": f"controlled-{request_ordinal}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "controlled",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    final = {
        "id": f"controlled-{request_ordinal}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "controlled",
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 16, "total_tokens": 17},
    }
    return (f"data: {json.dumps(initial)}\n\ndata: {json.dumps(final)}\n\ndata: [DONE]\n\n").encode()


class FakeTransport:
    endpoint = "controlled-fake://no-model-service"

    def open(self, payload: dict[str, Any], *, timeout_seconds: int | None = None) -> UpstreamStream:
        global request_count
        request_count += 1
        ordinal = request_count
        dump(
            root / "fake_requests" / f"{ordinal:02d}.json",
            {"request_ordinal": ordinal, "timeout_seconds": timeout_seconds, "payload": payload},
        )
        if ordinal == 4:
            error = {
                "error": {
                    "message": "maximum context length exceeded: reduce the length of the messages",
                    "type": "invalid_request_error",
                    "code": "context_length_exceeded",
                }
            }
            return UpstreamStream(
                status=400,
                headers={"content-type": "application/json"},
                chunks=iter([json.dumps(error).encode()]),
                close=lambda: None,
            )
        if ordinal == 1:
            return UpstreamStream(
                status=200,
                headers={"content-type": "text/event-stream"},
                chunks=iter(
                    [
                        sse_response(
                            request_ordinal=ordinal,
                            delta={
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "native-tool-1",
                                        "type": "function",
                                        "function": {
                                            "name": "exec",
                                            "arguments": json.dumps({"command": "printf 'R012_SQLITE_BOUNDARY\\n'"}),
                                        },
                                    }
                                ],
                            },
                            finish_reason="tool_calls",
                        )
                    ]
                ),
                close=lambda: None,
            )
        if ordinal == 2:
            return UpstreamStream(
                status=200,
                headers={"content-type": "text/event-stream"},
                chunks=iter(
                    [
                        sse_response(
                            request_ordinal=ordinal,
                            delta={"role": "assistant", "content": controlled_history},
                            finish_reason="stop",
                        )
                    ]
                ),
                close=lambda: None,
            )
        if ordinal == 7:
            # After the native SQLite transition, create a fresh complete tool
            # batch. The next normal decision (ordinal 8) must reselect and
            # inject the previously retired event prior. The transport never
            # labels a request from payload contents.
            return UpstreamStream(
                status=200,
                headers={"content-type": "text/event-stream"},
                chunks=iter(
                    [
                        sse_response(
                            request_ordinal=ordinal,
                            delta={
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "native-tool-2",
                                        "type": "function",
                                        "function": {
                                            "name": "exec",
                                            "arguments": json.dumps({"command": "printf 'R012_REINJECT_BOUNDARY\n'"}),
                                        },
                                    }
                                ],
                            },
                            finish_reason="tool_calls",
                        )
                    ]
                ),
                close=lambda: None,
            )
        # Deliberately generic for normal completions, the native compaction
        # summary, and any retry. Request artifacts determine their identity;
        # this transport does not inspect payload shape to label or bypass them.
        return UpstreamStream(
            status=200,
            headers={"content-type": "text/event-stream"},
            chunks=iter(
                [
                    sse_response(
                        request_ordinal=ordinal,
                        delta={"role": "assistant", "content": "Controlled fake transport completed this request."},
                        finish_reason="stop",
                    )
                ]
            ),
            close=lambda: None,
        )


task = {
    "skill_id": "controlled-task",
    "version": 1,
    "title": "Controlled task prior",
    "when_to_apply": "start",
    "rules": ["Keep tool observations as evidence."],
}
event = {
    "skill_id": "controlled-event",
    "version": 1,
    "title": "Controlled event prior",
    "when_to_apply": "after tool",
    "rules": ["Check the printed boundary marker."],
}
overlay = DurableOverlay(
    trial_id="r012-native-sqlite-probe",
    state_path=root / "overlay.json",
    evidence_dir=root / "proxy",
    token_counter=synthetic_count,
    max_input_tokens=250_000,
    task_selector=lambda *_: {"skill": task},
    event_selector=lambda *_: {"skill": event},
)
detector = SqliteTranscriptCompactionDetector(location_provider=session_location, evidence_dir=root / "proxy")
permit_directory = root / "permits"
permit_gate = NativeSummaryPermitGate(
    permit_dir=permit_directory,
    expected_session_id=session_id,
    trial_id="r012-native-sqlite-probe",
)
service = DurableProxyService(
    overlay=overlay,
    transport=FakeTransport(),
    compaction_evidence_provider=detector,
    native_summary_permit_gate=permit_gate,
    max_forwarded_requests=8,
    trial_deadline_monotonic=time.monotonic() + 600,
)
server, thread = serve_in_thread(service)
original_forward = service.forward


def bounded_forward(payload: dict[str, Any]) -> Any:
    global http_request_count
    http_request_count += 1
    if http_request_count == 8:
        threading.Thread(target=server.shutdown, daemon=True).start()
    return original_forward(payload)


service.forward = bounded_forward
port = server.server_address[1]
config = {
    "plugins": {
        "allow": ["codeskill-r012-sidecar"],
        "load": {"paths": [str(plugin_root)]},
        "entries": {
            "codeskill-r012-sidecar": {
                "enabled": True,
                "config": {
                    "auditPath": str(root / "plugin-audit.jsonl"),
                    "permitDirectory": str(permit_directory),
                    "trialId": "r012-native-sqlite-probe",
                    "sessionId": session_id,
                },
            }
        },
    },    "models": {
        "providers": {
            "codeskill-r012": {
                "baseUrl": f"http://127.0.0.1:{port}/v1",
                "apiKey": "EMPTY",
                "api": "openai-completions",
                "models": [
                    {
                        "id": "controlled",
                        "name": "Controlled fake model",
                        "reasoning": False,
                        "input": ["text"],
                        "contextWindow": 270_000,
                        "maxTokens": 16_384,
                        "compat": {"supportsUsageInStreaming": True},
                    }
                ],
            }
        }
    },
    "agents": {
        "defaults": {
            "workspace": str(workspace),
            "model": {"primary": "codeskill-r012/controlled"},
            "contextTokens": 270_000,
            "thinkingDefault": "off",
            "maxConcurrent": 1,
            # Keep enough history to require a genuine native summary after
            # the controlled overflow.  The fake transport still provides all
            # summary/retry responses; no external model is contacted.
            "compaction": {
                # Safeguard mode routes the provider overflow through
                # OpenClaw's own context-engine compaction lifecycle.  This
                # avoids the embedded runtime's small-session no-op while
                # still writing the native SQLite transcript entry.
                "mode": "safeguard",
                # A positive summary allowance is required by OpenClaw's
                # native summarizer; it remains far below the session budget
                # while allowing the fake transport to return a summary.
                "reserveTokens": 256,
                "reserveTokensFloor": 0,
                "keepRecentTokens": 1,
                "recentTurnsPreserve": 1,
                "memoryFlush": {"enabled": False},
                "midTurnPrecheck": {"enabled": True},
            },
        },
        "list": [{"id": "main"}],
    },
    "tools": {"profile": "coding", "allow": ["exec"], "exec": {"security": "full", "ask": "off"}},
}
# OpenClaw 2026.9 removed the two numeric compaction tuning keys. Keep them
# only for older supported releases; this is package-schema compatibility, not
# a request-purpose or payload inference.
if not legacy_compaction_reserve_knobs:
    config["agents"]["defaults"]["compaction"].pop("reserveTokens")
    config["agents"]["defaults"]["compaction"].pop("reserveTokensFloor")
dump(state / "openclaw.json", config)
cli_base = [
    "docker",
    "run",
    "--rm",
    "--name",
    "codeskill-r012-native-sqlite-probe",
    "--user",
    f"{os.getuid()}:{os.getgid()}",
    "--network",
    "host",
    "--read-only",
    "--tmpfs",
    "/tmp",
    "-e",
    f"HOME={home}",
    "-e",
    f"OPENCLAW_STATE_DIR={state}",
    "-v",
    f"{root}:{root}:rw",
    "-v",
    f"{openclaw_mount_root}:{container_mount_root}:ro",
    args.node_image,
    "node",
    str(container_openclaw_root / "openclaw.mjs"),
    "agent",
    "--local",
    "--json",
    "--session-id",
    session_id,
    "--model",
    "codeskill-r012/controlled",
    "--thinking",
    "off",
    "--timeout",
    "250",
]

started = time.monotonic()
cli_results: list[dict[str, Any]] = []
try:
    for label, message in (
        ("first", "Run one controlled boundary command and report its observation."),
        ("second", "Record one more completed controlled observation before the boundary check."),
        ("overflow", "Continue the isolated controlled session with one more boundary check."),
        ("reinject", "Run one final controlled tool check after the native compaction boundary."),
    ):
        try:
            result = subprocess.run(
                [*cli_base, "--message", message], capture_output=True, text=True, timeout=270
            )
            (root / f"cli-{label}.stdout").write_text(result.stdout, encoding="utf-8")
            (root / f"cli-{label}.stderr").write_text(result.stderr, encoding="utf-8")
            cli_results.append({"label": label, "exit_code": result.returncode})
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "stop", "-t", "2", "codeskill-r012-native-sqlite-probe"], capture_output=True)
            cli_results.append({"label": label, "infra": "bounded_timeout"})
            break
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)

location = detector.state.get("active_location")
native_compactions: list[dict[str, Any]] = []
if isinstance(location, dict):
    native_compactions = _read_compactions(location)
observations = []
for path in sorted((root / "proxy" / "native_compaction").glob("attempt-*.json")):
    observations.append(read_json(path))
native_summary_records = []
for path in sorted((root / "proxy" / "upstream_requests").glob("attempt-*.json")):
    record = read_json(path)
    if record.get("kind") == "r012_native_summary_upstream_request":
        native_summary_records.append(record)
permit_state_path = permit_directory / "sidecar-permit-state.json"
permit_state = read_json(permit_state_path) if permit_state_path.exists() else None
plugin_audit = []
audit_path = root / "plugin-audit.jsonl"
if audit_path.exists():
    plugin_audit = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line]
all_proxy_records = [
    read_json(path)
    for path in sorted((root / "proxy" / "upstream_requests").glob("attempt-*.json"))
]
transition_records = [
    record
    for record in all_proxy_records
    if isinstance(record.get("carried_priors"), dict)
    and isinstance(record.get("retired_event_priors"), dict)
    and record["retired_event_priors"].get("reason") == "r012_verified_native_compaction_retires_event_blocks_without_carry"
]
transition_attempt_ordinal = max(
    (int(record["attempt_ordinal"]) for record in transition_records if isinstance(record.get("attempt_ordinal"), int)),
    default=0,
)
reinjection_records = [
    record
    for record in all_proxy_records
    if isinstance(record.get("attempt_ordinal"), int)
    and int(record["attempt_ordinal"]) > transition_attempt_ordinal
    and isinstance(record.get("event_selection"), list)
    and any(
        isinstance(selection, dict)
        and selection.get("decision") == "injected_after_complete_batch"
        and any(
            isinstance(skill, dict)
            and isinstance(skill.get("skill"), dict)
            and skill["skill"].get("skill_id") == "controlled-event"
            for skill in selection.get("injected_skills", [])
        )
        for selection in record["event_selection"]
    )
    and "[CODESKILL EVENT PRIOR KNOWLEDGE]" in json.dumps(record.get("forwarded_request", {}), ensure_ascii=False)
]
permit_retired = (
    isinstance(permit_state, dict)
    and isinstance(permit_state.get("permits"), dict)
    and any(
        item.get("status") == "retired_after_native_sqlite_transition"
        for item in permit_state["permits"].values()
        if isinstance(item, dict)
    )
)
native_summary_raw_forwarded = bool(native_summary_records) and all(
    record.get("native_request") == record.get("forwarded_request")
    and record.get("overlay_disposition", {}).get("kind") == "native_summary_bypass"
    and record.get("overlay_disposition", {}).get("payload_or_prompt_heuristic") == "not_used"
    for record in native_summary_records
)
audit_kinds = [item.get("kind") for item in plugin_audit]
verified = (
    http_request_count <= 8
    and request_count <= 8
    and location is not None
    and bool(native_compactions)
    and any(item.get("outcome") == "new_compaction_confirmed" for item in observations)
    and any(isinstance(item.get("overlay_disposition"), dict) for item in observations)
    and "before_compaction" in audit_kinds
    and "native_summary_permit_written" in audit_kinds
    and "normal_call_wrapper_created" in audit_kinds
    and "normal_call_boundary_written" in audit_kinds
    and native_summary_raw_forwarded
    and bool(transition_records)
    and bool(reinjection_records)
    and permit_retired
    and all(item.get("exit_code") == 0 for item in cli_results)
)
status = {
    "cli_results": cli_results,
    "elapsed_seconds": time.monotonic() - started,
    "http_requests_including_rejections": http_request_count,
    "fake_transport_requests": request_count,
    "real_model_calls": 0,
    "accounting": "synthetic character-based counter; not real tokenizer evidence",
    "openclaw_test_dependency": {
        "path": str(openclaw_root),
        "mount_root": str(openclaw_mount_root),
        "container_package_root": str(container_openclaw_root),
        "package_name": openclaw_package["name"],
        "package_version": openclaw_package["version"],
        "mount_mode": "read_only",
        "node_image": args.node_image,
        "legacy_compaction_reserve_knobs": legacy_compaction_reserve_knobs,
    },
    "native_session": location,
    "native_compaction_ids": [item["compaction_id"] for item in native_compactions],
    "native_observation_outcomes": [item.get("outcome") for item in observations],
    "public_plugin_audit_kinds": audit_kinds,
    "native_summary_bypass_record_count": len(native_summary_records),
    "native_summary_raw_forwarded": native_summary_raw_forwarded,
    "native_summary_permit_retired_after_sqlite_transition": permit_retired,
    "transition_records_with_task_relocation_and_event_retirement": len(transition_records),
    "native_compaction_transition_attempt_ordinal": transition_attempt_ordinal,
    "event_reinjection_records_after_native_compaction": len(reinjection_records),
    "native_compaction_evidence_verified": verified,
    "scope": "controlled CLI plus fake transport only; not task solving, solver/verifier, tokenizer, or real-model evidence",
}
dump(root / "status.json", status)
print(json.dumps(status))
raise SystemExit(0 if verified else 1)
