"""LangChain chat boundary with lossless, non-retrying Task call evidence.

ChatOpenAI provides the official message and structured-output interface.  A
small HTTP transport adapter records the exact request/response payload and
rewrites its ``max_completion_tokens`` alias to the serving contract's
``max_tokens``.  The ledger records external facts; it does not choose graph
edges or automatically replay an uncertain paid request.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx
from langchain_openai import ChatOpenAI

from .types import canonical_json, sha256_text


class TaskCallBlocked(RuntimeError):
    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status = status


def _write_once(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise TaskCallBlocked("infra_blocked", f"immutable Task call artifact differs: {path}")
        return
    temporary = path.with_name(path.name + ".pending")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _write_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".pending")
    with temporary.open("wb") as stream:
        stream.write(canonical_json(value).encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class _CaptureTransport(httpx.BaseTransport):
    def __init__(self, *, delegate: httpx.BaseTransport, call_dir: Path,
                 token_counter: Callable[..., int], allowance: int):
        self.delegate = delegate
        self.call_dir = call_dir
        self.token_counter = token_counter
        self.allowance = allowance

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self.call_dir.joinpath("sent.json").exists():
            raise TaskCallBlocked("transport_uncertain", "Task call was already sent")
        try:
            payload = json.loads(request.content)
        except (ValueError, UnicodeDecodeError) as error:
            raise TaskCallBlocked("infra_blocked", "ChatOpenAI generated non-JSON request") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            raise TaskCallBlocked("infra_blocked", "ChatOpenAI request lacks messages")
        # ChatOpenAI 1.6.5 maps max_tokens to max_completion_tokens.  The
        # existing DeepSeek/SGLang manager contract uses max_tokens instead.
        if "max_completion_tokens" in payload:
            if "max_tokens" in payload:
                raise TaskCallBlocked("infra_blocked", "conflicting output token caps")
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        wire = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        options = {key: value for key, value in payload.items() if key != "messages"}
        try:
            count = self.token_counter(payload["messages"], request_options=options)
        except Exception as error:
            raise TaskCallBlocked("context_blocked", f"exact tokenizer unavailable: {error}") from error
        _write_once(self.call_dir / "wire-request.json", wire)
        _write_once(self.call_dir / "preflight.json", canonical_json({
            "input_tokens": count, "allowance": self.allowance,
            "request_sha256": hashlib.sha256(wire).hexdigest(),
        }).encode("utf-8"))
        if count > self.allowance:
            raise TaskCallBlocked("context_blocked", "Task request exceeds context allowance")
        headers = dict(request.headers)
        headers["content-length"] = str(len(wire))
        headers["accept-encoding"] = "identity"
        outbound = httpx.Request(request.method, request.url, headers=headers, content=wire)
        _write_state(self.call_dir / "sent.json", {
            "status": "sent", "request_sha256": hashlib.sha256(wire).hexdigest(),
            "sent_at_unix_ns": time.time_ns(),
        })
        sent_at = time.monotonic_ns()
        response = self.delegate.handle_request(outbound)
        body = response.read()
        latency_ms = (time.monotonic_ns() - sent_at) / 1_000_000
        _write_once(self.call_dir / "wire-response.json", body)
        _write_once(self.call_dir / "http.json", canonical_json({
            "status_code": response.status_code, "headers": dict(response.headers),
            "latency_ms": latency_ms,
            "response_sha256": hashlib.sha256(body).hexdigest(),
        }).encode("utf-8"))
        return httpx.Response(response.status_code, headers=response.headers,
                              content=body, request=outbound)

    def close(self) -> None:
        self.delegate.close()


@dataclass(frozen=True)
class TaskCallResult:
    status: str
    value: dict[str, Any] | None
    call_key: str
    call_dir: Path
    reason: str | None = None


class TaskChatBoundary:
    """One exact, versioned Task call with stable replay and raw capture."""

    def __init__(self, *, root: Path, base_url: str, model: str,
                 token_counter: Callable[..., int], api_key: str = "EMPTY",
                 context_tokens: int = 270000, output_tokens: int = 16384,
                 safety_tokens: int = 4096,
                 transport_factory: Callable[[], httpx.BaseTransport] | None = None):
        self.root = Path(root)
        self.base_url = base_url
        self.model = model
        self.token_counter = token_counter
        self.api_key = api_key
        self.output_tokens = output_tokens
        self.allowance = context_tokens - output_tokens - safety_tokens
        if self.allowance <= 0:
            raise ValueError("Task context allowance must be positive")
        self.transport_factory = transport_factory or (lambda: httpx.HTTPTransport(retries=0))

    def call(self, *, thread_id: str, stage: str, messages: list[dict[str, str]],
             schema: dict[str, Any], identity: dict[str, Any]) -> TaskCallResult:
        requested = {"thread_id": thread_id, "stage": stage,
                     "messages": messages, "schema": schema, "identity": identity,
                     "model": self.model, "base_url": self.base_url,
                     "temperature": 0, "reasoning_effort": "max",
                     "max_tokens": self.output_tokens}
        call_key = sha256_text(canonical_json(requested))
        call_dir = self.root / "task-calls" / call_key
        _write_once(call_dir / "identity.json", canonical_json(requested).encode("utf-8"))
        raw_path = call_dir / "wire-response.json"
        sent_path = call_dir / "sent.json"
        if raw_path.exists():
            return self._read_result(call_key, call_dir)
        if sent_path.exists():
            return TaskCallResult("transport_uncertain", None, call_key, call_dir,
                                  "request was sent but no complete response was saved")
        transport = _CaptureTransport(delegate=self.transport_factory(), call_dir=call_dir,
                                      token_counter=self.token_counter, allowance=self.allowance)
        try:
            with httpx.Client(transport=transport, timeout=300) as client:
                chat = ChatOpenAI(
                    model=self.model, base_url=self.base_url, api_key=self.api_key,
                    temperature=0, max_tokens=self.output_tokens, reasoning_effort="max",
                    max_retries=0, http_client=client, use_responses_api=False,
                )
                structured = chat.with_structured_output(schema, method="json_schema",
                                                         include_raw=True)
                structured.invoke(messages)
        except TaskCallBlocked as error:
            return TaskCallResult(error.status, None, call_key, call_dir, str(error))
        except Exception as error:
            if raw_path.exists():
                return self._read_result(call_key, call_dir)
            status = "transport_uncertain" if sent_path.exists() and not raw_path.exists() else "infra_blocked"
            return TaskCallResult(status, None, call_key, call_dir,
                                  f"{type(error).__name__}: {error}")
        if not raw_path.exists():
            return TaskCallResult("infra_blocked", None, call_key, call_dir,
                                  "official chat interface returned without captured response")
        return self._read_result(call_key, call_dir)

    @staticmethod
    def _read_result(call_key: str, call_dir: Path) -> TaskCallResult:
        raw = (call_dir / "wire-response.json").read_bytes()
        try:
            response = json.loads(raw)
            http_path = call_dir / "http.json"
            if http_path.is_file():
                http_record = json.loads(http_path.read_text(encoding="utf-8"))
                if http_record.get("response_sha256") != hashlib.sha256(raw).hexdigest():
                    raise ValueError("saved response hash differs from raw wire bytes")
                if http_record.get("status_code") != 200:
                    return TaskCallResult("rejected", None, call_key, call_dir,
                                          f"model HTTP status {http_record.get('status_code')}")
            choice = response["choices"][0]
            message = choice["message"]
            content = message.get("content")
            finish = choice.get("finish_reason")
            if finish == "length":
                try:
                    partial = json.loads(content) if isinstance(content, str) else None
                except (TypeError, ValueError):
                    partial = None
                return TaskCallResult("length", partial if isinstance(partial, dict) else None,
                                      call_key, call_dir, "model reached output cap")
            if not isinstance(content, str) or not content.strip():
                return TaskCallResult("rejected", None, call_key, call_dir,
                                      "complete response has no visible JSON content")
            value = json.loads(content)
            if not isinstance(value, dict):
                raise ValueError("model content is not a JSON object")
            if finish != "stop":
                return TaskCallResult("rejected", value, call_key, call_dir,
                                      f"model finish reason {finish!r} is not complete")
            preflight_path = call_dir / "preflight.json"
            usage = response.get("usage")
            if preflight_path.is_file() and isinstance(usage, dict) and isinstance(usage.get("prompt_tokens"), int):
                preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
                if usage["prompt_tokens"] != preflight.get("input_tokens"):
                    return TaskCallResult("infra_blocked", value, call_key, call_dir,
                                          "exact tokenizer count differs from provider prompt_tokens")
            return TaskCallResult("ok", value, call_key, call_dir)
        except (KeyError, IndexError, TypeError, ValueError) as error:
            return TaskCallResult("rejected", None, call_key, call_dir,
                                  f"malformed model response: {error}")
