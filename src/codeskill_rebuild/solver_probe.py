"""Bounded, synthetic M3 full-payload tokenizer/usage probes.

The M3 profile requires a server-side count of the exact OpenAI-shaped solver
payload, including its tools and tool-choice fields.  This is deliberately
separate from the manager client: probes have no task trace content, have a
smaller output/timeout cap, and preserve a preflight failure without reserving
a generation call.
"""

from __future__ import annotations

from copy import deepcopy
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .types import utc_now, write_json


class PayloadProbeError(RuntimeError):
    """A probe could not establish equal tokenizer and completion input use."""


class PayloadTokenizationError(PayloadProbeError):
    """The server rejected or failed to return a count for a complete payload."""


class ServerPayloadTokenCounter:
    """Count a complete OpenAI solver payload through the serving endpoint.

    The only added field is ``add_generation_prompt``.  It asks SGLang to
    render the same next-assistant boundary that chat completion will render;
    every original payload field, especially ``tools`` and ``tool_choice``, is
    retained verbatim and the exchange is kept for audit.
    """

    method = "sglang_full_openai_payload_tokenize_v1"

    def __init__(self, base_url: str, timeout_seconds: int = 60) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("M3 tokenizer probe timeout must be from 1 through 60 seconds")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.last_exchange: dict[str, Any] | None = None

    def __call__(self, payload: dict[str, Any]) -> int:
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            raise PayloadTokenizationError("full payload tokenizer requires an OpenAI messages list")
        token_payload = deepcopy(payload)
        token_payload["add_generation_prompt"] = True
        request = Request(
            self.base_url + "/tokenize",
            data=json.dumps(token_payload).encode("utf-8"),
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
        try:
            parsed: Any | None = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        self.last_exchange = {
            "endpoint": self.base_url + "/tokenize",
            "scope": "complete_openai_payload_plus_generation_marker",
            "request": token_payload,
            "http_status": status,
            "elapsed_seconds": time.monotonic() - started,
            "raw_response": raw,
            "parsed_response": parsed,
        }
        if status != 200 or not isinstance(parsed, dict) or isinstance(parsed.get("count"), bool) or not isinstance(parsed.get("count"), int):
            raise PayloadTokenizationError("complete-payload /tokenize failed; raw exchange is preserved")
        self.last_exchange["count"] = parsed["count"]
        return parsed["count"]


@dataclass(frozen=True)
class PayloadProbeProfile:
    base_url: str
    model: str
    timeout_seconds: int = 60
    max_output_tokens: int = 256
    max_total_calls: int = 100

    def validate(self) -> None:
        if self.timeout_seconds <= 0 or self.timeout_seconds > 60:
            raise ValueError("M3 synthetic completion timeout must be from 1 through 60 seconds")
        if self.max_output_tokens <= 0 or self.max_output_tokens > 256:
            raise ValueError("M3 synthetic completion output must be from 1 through 256 tokens")
        if self.max_total_calls <= 0:
            raise ValueError("global development call limit must be positive")


class FullPayloadUsageProbe:
    """Reserve at most one synthetic completion after full-payload preflight."""

    def __init__(
        self,
        *,
        profile: PayloadProbeProfile,
        run_dir: Path,
        ledger_path: Path,
        contract: dict[str, str],
        exact_token_counter: ServerPayloadTokenCounter,
    ) -> None:
        profile.validate()
        self.profile = profile
        self.run_dir = Path(run_dir)
        self.ledger_path = Path(ledger_path)
        self.contract = dict(contract)
        self.exact_token_counter = exact_token_counter
        self.call_count = len(list((self.run_dir / "model_calls").glob("call-*"))) if (self.run_dir / "model_calls").exists() else 0

    def _load_ledger(self) -> dict[str, Any]:
        if not self.ledger_path.is_file():
            return {"schema_version": 1, "limit": self.profile.max_total_calls, "calls": []}
        loaded = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or not isinstance(loaded.get("calls"), list) or not isinstance(loaded.get("limit"), int):
            raise PayloadProbeError("development ledger has an invalid schema")
        if loaded["limit"] != self.profile.max_total_calls:
            raise PayloadProbeError(
                f"payload probe profile limit {self.profile.max_total_calls} differs from durable ledger limit {loaded['limit']}"
            )
        return loaded

    def _reserve(self, *, call_id: str, purpose: str) -> None:
        ledger = self._load_ledger()
        if len(ledger["calls"]) >= self.profile.max_total_calls:
            raise PayloadProbeError(f"development call budget exhausted: {len(ledger['calls'])}/{self.profile.max_total_calls}")
        ledger["calls"].append(
            {
                "run_dir": str(self.run_dir),
                "call_id": call_id,
                "purpose": purpose,
                "reserved_at_utc": utc_now(),
                "status": "reserved",
            }
        )
        write_json(self.ledger_path, ledger)

    def _finalize(self, *, call_id: str, response: dict[str, Any], status: str) -> None:
        ledger = self._load_ledger()
        for entry in reversed(ledger["calls"]):
            if entry.get("run_dir") == str(self.run_dir) and entry.get("call_id") == call_id:
                entry.update(
                    {
                        "status": status,
                        "finalized_at_utc": utc_now(),
                        "http_status": response.get("http_status"),
                        "finish_reason": response.get("finish_reason"),
                        "classification": response.get("classification"),
                        "response_path": str(self.run_dir / "model_calls" / call_id / "response.json"),
                    }
                )
                write_json(self.ledger_path, ledger)
                return
        raise PayloadProbeError(f"{call_id}: reservation disappeared before finalization")

    def _validate_payload(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            raise PayloadProbeError("probe payload needs an OpenAI messages list")
        if payload.get("model") != self.profile.model:
            raise PayloadProbeError("probe payload model must equal the frozen profile model")
        max_tokens = payload.get("max_tokens")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 0 < max_tokens <= self.profile.max_output_tokens:
            raise PayloadProbeError(f"probe payload max_tokens must be from 1 through {self.profile.max_output_tokens}")
        if payload.get("stream") is not False:
            raise PayloadProbeError("probe payload must set stream=false so usage is auditable in one response")

    def run(self, *, purpose: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._validate_payload(payload)
        self.call_count += 1
        call_id = f"call-{self.call_count:04d}"
        call_dir = self.run_dir / "model_calls" / call_id
        call_dir.mkdir(parents=True, exist_ok=False)
        try:
            preflight_tokens = int(self.exact_token_counter(payload))
        except (PayloadTokenizationError, URLError, TimeoutError, OSError) as error:
            write_json(
                call_dir / "preflight.json",
                {
                    "kind": "m3_full_payload_tools_tokenizer_preflight",
                    "purpose": purpose,
                    "created_at_utc": utc_now(),
                    "contract": self.contract,
                    "classification": "tokenizer_unavailable",
                    "error": str(error),
                    "complete_chat_payload": payload,
                    "tokenizer_exchange": self.exact_token_counter.last_exchange,
                },
            )
            raise PayloadProbeError("complete-payload tokenize preflight failed; no completion was reserved") from error
        preflight = {
            "method": self.exact_token_counter.method,
            "exact_forwarded_input_tokens": preflight_tokens,
            "scope": "complete_openai_payload",
            "tokenizer_exchange": self.exact_token_counter.last_exchange,
        }
        self._reserve(call_id=call_id, purpose=purpose)
        request_record = {
            "kind": "m3_full_payload_tools_tokenizer_probe",
            "purpose": purpose,
            "historical": False,
            "fixture": False,
            "trace_content": "none; synthetic tools schema only",
            "started_at_utc": utc_now(),
            "contract": self.contract,
            "preflight": preflight,
            "request": deepcopy(payload),
        }
        write_json(call_dir / "request.json", request_record)
        request = Request(
            self.profile.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        status: int | None = None
        started = time.monotonic()
        try:
            with urlopen(request, timeout=self.profile.timeout_seconds) as response:
                status = response.status
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as error:
            status = error.code
            raw = error.read().decode("utf-8", errors="replace")
        except (URLError, TimeoutError, OSError) as error:
            raw = str(error)
        try:
            parsed: Any | None = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        response_record: dict[str, Any] = {
            "kind": "m3_full_payload_tools_tokenizer_probe",
            "purpose": purpose,
            "finished_at_utc": utc_now(),
            "elapsed_seconds": time.monotonic() - started,
            "http_status": status,
            "raw_response": raw,
            "parsed_response": parsed,
        }
        usage = parsed.get("usage") if isinstance(parsed, dict) else None
        choices = parsed.get("choices") if isinstance(parsed, dict) else None
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            response_record["finish_reason"] = choices[0].get("finish_reason")
        observed = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        response_record["usage"] = usage
        response_record["tokenizer_prompt_token_comparison"] = {
            "preflight_complete_payload_tokens": preflight_tokens,
            "chat_usage_prompt_tokens": observed,
            "matches": isinstance(observed, int) and observed == preflight_tokens,
        }
        if status != 200 or not isinstance(parsed, dict):
            response_record["classification"] = "transport_or_nonjson_failure"
            terminal = "failed"
        elif response_record.get("finish_reason") == "length":
            response_record["classification"] = "model_output_truncated"
            terminal = "truncated"
        elif not response_record["tokenizer_prompt_token_comparison"]["matches"]:
            response_record["classification"] = "tokenizer_prompt_count_mismatch"
            terminal = "tokenizer_mismatch"
        else:
            terminal = "succeeded"
        write_json(call_dir / "response.json", response_record)
        self._finalize(call_id=call_id, response=response_record, status=terminal)
        if terminal != "succeeded":
            raise PayloadProbeError(f"{call_id}: {response_record.get('classification')}; response is preserved")
        return {"call_id": call_id, "preflight": preflight, "response": response_record}
