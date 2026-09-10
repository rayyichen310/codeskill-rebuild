"""Bounded OpenAI-compatible DeepSeek manager client with raw evidence."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .context import ContextBlocked
from .types import utc_now, write_json


class ManagerCallError(RuntimeError):
    pass


class TokenizationError(RuntimeError):
    """The serving endpoint did not provide an auditable message token count."""


class ServerMessageTokenCounter:
    """Use SGLang's message-aware ``/tokenize`` endpoint for preflight checks.

    This deliberately sends the same role/content messages that will be sent to
    ``/chat/completions``.  The latest request/response exchange is retained so
    the manager client can preserve it alongside the live model request.
    """

    method = "sglang_message_tokenize"

    def __init__(self, base_url: str, timeout_seconds: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.last_exchange: dict[str, Any] | None = None

    def __call__(self, messages: list[dict[str, Any]]) -> int:
        payload = {"messages": messages, "add_generation_prompt": True}
        request = Request(
            self.base_url + "/tokenize",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        status: int | None = None
        started = time.monotonic()
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                status = response.status
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as error:
            status = error.code
            raw = error.read().decode("utf-8", errors="replace")
        except (URLError, TimeoutError, OSError) as error:
            raw = str(error)
        parsed: Any | None = None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            pass
        self.last_exchange = {
            "endpoint": self.base_url + "/tokenize",
            "request": payload,
            "http_status": status,
            "elapsed_seconds": time.monotonic() - started,
            "raw_response": raw,
            "parsed_response": parsed,
        }
        if status != 200 or not isinstance(parsed, dict) or not isinstance(parsed.get("count"), int):
            raise TokenizationError("message-aware /tokenize failed; raw exchange is preserved")
        self.last_exchange["count"] = parsed["count"]
        return parsed["count"]


@dataclass(frozen=True)
class ManagerProfile:
    base_url: str
    model: str
    timeout_seconds: int = 300
    max_output_tokens: int = 8192
    manager_context_tokens: int = 524288
    safety_tokens: int = 4096
    temperature: float = 0.0
    reasoning_effort: str = "high"
    # ``None`` is an explicit unlimited development-call policy.  It is not a
    # large sentinel value: per-call preflight, timeout, output, and journal
    # safeguards remain active.
    max_total_calls: int | None = 30

    def validate(self) -> None:
        if self.timeout_seconds > 300 or self.timeout_seconds <= 0:
            raise ValueError("R001 limits a manager request timeout to 300 seconds")
        if self.max_output_tokens > 8192 or self.max_output_tokens <= 0:
            raise ValueError("R001 limits manager output to 8192 tokens")
        if self.max_total_calls is not None and self.max_total_calls <= 0:
            raise ValueError("max_total_calls must be positive")


def update_development_ledger_limit(
    ledger_path: Path,
    *,
    new_limit: int | None,
    reason: str,
    contract: dict[str, str],
) -> dict[str, Any]:
    """Atomically change a development-call ceiling without erasing history."""
    if new_limit is not None and new_limit <= 0:
        raise ValueError("new ledger limit must be positive")
    path = Path(ledger_path)
    new_limit_value: int | str = new_limit if new_limit is not None else "unlimited"
    ledger: dict[str, Any] = {"schema_version": 2, "limit": new_limit_value, "calls": []}
    existed = path.is_file()
    if existed:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or not isinstance(loaded.get("calls"), list):
            raise ManagerCallError("manager ledger has an invalid schema")
        ledger = loaded
    calls = ledger["calls"]
    previous = ledger.get("limit")
    if previous != "unlimited" and (not isinstance(previous, int) or previous <= 0):
        raise ManagerCallError("manager ledger has an invalid limit")
    if new_limit is not None and len(calls) > new_limit:
        raise ManagerCallError(f"cannot lower ledger to {new_limit}; it already contains {len(calls)} calls")
    if previous != new_limit_value or not existed:
        history = ledger.setdefault("limit_history", [])
        if not isinstance(history, list):
            raise ManagerCallError("manager ledger limit_history has an invalid schema")
        history.append(
            {
                "changed_at_utc": utc_now(),
                "previous_limit": previous if existed else None,
                "new_limit": new_limit_value,
                "calls_preserved": len(calls),
                "reason": reason,
                "contract": dict(contract),
            }
        )
        ledger["schema_version"] = 2
        ledger["limit"] = new_limit_value
        write_json(path, ledger)
    return ledger


class ManagerClient:
    def __init__(self, profile: ManagerProfile, run_dir: Path, contract: dict[str, str], ledger_path: Path, exact_token_counter: Any):
        profile.validate()
        self.profile = profile
        self.run_dir = Path(run_dir)
        self.contract = dict(contract)
        self.ledger_path = Path(ledger_path)
        self.exact_token_counter = exact_token_counter
        self.call_count = len(list((self.run_dir / "model_calls").glob("call-*"))) if (self.run_dir / "model_calls").exists() else 0

    def _preflight(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        if self.exact_token_counter is None:
            raise ContextBlocked("Exact tokenizer/template counter is required for live manager calls")
        try:
            estimate = int(self.exact_token_counter(messages))
        except (TokenizationError, URLError, TimeoutError, OSError) as error:
            exchange = getattr(self.exact_token_counter, "last_exchange", None)
            detail = {"state": "tokenizer_unavailable", "error": str(error), "tokenizer_exchange": exchange}
            raise ContextBlocked(json.dumps(detail)) from error
        limit = self.profile.manager_context_tokens - self.profile.max_output_tokens - self.profile.safety_tokens
        record = {
            "method": getattr(self.exact_token_counter, "method", "exact_server_or_tokenizer"),
            "exact_tokenizer_verified": True,
            "estimated_input_tokens": estimate,
            "allowed_estimated_input_tokens": limit,
        }
        exchange = getattr(self.exact_token_counter, "last_exchange", None)
        if exchange is not None:
            record["tokenizer_exchange"] = exchange
        if estimate > limit:
            raise ContextBlocked(json.dumps({"state": "context_blocked", **record}))
        return record

    def _load_ledger(self) -> dict[str, Any]:
        limit: int | str = self.profile.max_total_calls if self.profile.max_total_calls is not None else "unlimited"
        ledger: dict[str, Any] = {"schema_version": 2, "limit": limit, "calls": []}
        if self.ledger_path.is_file():
            loaded = json.loads(self.ledger_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict) or not isinstance(loaded.get("calls"), list):
                raise ManagerCallError("manager ledger has an invalid schema")
            ledger = loaded
        return ledger

    def _reserve_call(self, call_id: str, purpose: str) -> None:
        ledger = self._load_ledger()
        calls = ledger.get("calls", [])
        durable_limit = ledger.get("limit")
        if durable_limit == "unlimited":
            # The explicit R014 policy is durable and authoritative.  A
            # legacy runner may still construct a finite profile, but it must
            # neither reject at that profile's old cap nor downgrade the
            # ledger back to a finite value.
            pass
        elif not isinstance(durable_limit, int) or durable_limit <= 0:
            raise ManagerCallError("manager ledger has an invalid limit")
        elif self.profile.max_total_calls is None:
            raise ManagerCallError("manager ledger limit is finite; explicitly activate unlimited policy before a new call")
        elif len(calls) >= durable_limit:
            raise ManagerCallError(f"Development manager-call budget exhausted: {len(calls)}/{durable_limit}")
        calls.append(
            {
                "run_dir": str(self.run_dir),
                "call_id": call_id,
                "purpose": purpose,
                "reserved_at_utc": utc_now(),
                "status": "reserved",
            }
        )
        ledger["calls"] = calls
        # Keep the durable limit unchanged.  Changes are auditable only via
        # update_development_ledger_limit(), never as a side effect of a
        # caller's profile object.
        ledger["schema_version"] = 2
        write_json(self.ledger_path, ledger)

    def _finalize_call(self, call_id: str, response_record: dict[str, Any], status: str) -> None:
        """Atomically convert a reservation into a recoverable terminal record."""
        ledger = self._load_ledger()
        for entry in reversed(ledger["calls"]):
            if entry.get("run_dir") == str(self.run_dir) and entry.get("call_id") == call_id:
                entry.update(
                    {
                        "status": status,
                        "finalized_at_utc": utc_now(),
                        "http_status": response_record.get("http_status"),
                        "finish_reason": response_record.get("finish_reason"),
                        "classification": response_record.get("classification"),
                        "response_path": str(self.run_dir / "model_calls" / call_id / "response.json"),
                    }
                )
                write_json(self.ledger_path, ledger)
                return
        raise ManagerCallError(f"{call_id}: reservation disappeared before finalization")

    def call_json(
        self,
        *,
        purpose: str,
        messages: list[dict[str, Any]],
        retry_of: str | None = None,
        call_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        next_call_number = self.call_count + 1
        call_id = f"call-{next_call_number:04d}"
        call_dir = self.run_dir / "model_calls" / call_id
        call_dir.mkdir(parents=True, exist_ok=False)
        self.call_count = next_call_number
        try:
            preflight = self._preflight(messages)
        except ContextBlocked as error:
            write_json(
                call_dir / "preflight.json",
                {
                    "kind": "manager_preflight_failure",
                    "purpose": purpose,
                    "created_at_utc": utc_now(),
                    "contract": self.contract,
                    "classification": "context_blocked",
                    "error": str(error),
                    "request_messages": messages,
                    "tokenizer_exchange": getattr(self.exact_token_counter, "last_exchange", None),
                },
            )
            raise
        self._reserve_call(call_id, purpose)
        payload: dict[str, Any] = {
            "model": self.profile.model,
            "messages": messages,
            "temperature": self.profile.temperature,
            "max_tokens": self.profile.max_output_tokens,
            "response_format": {"type": "json_object"},
            "reasoning_effort": self.profile.reasoning_effort,
        }
        request_record = {
            "kind": "live_manager_call",
            "purpose": purpose,
            "historical": False,
            "fixture": False,
            "started_at_utc": utc_now(),
            "contract": self.contract,
            "retry_of": retry_of,
            "call_metadata": call_metadata or {},
            "preflight": preflight,
            "request": payload,
            "output_budget_counts_reasoning": True,
        }
        write_json(call_dir / "request.json", request_record)
        request = Request(
            self.profile.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        status: int | None = None
        try:
            with urlopen(request, timeout=self.profile.timeout_seconds) as response:
                status = response.status
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as error:
            status = error.code
            raw = error.read().decode("utf-8", errors="replace")
        except (URLError, TimeoutError, OSError) as error:
            raw = str(error)
        parsed: Any | None = None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            pass
        response_record = {
            "kind": "live_manager_call",
            "purpose": purpose,
            "finished_at_utc": utc_now(),
            "elapsed_seconds": time.monotonic() - started,
            "http_status": status,
            "raw_response": raw,
            "parsed_response": parsed,
        }
        if isinstance(parsed, dict):
            try:
                response_record["finish_reason"] = parsed["choices"][0].get("finish_reason")
                response_record["usage"] = parsed.get("usage")
            except (KeyError, IndexError, TypeError):
                response_record["classification"] = "model_output_invalid"
        usage = response_record.get("usage")
        observed_prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        expected_prompt_tokens = preflight["estimated_input_tokens"]
        response_record["tokenizer_prompt_token_comparison"] = {
            "method": preflight["method"],
            "preflight_message_tokens": expected_prompt_tokens,
            "chat_usage_prompt_tokens": observed_prompt_tokens,
            "matches": isinstance(observed_prompt_tokens, int) and observed_prompt_tokens == expected_prompt_tokens,
        }
        if response_record.get("finish_reason") == "length":
            response_record["classification"] = "model_output_truncated"
        write_json(call_dir / "response.json", response_record)
        if status != 200 or not isinstance(parsed, dict):
            response_record.setdefault("classification", "transport_or_nonjson_failure")
            write_json(call_dir / "response.json", response_record)
            self._finalize_call(call_id, response_record, "failed")
            raise ManagerCallError(f"{call_id}: HTTP {status}; raw response is preserved")
        if response_record.get("finish_reason") == "length":
            self._finalize_call(call_id, response_record, "truncated")
            raise ManagerCallError(f"{call_id}: model output reached length limit; preserved as truncated")
        if not response_record["tokenizer_prompt_token_comparison"]["matches"]:
            response_record["classification"] = "tokenizer_prompt_count_mismatch"
            write_json(call_dir / "response.json", response_record)
            self._finalize_call(call_id, response_record, "tokenizer_mismatch")
            raise ManagerCallError(f"{call_id}: preflight message tokens differ from chat usage; preserved for review")
        try:
            choice = parsed["choices"][0]
            content = choice["message"]["content"]
            action = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            response_record["classification"] = "model_output_invalid"
            write_json(call_dir / "response.json", response_record)
            self._finalize_call(call_id, response_record, "invalid_output")
            raise ManagerCallError(f"{call_id}: invalid manager JSON; raw response is preserved") from error
        self._finalize_call(call_id, response_record, "succeeded")
        return {"call_id": call_id, "response": parsed, "json": action, "preflight": preflight}
