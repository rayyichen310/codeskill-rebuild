from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codeskill_rebuild.context import ContextBlocked
from codeskill_rebuild.manager import ManagerCallError, ManagerClient, ManagerProfile, ServerMessageTokenCounter, update_development_ledger_limit


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


class ExactCounter:
    method = "fixture_exact_counter"

    def __init__(self) -> None:
        self.request_options: dict | None = None

    def __call__(self, messages: list[dict], *, request_options: dict | None = None) -> int:
        self.request_options = dict(request_options or {})
        return 10


class FixedCounter:
    method = "fixture_fixed_counter"

    def __init__(self, count: int) -> None:
        self.count = count
        self.request_options: dict | None = None

    def __call__(self, messages: list[dict], *, request_options: dict | None = None) -> int:
        self.request_options = dict(request_options or {})
        return self.count


class MessagesOnlyCounter:
    method = "fixture_messages_only_counter"

    def __call__(self, messages: list[dict]) -> int:
        return 10


class TokenizeResponse(FakeResponse):
    pass


class ManagerTest(unittest.TestCase):
    def client(self, root: Path, *, limit: int | None = 30, counter: object | None = None) -> ManagerClient:
        return ManagerClient(
            ManagerProfile(base_url="http://example.invalid/v1", model="test", max_total_calls=limit),
            root / "run",
            {"version": "v0.4"},
            root / "ledger.json",
            counter,
        )

    def test_missing_exact_counter_writes_context_blocked_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = self.client(root)
            with self.assertRaises(ContextBlocked):
                client.call_json(purpose="description", messages=[{"role": "user", "content": "x"}])
            record = json.loads((root / "run" / "model_calls" / "call-0001" / "preflight.json").read_text())
        self.assertEqual(record["classification"], "context_blocked")

    def test_exact_context_boundary_accepts_equality_and_blocks_only_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            at_limit = self.client(root, counter=FixedCounter(512_000))
            self.assertEqual(at_limit._preflight([{"role": "user", "content": "x"}])["allowed_estimated_input_tokens"], 512_000)
            over_limit = self.client(root / "over", counter=FixedCounter(512_001))
            with self.assertRaises(ContextBlocked):
                over_limit._preflight([{"role": "user", "content": "x"}])

    def test_length_finish_is_preserved_and_rejected(self) -> None:
        response = {"choices": [{"message": {"content": "{}"}, "finish_reason": "length"}], "usage": {"prompt_tokens": 1}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = self.client(root, counter=ExactCounter())
            with patch("codeskill_rebuild.manager.urlopen", return_value=FakeResponse(response)):
                with self.assertRaisesRegex(ManagerCallError, "length limit"):
                    client.call_json(purpose="event_extract", messages=[{"role": "user", "content": "x"}])
            record = json.loads((root / "run" / "model_calls" / "call-0001" / "response.json").read_text())
            ledger = json.loads((root / "ledger.json").read_text())
        self.assertEqual(record["classification"], "model_output_truncated")
        self.assertEqual(len(ledger["calls"]), 1)
        self.assertEqual(ledger["calls"][0]["status"], "truncated")

    def test_persistent_call_budget_is_enforced(self) -> None:
        response = {"choices": [{"message": {"content": "{\"action\":\"skip\"}"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = self.client(root, limit=1, counter=ExactCounter())
            with patch("codeskill_rebuild.manager.urlopen", return_value=FakeResponse(response)):
                client.call_json(purpose="event_extract", messages=[{"role": "user", "content": "x"}])
                with self.assertRaisesRegex(ManagerCallError, "budget exhausted"):
                    client.call_json(purpose="repair", messages=[{"role": "user", "content": "x"}])

    def test_approved_limit_change_preserves_prior_call_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "ledger.json"
            ledger_path.write_text(json.dumps({"schema_version": 1, "limit": 60, "calls": [{"call_id": "call-0001", "status": "succeeded"}]}), encoding="utf-8")
            updated = update_development_ledger_limit(
                ledger_path,
                new_limit=100,
                reason="R009 approved common-bank pipeline",
                contract={"version": "v0.8", "reproduction_spec_sha256": "spec", "research_decisions_sha256": "decisions"},
            )
        self.assertEqual(updated["limit"], 100)
        self.assertEqual(updated["calls"], [{"call_id": "call-0001", "status": "succeeded"}])
        self.assertEqual(updated["limit_history"][0]["previous_limit"], 60)
        self.assertEqual(updated["limit_history"][0]["calls_preserved"], 1)

    def test_unlimited_policy_is_explicit_and_preserves_finite_ledger_history(self) -> None:
        response = {"choices": [{"message": {"content": "{\"action\":\"skip\"}"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "ledger.json"
            ledger_path.write_text(json.dumps({"schema_version": 1, "limit": 100, "calls": [{"call_id": "historic", "status": "succeeded"}]}), encoding="utf-8")
            updated = update_development_ledger_limit(
                ledger_path,
                new_limit=None,
                reason="R014 user-authorized unlimited development calls",
                contract={"version": "v0.12", "reproduction_spec_sha256": "spec", "research_decisions_sha256": "decisions"},
            )
            client = self.client(root, limit=None, counter=ExactCounter())
            with patch("codeskill_rebuild.manager.urlopen", return_value=FakeResponse(response)):
                client.call_json(purpose="event_extract", messages=[{"role": "user", "content": "x"}])
                client.call_json(purpose="event_extract", messages=[{"role": "user", "content": "y"}])
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(updated["limit"], "unlimited")
        self.assertEqual(updated["calls"][0]["call_id"], "historic")
        self.assertEqual(updated["limit_history"][0]["previous_limit"], 100)
        self.assertEqual(updated["limit_history"][0]["new_limit"], "unlimited")
        self.assertEqual(ledger["limit"], "unlimited")
        self.assertEqual(len(ledger["calls"]), 3)

    def test_unlimited_profile_requires_explicit_ledger_activation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ledger.json").write_text(json.dumps({"schema_version": 1, "limit": 100, "calls": []}), encoding="utf-8")
            client = self.client(root, limit=None, counter=ExactCounter())
            with self.assertRaisesRegex(ManagerCallError, "explicitly activate unlimited"):
                client._reserve_call("call-0001", "event_extract")

    def test_finite_legacy_profile_preserves_an_activated_unlimited_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "ledger.json"
            update_development_ledger_limit(
                ledger_path,
                new_limit=None,
                reason="R014 explicit activation",
                contract={"version": "v0.12"},
            )
            legacy = self.client(root, limit=100, counter=ExactCounter())
            legacy._reserve_call("call-0001", "legacy_evolution")
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(ledger["limit"], "unlimited")
        self.assertEqual(ledger["calls"][0]["purpose"], "legacy_evolution")

    def test_explicit_unlimited_activation_creates_an_empty_durable_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "new-ledger.json"
            ledger = update_development_ledger_limit(
                ledger_path,
                new_limit=None,
                reason="R014 explicit activation before any manager call",
                contract={"version": "v0.12"},
            )
            durable = json.loads(ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(ledger["limit"], "unlimited")
        self.assertEqual(durable["limit_history"][0]["previous_limit"], None)
        self.assertEqual(durable["limit_history"][0]["new_limit"], "unlimited")

    def test_server_message_counter_preserves_the_tokenize_exchange(self) -> None:
        counter = ServerMessageTokenCounter("http://example.invalid/v1")
        with patch("codeskill_rebuild.manager.urlopen", return_value=TokenizeResponse({"count": 17})) as opened:
            self.assertEqual(
                counter(
                    [{"role": "user", "content": "x"}],
                    request_options={
                        "model": "deepseek-ai/DeepSeek-V4-Flash",
                        "temperature": 0.0,
                        "max_tokens": 8192,
                        "response_format": {"type": "json_object"},
                        "reasoning_effort": "max",
                    },
                ),
                17,
            )
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "http://example.invalid/v1/tokenize")
        self.assertEqual(counter.last_exchange["count"], 17)
        self.assertTrue(counter.last_exchange["request"]["add_generation_prompt"])
        sent = json.loads(request.data.decode("utf-8"))
        self.assertEqual(sent["model"], "deepseek-ai/DeepSeek-V4-Flash")
        self.assertEqual(sent["reasoning_effort"], "max")
        self.assertEqual(sent["response_format"], {"type": "json_object"})

    def test_call_json_preflights_the_complete_effective_request_shape(self) -> None:
        response = {
            "choices": [{"message": {"content": '{"action":"skip"}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10},
        }
        messages = [
            {"role": "system", "content": "Return one JSON object."},
            {"role": "user", "content": '{"task":"saved request shape"}'},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            counter = ExactCounter()
            client = self.client(root, counter=counter)
            with patch("codeskill_rebuild.manager.urlopen", return_value=FakeResponse(response)):
                result = client.call_json(purpose="event_extract", messages=messages)
            request_record = json.loads((root / "run" / "model_calls" / "call-0001" / "request.json").read_text())
            response_record = json.loads((root / "run" / "model_calls" / "call-0001" / "response.json").read_text())
        expected_options = {
            "model": "test",
            "temperature": 0.0,
            "max_tokens": 8192,
            "response_format": {"type": "json_object"},
            "reasoning_effort": "high",
        }
        self.assertEqual(result["json"], {"action": "skip"})
        self.assertEqual(response_record["classification"], "model_output_complete")
        self.assertEqual(counter.request_options, expected_options)
        self.assertEqual(request_record["preflight"]["request_options"], expected_options)
        self.assertEqual(request_record["request"], {"messages": messages, **expected_options})

    def test_messages_only_counter_is_rejected_for_effective_request_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = self.client(root, counter=MessagesOnlyCounter())
            with self.assertRaises(ContextBlocked) as raised:
                client.call_json(purpose="event_extract", messages=[{"role": "user", "content": "x"}])
            preflight = json.loads((root / "run" / "model_calls" / "call-0001" / "preflight.json").read_text())
        self.assertEqual(preflight["classification"], "context_blocked")
        self.assertIn("tokenizer_incompatible_with_effective_request", str(raised.exception))

    def test_malformed_model_json_finalizes_reservation_with_distinct_classification(self) -> None:
        response = {"choices": [{"message": {"content": "not-json"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = self.client(root, counter=ExactCounter())
            with patch("codeskill_rebuild.manager.urlopen", return_value=FakeResponse(response)):
                with self.assertRaisesRegex(ManagerCallError, "malformed manager JSON"):
                    client.call_json(purpose="event_extract", messages=[{"role": "user", "content": "x"}])
            ledger = json.loads((root / "ledger.json").read_text())
            record = json.loads((root / "run" / "model_calls" / "call-0001" / "response.json").read_text())
        self.assertEqual(record["classification"], "model_output_malformed")
        self.assertEqual(record["raw_response"], json.dumps(response))
        self.assertEqual(ledger["calls"][0]["status"], "malformed_output")

    def test_empty_and_non_stop_outputs_are_not_parsed_as_success(self) -> None:
        cases = [
            (
                "empty",
                {"choices": [{"message": {"content": "  "}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10}},
                "model_output_empty",
                "empty_output",
            ),
            (
                "incomplete",
                {"choices": [{"message": {"content": '{"action":"no_related_group","reason":"partial"}'}, "finish_reason": "content_filter"}], "usage": {"prompt_tokens": 10}},
                "model_output_incomplete",
                "incomplete_output",
            ),
            (
                "malformed_shape",
                {"choices": [], "usage": {"prompt_tokens": 10}},
                "model_output_malformed",
                "malformed_output",
            ),
            (
                "null_choice",
                {"choices": [None], "usage": {"prompt_tokens": 10}},
                "model_output_malformed",
                "malformed_output",
            ),
            (
                "string_choice",
                {"choices": ["bad"], "usage": {"prompt_tokens": 10}},
                "model_output_malformed",
                "malformed_output",
            ),
        ]
        for name, response, classification, status in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                client = self.client(root, counter=ExactCounter())
                with patch("codeskill_rebuild.manager.urlopen", return_value=FakeResponse(response)):
                    with self.assertRaises(ManagerCallError):
                        client.call_json(purpose="task_pairing", messages=[{"role": "user", "content": "x"}])
                record = json.loads((root / "run" / "model_calls" / "call-0001" / "response.json").read_text())
                ledger = json.loads((root / "ledger.json").read_text())
            self.assertEqual(record["classification"], classification)
            self.assertEqual(record["raw_response"], json.dumps(response))
            self.assertEqual(ledger["calls"][0]["status"], status)

    def test_prompt_token_mismatch_is_preserved_and_rejected(self) -> None:
        response = {"choices": [{"message": {"content": "{\"action\":\"skip\"}"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 9}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = self.client(root, counter=ExactCounter())
            with patch("codeskill_rebuild.manager.urlopen", return_value=FakeResponse(response)):
                with self.assertRaisesRegex(ManagerCallError, "tokens differ"):
                    client.call_json(purpose="description", messages=[{"role": "user", "content": "x"}])
            record = json.loads((root / "run" / "model_calls" / "call-0001" / "response.json").read_text())
            ledger = json.loads((root / "ledger.json").read_text())
        self.assertFalse(record["tokenizer_prompt_token_comparison"]["matches"])
        self.assertEqual(record["classification"], "tokenizer_prompt_count_mismatch")
        self.assertEqual(ledger["calls"][0]["status"], "tokenizer_mismatch")
