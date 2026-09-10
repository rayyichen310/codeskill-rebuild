from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codeskill_rebuild.solver_probe import FullPayloadUsageProbe, PayloadProbeProfile, ServerPayloadTokenCounter


class FakeResponse:
    status = 200

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def synthetic_payload() -> dict:
    return {
        "model": "deepseek-ai/DeepSeek-V4-Flash",
        "messages": [
            {"role": "system", "content": "Synthetic tokenization probe only."},
            {"role": "user", "content": "Return a short acknowledgement."},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "probe_echo",
                    "description": "Synthetic schema used only to count the complete solver payload.",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                },
            }
        ],
        "tool_choice": "auto",
        "temperature": 1.0,
        "top_p": 0.95,
        "reasoning_effort": "high",
        "max_tokens": 256,
        "stream": False,
    }


class SolverProbeTest(unittest.TestCase):
    def test_counter_sends_complete_payload_plus_generation_marker(self) -> None:
        counter = ServerPayloadTokenCounter("http://example.invalid/v1", timeout_seconds=60)
        payload = synthetic_payload()
        with patch("codeskill_rebuild.solver_probe.urlopen", return_value=FakeResponse({"count": 37})) as opened:
            self.assertEqual(counter(payload), 37)
        request = opened.call_args.args[0]
        sent = json.loads(request.data.decode("utf-8"))
        self.assertEqual(sent["tools"], payload["tools"])
        self.assertEqual(sent["tool_choice"], "auto")
        self.assertEqual(sent["max_tokens"], 256)
        self.assertTrue(sent["add_generation_prompt"])
        self.assertEqual(counter.last_exchange["count"], 37)
        self.assertEqual(counter.last_exchange["scope"], "complete_openai_payload_plus_generation_marker")

    def test_probe_reserves_and_finalizes_one_global_ledger_call_after_matching_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            probe = FullPayloadUsageProbe(
                profile=PayloadProbeProfile(base_url="http://example.invalid/v1", model="deepseek-ai/DeepSeek-V4-Flash"),
                run_dir=root / "run",
                ledger_path=root / "ledger.json",
                contract={"version": "v0.10"},
                exact_token_counter=ServerPayloadTokenCounter("http://example.invalid/v1", timeout_seconds=60),
            )
            with patch(
                "codeskill_rebuild.solver_probe.urlopen",
                side_effect=[
                    FakeResponse({"count": 37}),
                    FakeResponse({"usage": {"prompt_tokens": 37, "completion_tokens": 3}, "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}),
                ],
            ):
                result = probe.run(purpose="m3_full_payload_tools_tokenizer_probe", payload=synthetic_payload())
            request_record = json.loads((root / "run" / "model_calls" / "call-0001" / "request.json").read_text())
            response_record = json.loads((root / "run" / "model_calls" / "call-0001" / "response.json").read_text())
            ledger = json.loads((root / "ledger.json").read_text())
        self.assertEqual(result["call_id"], "call-0001")
        self.assertEqual(request_record["request"]["tools"], synthetic_payload()["tools"])
        self.assertEqual(request_record["request"]["tool_choice"], "auto")
        self.assertTrue(response_record["tokenizer_prompt_token_comparison"]["matches"])
        self.assertEqual(ledger["calls"][0]["status"], "succeeded")
        self.assertEqual(ledger["calls"][0]["purpose"], "m3_full_payload_tools_tokenizer_probe")

    def test_tokenizer_failure_writes_preflight_without_reserving_a_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            probe = FullPayloadUsageProbe(
                profile=PayloadProbeProfile(base_url="http://example.invalid/v1", model="deepseek-ai/DeepSeek-V4-Flash"),
                run_dir=root / "run",
                ledger_path=root / "ledger.json",
                contract={"version": "v0.10"},
                exact_token_counter=ServerPayloadTokenCounter("http://example.invalid/v1", timeout_seconds=60),
            )
            with patch("codeskill_rebuild.solver_probe.urlopen", return_value=FakeResponse({"error": "no count"})):
                with self.assertRaisesRegex(RuntimeError, "tokenize"):
                    probe.run(purpose="m3_full_payload_tools_tokenizer_probe", payload=synthetic_payload())
            preflight = json.loads((root / "run" / "model_calls" / "call-0001" / "preflight.json").read_text())
        self.assertEqual(preflight["classification"], "tokenizer_unavailable")
        self.assertFalse((root / "ledger.json").exists())


if __name__ == "__main__":
    unittest.main()
