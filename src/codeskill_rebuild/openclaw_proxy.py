"""Isolated OpenAI-compatible proxy around :mod:`openclaw_overlay`.

The proxy is deliberately per trial.  It keeps native OpenClaw state outside
of its process, rebuilds only durable overlay messages in the *forwarded*
payload, and writes request/response evidence without copying HTTP credentials.
It has no automatic compaction detector: a relocation is possible only when a
caller provides native-session evidence bound to the current transition.
"""

from __future__ import annotations

import io
import json
import math
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .openclaw_overlay import DurableOverlay, OverlayAnchorEvidenceError, OverlayEventSkillBudgetError, OverlayInputLimitError
from .openclaw_native_summary import NativeSummaryPermit, NativeSummaryPermitError, NativeSummaryPermitGate, NormalCallBoundary
from .types import read_json, utc_now, write_json


class ProxyError(RuntimeError):
    pass


class PayloadCounter(Protocol):
    """Count the complete serialized solver payload before it is forwarded."""

    def __call__(self, payload: dict[str, Any]) -> int: ...


CompactionEvidenceProvider = Callable[[str, int, int, dict[str, Any]], dict[str, Any] | None]


@dataclass
class UpstreamStream:
    status: int
    headers: dict[str, str]
    chunks: Iterator[bytes]
    close: Callable[[], None]


class UpstreamTransport(Protocol):
    endpoint: str

    def open(self, payload: dict[str, Any], *, timeout_seconds: int | None = None) -> UpstreamStream: ...


class UrllibUpstreamTransport:
    """Minimal streaming transport; credentials are configured, never logged."""

    def __init__(self, endpoint: str, *, timeout_seconds: int, authorization: str | None = None) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.endpoint = endpoint.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._authorization = authorization

    def open(self, payload: dict[str, Any], *, timeout_seconds: int | None = None) -> UpstreamStream:
        effective_timeout = self.timeout_seconds if timeout_seconds is None else min(self.timeout_seconds, timeout_seconds)
        if effective_timeout <= 0:
            raise ProxyError("upstream timeout budget is exhausted before opening the request")
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream, application/json"}
        if self._authorization:
            headers["Authorization"] = self._authorization
        request = Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            response = urlopen(request, timeout=effective_timeout)
        except HTTPError as error:
            raw = error.read()
            return UpstreamStream(
                status=error.code,
                headers={key.lower(): value for key, value in error.headers.items()},
                chunks=iter((raw,)),
                close=lambda: None,
            )
        except (URLError, TimeoutError, OSError) as error:
            raise ProxyError(f"upstream transport error: {type(error).__name__}: {error}") from error

        def chunks() -> Iterator[bytes]:
            while True:
                block = response.read(8192)
                if not block:
                    return
                yield block

        return UpstreamStream(
            status=response.status,
            headers={key.lower(): value for key, value in response.headers.items()},
            chunks=chunks(),
            close=response.close,
        )


def _response_usage(raw: bytes, *, streaming: bool) -> dict[str, Any] | None:
    """Read the optional OpenAI usage object without changing response bytes."""
    text = raw.decode("utf-8", errors="replace")
    candidates: list[Any] = []
    if streaming:
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            value = line.removeprefix("data:").strip()
            if value == "[DONE]":
                continue
            try:
                candidates.append(json.loads(value))
            except json.JSONDecodeError:
                continue
    else:
        try:
            candidates.append(json.loads(text))
        except json.JSONDecodeError:
            return None
    for candidate in reversed(candidates):
        usage = candidate.get("usage") if isinstance(candidate, dict) else None
        if isinstance(usage, dict):
            return usage
    return None


def _http_error_body(*, message: str, code: str) -> bytes:
    return json.dumps(
        {"error": {"message": message, "type": "invalid_request_error", "code": code}}, ensure_ascii=False
    ).encode("utf-8")


class ProxyResponse:
    """One response which is copied chunk-for-chunk to the downstream client."""

    def __init__(
        self,
        *,
        status: int,
        headers: dict[str, str],
        chunks: Iterator[bytes],
        finish: Callable[[bytes, BaseException | None], None],
    ) -> None:
        self.status = status
        self.headers = headers
        self._chunks = chunks
        self._finish = finish
        self._consumed = False

    def iter_bytes(self) -> Iterator[bytes]:
        if self._consumed:
            raise ProxyError("a proxy response can only be consumed once")
        self._consumed = True
        seen: list[bytes] = []
        error: BaseException | None = None
        try:
            for chunk in self._chunks:
                if not isinstance(chunk, bytes):
                    raise ProxyError("upstream stream yielded a non-byte chunk")
                seen.append(chunk)
                yield chunk
        except BaseException as caught:
            error = caught
            raise
        finally:
            self._finish(b"".join(seen), error)


class DurableProxyService:
    """Forward one trial's requests through a :class:`DurableOverlay`."""

    def __init__(
        self,
        *,
        overlay: DurableOverlay,
        transport: UpstreamTransport,
        compaction_evidence_provider: CompactionEvidenceProvider | None = None,
        native_summary_permit_gate: NativeSummaryPermitGate | None = None,
        max_forwarded_requests: int | None = None,
        max_output_tokens: int | None = None,
        trial_deadline_monotonic: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_forwarded_requests is not None and max_forwarded_requests <= 0:
            raise ValueError("max_forwarded_requests must be positive when set")
        if max_output_tokens is not None and max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive when set")
        self.overlay = overlay
        self.transport = transport
        self.compaction_evidence_provider = compaction_evidence_provider
        self.native_summary_permit_gate = native_summary_permit_gate
        self.max_forwarded_requests = max_forwarded_requests
        self.max_output_tokens = max_output_tokens
        self.trial_deadline_monotonic = trial_deadline_monotonic
        self.clock = clock
        self._lock = threading.Lock()

    def _attempt_path(self, record: dict[str, Any]) -> Path:
        return self.overlay.evidence_dir / "upstream_requests" / f"attempt-{int(record['attempt_ordinal']):04d}.json"

    def _rewrite_record(self, record: dict[str, Any]) -> None:
        write_json(self._attempt_path(record), record)

    def _local_error(self, *, status: int, body: bytes, record: dict[str, Any] | None = None) -> ProxyResponse:
        if record is not None:
            record.setdefault("proxy_outcome", "not_forwarded")
            self._rewrite_record(record)
        return ProxyResponse(
            status=status,
            headers={"content-type": "application/json; charset=utf-8", "content-length": str(len(body))},
            chunks=iter((body,)),
            finish=lambda _raw, _error: None,
        )

    def forward(self, payload: dict[str, Any]) -> ProxyResponse:
        """Synchronously select/re-overlay, then create a streaming response.

        One proxy has a lock because its overlay's event selector and durable
        state must be serialized with the request boundary.  Separate arms use
        distinct proxy instances/state directories.
        """
        if not isinstance(payload, dict):
            return self._local_error(status=400, body=_http_error_body(message="JSON request must be an object", code="invalid_payload"))
        # OpenAI credentials belong in a header.  Rejecting top-level keys is
        # narrow by design; task/tool text is never regex-redacted or changed.
        forbidden = {"api_key", "authorization", "x-api-key"}.intersection(payload)
        if forbidden:
            return self._local_error(
                status=400,
                body=_http_error_body(message="credentials must not be present in the JSON payload", code="credential_in_payload"),
            )
        with self._lock:
            def reject_limit(*, status: int, code: str, message: str, outcome: str, details: dict[str, Any]) -> ProxyResponse:
                record = self.overlay.record_proxy_rejection(
                    payload,
                    outcome=outcome,
                    code=code,
                    message=message,
                    details=details,
                )
                return self._local_error(status=status, body=_http_error_body(message=message, code=code), record=record)

            if self.max_forwarded_requests is not None and int(self.overlay.state.get("request_count", 0)) >= self.max_forwarded_requests:
                return reject_limit(
                    status=429,
                    code="codeskill_request_limit_reached",
                    message=f"CODESKILL forwarded solver request limit {self.max_forwarded_requests} reached",
                    outcome="request_limit_rejected",
                    details={"max_forwarded_requests": self.max_forwarded_requests, "forwarded_request_count": int(self.overlay.state.get("request_count", 0))},
                )
            if self.max_output_tokens is not None:
                # OpenClaw 2026.9.3 uses the current OpenAI spelling
                # ``max_completion_tokens``.  Keep accepting the legacy
                # ``max_tokens`` field for the existing R012 callers, while
                # applying the same hard cap to either field and rejecting a
                # payload that supplies an invalid value in either spelling.
                requested_limits = {
                    field: payload[field]
                    for field in ("max_tokens", "max_completion_tokens")
                    if field in payload
                }
                valid_limits = bool(requested_limits) and all(
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and 0 < value <= self.max_output_tokens
                    for value in requested_limits.values()
                )
                if not valid_limits:
                    return reject_limit(
                        status=400,
                        code="codeskill_max_tokens_exceeded",
                        message=(
                            "CODESKILL max_tokens or max_completion_tokens must be "
                            f"an integer from 1 through {self.max_output_tokens}"
                        ),
                        outcome="output_limit_rejected",
                        details={
                            "max_output_tokens": self.max_output_tokens,
                            "requested_max_tokens": payload.get("max_tokens"),
                            "requested_max_completion_tokens": payload.get("max_completion_tokens"),
                        },
                    )
            remaining_timeout: int | None = None
            if self.trial_deadline_monotonic is not None:
                remaining = self.trial_deadline_monotonic - self.clock()
                if remaining <= 0:
                    return reject_limit(
                        status=408,
                        code="codeskill_trial_deadline_exceeded",
                        message="CODESKILL trial deadline elapsed before the upstream solver request",
                        outcome="trial_deadline_rejected",
                        details={"trial_deadline_monotonic": self.trial_deadline_monotonic, "observed_monotonic": self.clock()},
                    )
                remaining_timeout = max(1, math.ceil(remaining))
            next_attempt = int(self.overlay.state.get("attempt_count", 0)) + 1
            try:
                compaction_evidence = (
                    self.compaction_evidence_provider(
                        self.overlay.trial_id,
                        int(self.overlay.state.get("request_count", 0)),
                        next_attempt,
                        payload,
                    )
                    if self.compaction_evidence_provider
                    else None
                )
            except Exception as error:
                # The native-session observer is part of the evidence gate,
                # rather than a best-effort telemetry source.  Continuing
                # after malformed JSONL, an unproved session rotation, or an
                # inaccessible session file could later misplace an event
                # prior, so retain the native request and stop before I/O.
                return reject_limit(
                    status=409,
                    code="codeskill_native_compaction_evidence_error",
                    message=f"CODESKILL native compaction evidence unavailable: {type(error).__name__}: {error}",
                    outcome="native_compaction_evidence_rejected",
                    details={"provider_error_type": type(error).__name__, "provider_error": str(error)},
                )

            try:
                native_summary_decision = (
                    self.native_summary_permit_gate.authorize(compaction_evidence=compaction_evidence)
                    if self.native_summary_permit_gate is not None
                    else None
                )
            except NativeSummaryPermitError as error:
                return reject_limit(
                    status=409,
                    code="codeskill_native_summary_permit_error",
                    message=f"CODESKILL native summary permit unavailable: {error}",
                    outcome="native_summary_permit_rejected",
                    details={"permit_error": str(error)},
                )

            def confirm_native_transition(record: dict[str, Any]) -> ProxyResponse | None:
                """Finalize detector evidence after its overlay attempt is durable.

                A session-JSONL detector may intentionally keep a newly seen
                compaction pending.  It is consumed only once this exact
                overlay attempt has an on-disk disposition, so a failed
                preflight cannot erase proof required by a retry.
                """
                if not isinstance(compaction_evidence, dict) or self.compaction_evidence_provider is None:
                    return None
                confirm = getattr(self.compaction_evidence_provider, "confirm_transition", None)
                if not callable(confirm):
                    return None
                try:
                    confirm(
                        compaction_evidence,
                        record,
                        overlay_attempt_path=self._attempt_path(record),
                    )
                except Exception as error:
                    record.update(
                        {
                            "proxy_outcome": "native_compaction_confirmation_rejected",
                            "error_code": "codeskill_native_compaction_confirmation_error",
                            "error": f"{type(error).__name__}: {error}",
                        }
                    )
                    self._rewrite_record(record)
                    return self._local_error(
                        status=409,
                        body=_http_error_body(
                            message=f"CODESKILL native compaction evidence confirmation failed: {type(error).__name__}: {error}",
                            code="codeskill_native_compaction_confirmation_error",
                        ),
                        record=record,
                    )
                return None

            native_summary_permit = native_summary_decision if isinstance(native_summary_decision, NativeSummaryPermit) else None
            normal_call_boundary = native_summary_decision if isinstance(native_summary_decision, NormalCallBoundary) else None
            if native_summary_permit is not None:
                try:
                    forwarded, record = self.overlay.prepare_native_summary(
                        payload,
                        permit=native_summary_permit.evidence(),
                    )
                except BaseException as error:
                    return reject_limit(
                        status=500,
                        code="codeskill_native_summary_prepare_error",
                        message=f"CODESKILL native summary preparation failed: {type(error).__name__}: {error}",
                        outcome="native_summary_prepare_rejected",
                        details={"error_type": type(error).__name__, "error": str(error)},
                    )
            else:
                try:
                    forwarded, record = self.overlay.prepare(payload, compaction_evidence=compaction_evidence)
                except OverlayInputLimitError as error:
                    attempt_path = self.overlay.evidence_dir / "upstream_requests" / f"attempt-{next_attempt:04d}.json"
                    if attempt_path.exists():
                        confirmation = confirm_native_transition(read_json(attempt_path))
                        if confirmation is not None:
                            return confirmation
                    # This exact phrase is deliberately one of OpenClaw's own
                    # generic overflow matcher forms.  It represents only a real
                    # complete-payload cap breach, never a missing durable anchor.
                    return self._local_error(
                        status=400,
                        body=_http_error_body(
                            message=f"Context length exceeded: {error}",
                            code="context_length_exceeded",
                        ),
                    )
                except OverlayEventSkillBudgetError as error:
                    attempt_path = self.overlay.evidence_dir / "upstream_requests" / f"attempt-{next_attempt:04d}.json"
                    if attempt_path.exists():
                        confirmation = confirm_native_transition(read_json(attempt_path))
                        if confirmation is not None:
                            return confirmation
                    return self._local_error(
                        status=409,
                        body=_http_error_body(
                            message=f"CODESKILL frozen event skill budget exceeded: {error}",
                            code="codeskill_event_skill_budget_exceeded",
                        ),
                    )
                except OverlayAnchorEvidenceError as error:
                    attempt_path = self.overlay.evidence_dir / "upstream_requests" / f"attempt-{next_attempt:04d}.json"
                    if attempt_path.exists():
                        confirmation = confirm_native_transition(read_json(attempt_path))
                        if confirmation is not None:
                            return confirmation
                    # A lost anchor without native compaction evidence is a data
                    # integrity failure.  It must not be misclassified as an
                    # overflow, or OpenClaw could compact and silently continue
                    # with unprovable prior placement.
                    return self._local_error(
                        status=409,
                        body=_http_error_body(
                            message=f"CODESKILL durable overlay anchor evidence unavailable: {error}",
                            code="codeskill_anchor_evidence_missing",
                        ),
                    )
                except BaseException as error:
                    attempt_path = self.overlay.evidence_dir / "upstream_requests" / f"attempt-{next_attempt:04d}.json"
                    if attempt_path.exists():
                        confirmation = confirm_native_transition(read_json(attempt_path))
                        if confirmation is not None:
                            return confirmation
                    return self._local_error(
                        status=500,
                        body=_http_error_body(message=f"overlay failure: {type(error).__name__}: {error}", code="codeskill_overlay_error"),
                    )
                if normal_call_boundary is not None:
                    record["normal_call_boundary"] = normal_call_boundary.evidence()
                    self._rewrite_record(record)
                confirmation = confirm_native_transition(record)
                if confirmation is not None:
                    return confirmation
            # Selection, durable-state I/O, and exact tokenization above may
            # take appreciable time.  The early check prevents starting work
            # after an already-expired trial, while this second check prevents
            # a completed overlay pass from opening an upstream socket after
            # the trial deadline.  Keep the prepared attempt ordinal: it is a
            # real request-boundary attempt even though no upstream bytes were
            # opened, and a second rejection record would misstate ordering.
            if self.trial_deadline_monotonic is not None:
                observed_monotonic = self.clock()
                remaining = self.trial_deadline_monotonic - observed_monotonic
                if remaining <= 0:
                    message = "CODESKILL trial deadline elapsed after overlay preparation and before the upstream solver request"
                    record.update(
                        {
                            "proxy_outcome": "trial_deadline_rejected",
                            "error_code": "codeskill_trial_deadline_exceeded",
                            "error": message,
                            "deadline_recheck": {
                                "phase": "after_overlay_prepare_before_upstream_open",
                                "trial_deadline_monotonic": self.trial_deadline_monotonic,
                                "observed_monotonic": observed_monotonic,
                            },
                        }
                    )
                    self._rewrite_record(record)
                    return self._local_error(
                        status=408,
                        body=_http_error_body(message=message, code="codeskill_trial_deadline_exceeded"),
                        record=record,
                    )
                remaining_timeout = max(1, math.ceil(remaining))
            try:
                stream = self.transport.open(forwarded, timeout_seconds=remaining_timeout)
            except ProxyError as error:
                record["proxy_outcome"] = "transport_open_error"
                record["proxy_error"] = str(error)
                self._rewrite_record(record)
                return self._local_error(
                    status=502,
                    body=_http_error_body(message=str(error), code="upstream_transport_error"),
                    record=record,
                )
            record["proxy_outcome"] = "upstream_opened"
            record["upstream_timeout_seconds"] = remaining_timeout
            record["upstream"] = {"endpoint": self.transport.endpoint, "http_status": stream.status, "headers": {key: value for key, value in stream.headers.items() if key.lower() in {"content-type", "x-request-id"}}}
            self._rewrite_record(record)

        streaming = payload.get("stream") is True

        def finish(raw: bytes, error: BaseException | None) -> None:
            try:
                stream.close()
            finally:
                record["finished_at_utc"] = utc_now()
                record["raw_upstream_response"] = raw.decode("utf-8", errors="replace")
                record["streaming"] = streaming
                if error is not None:
                    record["proxy_outcome"] = "stream_copy_error"
                    record["proxy_error"] = f"{type(error).__name__}: {error}"
                else:
                    record["proxy_outcome"] = "stream_forwarded"
                usage = _response_usage(raw, streaming=streaming)
                record["upstream_usage"] = usage
                expected = record.get("exact_forwarded_input_tokens")
                observed = usage.get("prompt_tokens") if isinstance(usage, dict) else None
                record["tokenizer_prompt_token_comparison"] = {
                    "preflight_complete_payload_tokens": expected,
                    "upstream_usage_prompt_tokens": observed,
                    "matches": isinstance(expected, int) and isinstance(observed, int) and expected == observed,
                }
                if isinstance(observed, int) and observed != expected:
                    record["classification"] = "tokenizer_prompt_count_mismatch"
                self._rewrite_record(record)

        headers = {
            key: value
            for key, value in stream.headers.items()
            if key.lower() in {"content-type", "cache-control", "x-request-id"}
        }
        headers.setdefault("content-type", "text/event-stream" if streaming else "application/json")
        return ProxyResponse(status=stream.status, headers=headers, chunks=stream.chunks, finish=finish)


def handler_for(service: DurableProxyService) -> type[BaseHTTPRequestHandler]:
    """Create an HTTP handler bound to one isolated durable proxy service."""

    class OpenAIProxyHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path not in {"/v1/chat/completions", "/chat/completions"}:
                self.send_error(404, "only the OpenAI chat-completions endpoint is available")
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
                raw = self.rfile.read(length)
                payload = json.loads(raw)
            except (ValueError, json.JSONDecodeError) as error:
                body = _http_error_body(message=f"invalid JSON request: {error}", code="invalid_json")
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            response = service.forward(payload)
            self.send_response(response.status)
            for key, value in response.headers.items():
                self.send_header(key, value)
            # Streaming responses intentionally have no content length.  Close
            # the downstream connection after `[DONE]`/EOF so ordinary OpenAI
            # clients can finish reading without a fake buffering layer.
            if "content-length" not in {key.lower() for key in response.headers}:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            try:
                for chunk in response.iter_bytes():
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # The response finalizer records this as stream-copy evidence.
                return

        def log_message(self, _format: str, *_args: Any) -> None:
            # HTTP access logs may contain task text in URLs/query strings;
            # request-level evidence is written by the service instead.
            return

    return OpenAIProxyHandler


def serve_in_thread(service: DurableProxyService, *, host: str = "127.0.0.1", port: int = 0) -> tuple[ThreadingHTTPServer, threading.Thread]:
    """Start a local proxy for tests or a bounded single-trial runner."""
    server = ThreadingHTTPServer((host, port), handler_for(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
