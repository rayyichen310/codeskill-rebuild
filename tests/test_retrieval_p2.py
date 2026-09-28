from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any

from codeskill_rebuild.openclaw_sidecar_retrieval import FrozenBankSelectors, SidecarRetrievalError
from codeskill_rebuild.r012_execution import profile_sha256
from codeskill_rebuild.relevance_judge import JUDGE_PROMPT, JevJudge, JudgeConfigError
from codeskill_rebuild.retrieval_query import (
    QUERY_CONSTRUCTION,
    event_query_fields,
    judge_state,
    plain_action,
    problem_statement,
    repo_context,
    task_query_fields,
)
import tests.test_openclaw_sidecar_retrieval as legacy

SWE_USER = (
    "[Mon 2026-09-28 02:50 UTC] You are working directly inside a development environment.\n"
    "IMPORTANT - ENVIRONMENT RULES: do not commit.\n\n<issue_description>\n1-element tuple rendered incorrectly\n"
    "</issue_description>\n\nFollow these phases..."
)
TB2_USER = "[Tue 2026-09-15 13:32 UTC] You are given a COBOL program located at /app/src/program.cbl."


def assistant(command: str, reasoning: str = "") -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "reasoning_content": reasoning,
        "tool_calls": [{"id": "call1", "type": "function", "function": {"name": "exec", "arguments": json.dumps({"command": command})}}],
    }


class QueryConstructionTest(unittest.TestCase):
    def test_task_query_keeps_only_the_problem_and_repo(self) -> None:
        self.assertEqual(problem_statement(SWE_USER), "1-element tuple rendered incorrectly")
        self.assertEqual(problem_statement(TB2_USER), "You are given a COBOL program located at /app/src/program.cbl.")
        self.assertEqual(repo_context("sphinx-doc__sphinx-9367"), "sphinx-doc/sphinx")
        self.assertEqual(repo_context("cobol-modernization"), "")
        self.assertEqual(
            task_query_fields(SWE_USER, "sphinx-doc__sphinx-9367"),
            {"goal_problem": "1-element tuple rendered incorrectly", "repo_context": "sphinx-doc/sphinx"},
        )

    def test_event_query_keeps_errors_and_tail_of_long_output(self) -> None:
        output = "header line\n" + "x" * 5000 + "\n/usr/bin/sh: 1: xxd: not found\nCommand not found"
        fields = event_query_fields(TB2_USER, assistant("head -c 20 f | xxd", "check the bytes"), [{"role": "tool", "content": output}])
        self.assertTrue(fields["observation_errors_tests"].startswith("/usr/bin/sh: 1: xxd: not found"))
        self.assertNotIn("header line", fields["observation_errors_tests"])
        self.assertTrue(fields["observation_errors_tests"].endswith("Command not found"))
        self.assertEqual(fields["recent_action"], "head -c 20 f | xxd")
        self.assertEqual(fields["public_reasoning"], "check the bytes")
        self.assertEqual(fields["task_context"], problem_statement(TB2_USER))

    def test_non_command_tool_is_rendered_as_name_and_arguments(self) -> None:
        message = {"tool_calls": [{"function": {"name": "read", "arguments": json.dumps({"path": "/app/a.py"})}}]}
        self.assertEqual(plain_action(message), 'read {"path": "/app/a.py"}')

    def test_task_judge_state_has_no_action_fields(self) -> None:
        self.assertEqual(judge_state(TB2_USER, None, []), {"task": problem_statement(TB2_USER)})


def answer(choice: str) -> dict[str, Any]:
    return {"model": "jev-1.13.0", "answers": {"pick": {"type": "choice", "choice": choice, "confidence": 0.9}}}


class ScriptedTransport:
    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, body: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, str]]:
        self.bodies.append(body)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


JUDGE_CONFIG = {
    "kind": "typesafe_jev",
    "prompt": JUDGE_PROMPT,
    "model": "jev-1.13.0",
    "endpoint": "https://api.typesafe.ai/v1/systemone",
    "apiKeyEnv": "TYPESAFE_API_KEY_FOR_TESTS_ONLY",
    "shortlist": 3,
    "timeoutSeconds": 30,
    "maxAttempts": 3,
}
SHORTLIST = [{"title": "a", "when_to_apply": "when a"}, {"title": "b", "when_to_apply": "when b"}]


def judge_with(*replies: Any) -> tuple[JevJudge, ScriptedTransport]:
    transport = ScriptedTransport(*replies)
    return JevJudge.from_config(JUDGE_CONFIG, transport=transport, sleep=lambda _: None), transport


class JevJudgeTest(unittest.TestCase):
    def test_pick_maps_to_shortlist_index_and_none_to_no_skill(self) -> None:
        judge, transport = judge_with((200, answer("skill_1"), {}), (200, answer("none"), {}))
        self.assertEqual(judge.decide("event", {"task": "t"}, SHORTLIST)["choice_index"], 1)
        self.assertIsNone(judge.decide("event", {"task": "t"}, SHORTLIST)["choice_index"])
        body = transport.bodies[0]
        self.assertEqual(body["model"], "jev-1.13.0")
        self.assertEqual(set(body["questions"]["pick"]["criteria"]), {"skill_0", "skill_1", "none"})
        self.assertEqual(set(body["questions"]), {"pick", "fits_0", "fits_1"})

    def test_transient_failures_retry_then_record_judge_failed(self) -> None:
        judge, _ = judge_with((429, {}, {"retry-after": "0"}), ConnectionError("reset"), (200, answer("skill_0"), {}))
        self.assertEqual(judge.decide("event", {"task": "t"}, SHORTLIST)["choice_index"], 0)
        judge, _ = judge_with((503, {}, {}), (503, {}, {}), (503, {}, {}))
        record = judge.decide("event", {"task": "t"}, SHORTLIST)
        self.assertEqual(record["status"], "judge_failed")
        self.assertIsNone(record["choice_index"])
        self.assertEqual(len(record["attempts"]), 3)

    def test_configuration_errors_are_raised_not_retried(self) -> None:
        judge, transport = judge_with((401, {"detail": "bad key"}, {}))
        with self.assertRaises(JudgeConfigError):
            judge.decide("event", {"task": "t"}, SHORTLIST)
        self.assertEqual(len(transport.bodies), 1)
        judge, _ = judge_with((200, answer("skill_7"), {}))
        with self.assertRaises(JudgeConfigError):
            judge.decide("event", {"task": "t"}, SHORTLIST)
        with self.assertRaises(JudgeConfigError):
            JevJudge.from_config({**JUDGE_CONFIG, "prompt": "other"})
        with self.assertRaises(JudgeConfigError):
            JevJudge.from_config(JUDGE_CONFIG).decide("event", {"task": "t"}, SHORTLIST)


P2_SELECTION = {"queryConstruction": QUERY_CONSTRUCTION, "judge": JUDGE_CONFIG}


class P2SidecarTest(unittest.TestCase):
    """The legacy frozen-bank fixture with a P2 profile (one skill per phase)."""

    def _p2_config(self, directory: Path) -> dict[str, Any]:
        fixture = legacy.FrozenBankSidecarRetrievalTest()
        fixture.profile = deepcopy(legacy.FrozenBankSidecarRetrievalTest.profile)
        fixture.profile["p2_selection"] = deepcopy(P2_SELECTION)
        fixture.profile["event_selection"]["max_matching_skills"] = 1
        state_path, config = fixture._state_and_config(directory)
        retrieval = config["retrieval"]
        retrieval["p2Selection"] = deepcopy(P2_SELECTION)
        retrieval["taskSelection"]["maxMatchingSkills"] = 1
        retrieval["eventSelection"]["maxMatchingSkills"] = 1
        self.assertEqual(retrieval["profileSha256"], profile_sha256(fixture.profile))
        return config

    def test_judge_pick_is_the_only_injected_skill(self) -> None:
        judge, transport = judge_with((200, answer("skill_0"), {}), (200, answer("none"), {}))
        with tempfile.TemporaryDirectory() as tmp:
            selectors = FrozenBankSelectors.from_config(self._p2_config(Path(tmp)), encoder=legacy.FakeEncoder(), judge=judge)
            prefix = [
                {"role": "user", "content": TB2_USER},
                assistant("pytest", "run the tests"),
                {"role": "tool", "tool_call_id": "call1", "content": "E   AssertionError: test failure"},
            ]
            event = selectors.select_event({"assistant_index": 1, "tool_result_indices": [2]}, prefix)
            task = selectors.select_task({"role": "user", "content": TB2_USER}, [{"role": "user", "content": TB2_USER}])
        self.assertEqual([item["title"] for item in event["skills"]], ["safe event"])
        self.assertEqual(event["query"]["relevance_judge"]["status"], "ok")
        self.assertEqual(event["query"]["query"]["fields"]["recent_action"], "pytest")
        self.assertEqual(transport.bodies[0]["state"]["last_action"], "pytest")
        self.assertEqual(task["skills"], [])
        decisions = {c["title"]: c["selection_decision"] for c in task["query"]["diagnostics"]["candidates"]}
        self.assertEqual(decisions["safe task"], "rejected_by_relevance_judge")
        self.assertEqual(transport.bodies[1]["state"], {"task": problem_statement(TB2_USER)})

    def test_config_must_match_frozen_p2_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self._p2_config(Path(tmp))
            config["retrieval"]["p2Selection"]["judge"] = {**JUDGE_CONFIG, "model": "jev-latest"}
            with self.assertRaises(SidecarRetrievalError):
                FrozenBankSelectors.from_config(config, encoder=legacy.FakeEncoder())
            config = self._p2_config(Path(tmp) / "second")
            del config["retrieval"]["p2Selection"]
            with self.assertRaises(SidecarRetrievalError):
                FrozenBankSelectors.from_config(config, encoder=legacy.FakeEncoder())

    def test_p2_requires_single_skill_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self._p2_config(Path(tmp))
            config["retrieval"]["taskSelection"]["maxMatchingSkills"] = 2
            with self.assertRaises(SidecarRetrievalError):
                FrozenBankSelectors.from_config(config, encoder=legacy.FakeEncoder())


if __name__ == "__main__":
    unittest.main()
