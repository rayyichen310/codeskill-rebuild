from __future__ import annotations

from argparse import Namespace
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codeskill_rebuild.manager import ManagerProfile
from codeskill_rebuild.types import sha256_file
from scripts import run_r015_c_only_harbor_driver as driver
from scripts.legacy import run_r015_thinking_ab as thinking_ab
from tests.test_manager import FakeResponse


class FixedCounter:
    method = "test_exact_message_counter"

    def __init__(self, count: int) -> None:
        self.count = count
        self.options: dict | None = None

    def __call__(self, messages, *, request_options):
        self.options = deepcopy(request_options)
        return self.count


class R015ThinkingAB16KTest(unittest.TestCase):
    def _manifest(self, root: Path) -> dict:
        source_root = root / "sources"
        for task in thinking_ab.TRACE_NAMES:
            path = source_root / "round-1" / task / "official-harbor" / "trajectory-live.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "source": {"canonical_instance_id": task},
                "steps": [{"source_entry_id": "step", "role": "user", "content": []}],
            }), encoding="utf-8")
        output = root / "manifest.json"
        args = Namespace(
            source_root=str(source_root),
            output=str(output),
            base_url="http://example.invalid/v1",
            model="deepseek-ai/DeepSeek-V4-Flash",
            profile=str(thinking_ab.ROOT / "configs" / "r015-thinking-ab-16k.json"),
        )
        thinking_ab.prepare(args)
        manifest = thinking_ab._read_json(output)
        thinking_ab._verify_manifest(output, manifest)
        return manifest

    def test_profile_and_real_manager_request_use_16k_in_both_arms(self):
        with self.assertRaisesRegex(ValueError, "default manager output"):
            ManagerProfile(base_url="http://example.invalid/v1", model="test", max_output_tokens=16384).validate()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._manifest(root)
            self.assertEqual(manifest["manager_profile"]["input_allowance_tokens"], 249520)
            self.assertEqual(manifest["manager_profile"]["summary_segment_max_tokens"], 65536)
            options = []
            for policy in ("keep", "exclude"):
                counter = FixedCounter(249520)
                with patch.object(driver, "ServerMessageTokenCounter", return_value=counter):
                    context, executor = thinking_ab._context(
                        arm_root=root / policy,
                        manifest=manifest,
                        policy=policy,
                        task_id="build-pmars",
                    )
                manager = executor.manager
                self.assertEqual(manager.profile.output_budget_profile, "r015_thinking_ab_16k")
                self.assertEqual(manager.profile.max_output_tokens, 16384)
                self.assertEqual(context["driver_config"]["historical_thinking_policy"], policy)
                response = {
                    "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 249520, "completion_tokens": 1},
                }
                with patch("codeskill_rebuild.manager.urlopen", return_value=FakeResponse(response)):
                    manager.call_json(
                        purpose=f"16k-wire-{policy}",
                        messages=[{"role": "user", "content": "fixture"}],
                    )
                request_path = manager.run_dir / "model_calls" / "call-0001" / "request.json"
                request = thinking_ab._read_json(request_path)
                self.assertEqual(request["preflight"]["allowed_estimated_input_tokens"], 249520)
                self.assertEqual(request["request"]["max_tokens"], 16384)
                self.assertEqual(request["request"]["reasoning_effort"], "max")
                self.assertEqual(counter.options["max_tokens"], 16384)
                options.append({key: value for key, value in request["request"].items() if key != "messages"})
            self.assertEqual(options[0], options[1])

    def test_16k_summary_keeps_65536_segment_cap(self):
        trace = {
            "source": {"instance_id": "task", "canonical_instance_id": "task"},
            "instruction": "Repair task",
            "text_manager_eligible": True,
            "outcome": {"reward": 0},
            "steps": [
                {"source_entry_id": "action", "role": "assistant", "content": [
                    {"type": "tool_call", "tool_call_id": "one", "name": "exec", "arguments": {"cmd": "check"}}
                ]},
                {"source_entry_id": "result", "role": "toolResult", "content": [],
                 "tool_result": {"tool_call_id": "one"}},
            ],
        }

        def counter(messages, *, request_options):
            payload = json.loads(messages[-1]["content"])
            if "segment_steps" in payload:
                return 100
            return 100 if all(
                item.get("trajectory_input_mode") == "evidence_compacted"
                for item in payload["trajectories"]
            ) else 249521

        def summarize(executor, **kwargs):
            steps = json.loads(kwargs["messages"][-1]["content"])["segment_steps"]
            return {
                "call_id": "summary-1",
                "json": {
                    "summary": "The check completed.",
                    "covered_step_ids": [step["source_entry_id"] for step in steps],
                    "verbatim_evidence_step_ids": ["result"],
                },
            }, {"path": "fixture-journal", "sha256": "fixture", "response": {}}, None

        executor = SimpleNamespace(manager=SimpleNamespace(
            profile=SimpleNamespace(
                model="controlled", temperature=0, max_output_tokens=16384,
                manager_context_tokens=270000, safety_tokens=4096, reasoning_effort="max",
            ),
            exact_token_counter=counter,
        ))
        builder = lambda values: [{"role": "user", "content": json.dumps({"trajectories": values})}]
        with TemporaryDirectory() as tmp, patch.object(driver, "_manager_call", side_effect=summarize), patch.object(
            driver, "_finish_manager_journal", side_effect=lambda executor, journal, **kwargs: journal,
        ):
            driver._prepare_trajectory_messages(
                context={"artifact_root": tmp, "trial_id": "r1:task"},
                executor=executor, phase="task", traces=[trace], builder=builder,
            )
            record = thinking_ab._read_json(Path(tmp) / "manager-context" / "task.json")
        self.assertEqual(record["allowance_tokens"], 249520)
        self.assertEqual(record["summary_segment_allowance_tokens"], 65536)
        self.assertEqual(record["trajectory_input_mode"], "evidence_compacted")

    def test_paired_merge_waits_for_validated_predecessors_in_both_arms(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._manifest(root)
            manifest_path = root / "manifest.json"
            manifest_sha = sha256_file(manifest_path)
            arm_paths = {}
            for policy in ("keep", "exclude"):
                path = root / policy / "arm-result.json"
                path.parent.mkdir()
                candidates = {"fix-git": [{"candidate_id": "fixed"}], "git-leak-recovery": [{"candidate_id": "leak"}]}
                if policy == "exclude":
                    candidates["git-leak-recovery"] = []
                path.write_text(json.dumps({
                    "status": "complete", "policy": policy,
                    "manifest_sha256": manifest_sha,
                    "results": {task: {"task_candidate": {"candidates": value}} for task, value in candidates.items()},
                }), encoding="utf-8")
                arm_paths[policy] = path
            output = root / "paired-merge.json"
            with patch.object(thinking_ab, "_run_fixed_merge") as merge:
                thinking_ab.run_pair_merge(Namespace(
                    manifest=str(manifest_path),
                    keep_run=str(arm_paths["keep"]),
                    exclude_run=str(arm_paths["exclude"]),
                    output=str(output),
                ))
            merge.assert_not_called()
            result = thinking_ab._read_json(output)
            self.assertEqual(result["arms"]["keep"]["status"], "not_run")
            self.assertEqual(result["arms"]["exclude"]["status"], "not_run")
            self.assertEqual(result["missing_predecessors_by_arm"]["exclude"], ["git-leak-recovery"])


if __name__ == "__main__":
    unittest.main()
