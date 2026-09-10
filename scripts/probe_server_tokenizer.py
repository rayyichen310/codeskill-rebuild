#!/usr/bin/env python3
"""Probe SGLang tokenization endpoints without generating a completion."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from codeskill_rebuild.types import write_json


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def post(url: str, payload: dict, timeout: int) -> dict:
    request = Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return {"http_status": response.status, "raw_response": response.read().decode("utf-8", errors="replace")}
    except HTTPError as error:
        return {"http_status": error.code, "raw_response": error.read().decode("utf-8", errors="replace")}
    except (URLError, TimeoutError, OSError) as error:
        return {"http_status": None, "raw_response": str(error), "transport_exception": type(error).__name__}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--research-decisions-sha256", required=True)
    parser.add_argument("--base-url", default="http://<MODEL_SERVICE_HOST>:31000")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    messages = [{"role": "system", "content": "Tokenizer probe."}, {"role": "user", "content": "Count these messages."}]
    cases = {
        "flat_text": {"text": "Tokenizer probe. Count these messages."},
        "openai_messages": {"messages": messages, "add_generation_prompt": True},
    }
    responses = {name: {"request": payload, **post(args.base_url.rstrip("/") + "/tokenize", payload, args.timeout_seconds)} for name, payload in cases.items()}
    for response in responses.values():
        try:
            response["parsed_response"] = json.loads(response["raw_response"])
        except json.JSONDecodeError:
            pass
    write_json(
        args.out / "tokenizer-probe.json",
        {
            "kind": "live_sglang_tokenize_probe",
            "created_at_utc": now(),
            "historical": False,
            "fixture": False,
            "contract": {"version": "v0.4", "reproduction_spec_sha256": args.contract_sha256, "research_decisions_sha256": args.research_decisions_sha256},
            "endpoint": args.base_url.rstrip("/") + "/tokenize",
            "responses": responses,
        },
    )


if __name__ == "__main__":
    main()
