"""Compare SGLang message-tokenization with a saved chat-completion usage count."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from codeskill_rebuild.manager import ServerMessageTokenCounter
from codeskill_rebuild.types import utc_now, write_json


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--response", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--base-url", default="http://<MODEL_SERVICE_HOST>:31000/v1")
    parser.add_argument("--timeout", default=30, type=int)
    args = parser.parse_args()

    request_record = json.loads(args.request.read_text(encoding="utf-8"))
    response_record = json.loads(args.response.read_text(encoding="utf-8"))
    messages = request_record["request"]["messages"]
    counter = ServerMessageTokenCounter(args.base_url, timeout_seconds=args.timeout)
    counted = counter(messages)
    usage = response_record.get("parsed_response", {}).get("usage", {})
    observed = usage.get("prompt_tokens")
    write_json(
        args.output,
        {
            "kind": "live_message_tokenizer_vs_saved_chat_usage",
            "created_at_utc": utc_now(),
            "historical": False,
            "fixture": False,
            "source_request": {"path": str(args.request), "sha256": sha256_file(args.request)},
            "source_response": {"path": str(args.response), "sha256": sha256_file(args.response)},
            "server_message_token_count": counted,
            "saved_chat_usage_prompt_tokens": observed,
            "counts_match": isinstance(observed, int) and counted == observed,
            "tokenizer_exchange": counter.last_exchange,
        },
    )


if __name__ == "__main__":
    main()
