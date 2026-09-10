#!/usr/bin/env python3
"""Bounded M1 DeepSeek chat transport and JSON-format probe.

The script intentionally uses only the standard library so the first model
request is independently reproducible before the manager environment exists.
It records the unmodified request and response, excluding credentials.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "http://<MODEL_SERVICE_HOST>:31000/v1"
DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4-Flash"


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--max-output-tokens", type=int, default=256)
    parser.add_argument("--reasoning-effort", choices=("off", "low", "medium", "high"))
    parser.add_argument("--contract-version", default="v0.4")
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--research-decisions-sha256", required=True)
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=False)
    expected = {"probe": "m1", "status": "ok"}
    user_content = 'Return this object with the same values: {"probe":"m1","status":"ok"}'
    if args.reasoning_effort:
        expected = {"product": 437}
        user_content = (
            "Work out 19 multiplied by 23 internally. Then return only this "
            'JSON object: {"product":437}'
        )
    payload = {
        "model": args.model,
        "temperature": 0,
        "max_tokens": args.max_output_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a CODESKILL manager transport probe. "
                    "Return exactly one JSON object and no prose."
                ),
            },
            {
                "role": "user",
                "content": user_content,
            },
        ],
    }
    if args.reasoning_effort:
        payload["reasoning_effort"] = args.reasoning_effort
    call = {
        "kind": "live_m1_manager_transport_probe",
        "historical": False,
        "fixture": False,
        "contract": {
            "version": args.contract_version,
            "reproduction_spec_sha256": args.contract_sha256,
            "research_decisions_sha256": args.research_decisions_sha256,
        },
        "started_at_utc": utc_now(),
        "base_url": args.base_url,
        "timeout_seconds": args.timeout_seconds,
        "request": payload,
    }
    write_json(args.out / "request.json", call)

    request = Request(
        f"{args.base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urlopen(request, timeout=args.timeout_seconds) as response:
            raw_response = response.read().decode("utf-8", errors="replace")
            status_code = response.status
    except HTTPError as error:
        raw_response = error.read().decode("utf-8", errors="replace")
        status_code = error.code
    except URLError as error:
        raw_response = str(error)
        status_code = None
    elapsed_seconds = time.monotonic() - started
    record = {
        "kind": call["kind"],
        "started_at_utc": call["started_at_utc"],
        "finished_at_utc": utc_now(),
        "elapsed_seconds": elapsed_seconds,
        "http_status": status_code,
        "raw_response": raw_response,
    }
    parsed: object | None = None
    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError:
        pass
    if parsed is not None:
        record["parsed_response"] = parsed
    write_json(args.out / "response.json", record)

    if status_code != 200:
        return 1
    if not isinstance(parsed, dict):
        return 2
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        return 3
    message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        return 4
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        return 5
    if result != expected:
        return 6
    return 0


if __name__ == "__main__":
    sys.exit(main())
