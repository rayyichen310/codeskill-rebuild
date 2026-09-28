from __future__ import annotations

import json
import unittest
from typing import Any

from codeskill_rebuild.compact_trace import command_sequence, cut, render


def call(call_id: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_call", "tool_call_id": call_id, "tool_name": tool, "partial_arguments": json.dumps(args)}


def result(call_id: str, text: str, exit_code: int | None) -> dict[str, Any]:
    details = {"exitCode": exit_code} if exit_code is not None else {}
    return {"role": "toolResult", "content": [{"type": "text", "text": text}], "tool_result": {"tool_call_id": call_id, "details": details}}


def trajectory(steps: list[dict[str, Any]]) -> dict[str, Any]:
    first = {"role": "user", "content": [{"type": "text", "text": "[Mon 2026-09-28 02:50 UTC] Build the thing."}]}
    return {"outcome": {"official_reward": "1"}, "steps": [first, *steps]}


class CompactTraceTest(unittest.TestCase):
    def test_parallel_calls_are_numbered_and_paired_with_results(self) -> None:
        traj = trajectory([
            {"role": "assistant", "content": [
                {"type": "thinking", "text": "t" * 1000},
                call("a", "exec", {"command": "ls"}),
                call("b", "read", {"path": "/app/x"}),
            ]},
            result("b", "file body", None),
            result("a", "x\ny", 0),
        ])
        text = render(traj)
        self.assertTrue(text.startswith("TASK:\nBuild the thing.\nOFFICIAL RESULT: pass"))
        self.assertIn("THINK: " + "t" * 400 + "\n", text)
        self.assertIn('ACTION[1]: ls\nACTION[2]: read {"path": "/app/x"}', text)
        self.assertIn("RESULT[1] (exit 0):\nx\ny\nRESULT[2] (ok):\nfile body", text)

    def test_long_output_keeps_head_and_tail(self) -> None:
        text = cut("a" * 1000 + "b" * 1000)
        self.assertTrue(text.startswith("a" * 750) and text.endswith("b" * 750))
        self.assertIn("[500 chars omitted]", text)

    def test_command_sequence_hides_write_content_and_trims_long_runs(self) -> None:
        steps: list[dict[str, Any]] = [
            {"role": "assistant", "content": [call("w", "write", {"path": "/app/f.py", "content": "print(1)\n" * 50})]},
            result("w", "ok", None),
        ]
        for i in range(90):
            steps += [{"role": "assistant", "content": [call(f"c{i}", "exec", {"command": f"echo {i}"})]}, result(f"c{i}", "", 0)]
        lines = command_sequence(trajectory(steps)).splitlines()
        self.assertEqual(lines[0], "S1: write /app/f.py (450 chars)  [ok]")
        self.assertEqual(len(lines), 81)
        self.assertEqual(lines[40], "…[11 commands omitted]…")
        self.assertEqual(lines[-1], "S91: echo 89  [exit 0]")


if __name__ == "__main__":
    unittest.main()
