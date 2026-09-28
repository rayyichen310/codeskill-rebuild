from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from codeskill_rebuild.arm_banks import exact_skill_schema
from codeskill_rebuild.bank import BankError, SkillBank, validate_skill_candidate
from codeskill_rebuild.code_examples import (
    CodeExampleError,
    check_code_syntax,
    code_example_id,
    combine_materialized_code_examples,
    validate_materialized_code_examples,
)
from codeskill_rebuild.pipeline import (
    event_extraction_with_evidence_messages,
    maintenance_from_skills_messages as maintenance_messages,
    task_candidate_merge_messages,
    validate_event_extraction_with_evidence,
    validate_maintenance_from_skills as validate_maintenance,
    validate_task_candidate_with_evidence,
    validate_task_extraction_with_evidence,
)
from codeskill_rebuild.r012_execution import evolution_messages, validate_evolution_output
from codeskill_rebuild.runtime import build_initial_messages, inject_event_message, render_skill
from codeskill_rebuild.types import canonical_json, sha256_text


S01_ACTION_ID = "4c98039a-3330-4935-a7a4-b9930f41db26"
S01_RESULT_ID = "06e898ae-e7d0-4747-9140-3eb22f4affc6"
S01_CALL_ID = "call_2169a863d058401e94db64ab"
S01_COMMAND = """cd /app/pmars-source && python3 -c "
import lzma, tarfile
with tarfile.open('pmars_0.9.4.orig.tar.xz', 'r:xz') as t:
    t.extractall()
print('Extracted orig')
with tarfile.open('pmars_0.9.4-1.debian.tar.xz', 'r:xz') as t:
    t.extractall()
print('Extracted debian')
" && ls -la && echo "---" && ls -la pmars-0.9.4/"""
S01_RESULT = "Extracted orig\nExtracted debian\n"


def trace_with_command(
    command: str = S01_COMMAND,
    *,
    source_id: str = "build-pmars",
    action_id: str = S01_ACTION_ID,
    result_id: str = S01_RESULT_ID,
    call_id: str = S01_CALL_ID,
    result_text: str = S01_RESULT,
) -> dict:
    return {
        "source": {"canonical_instance_id": source_id},
        "steps": [
            {
                "source_entry_id": action_id,
                "role": "assistant",
                "content": [{"type": "tool_call", "tool_call_id": call_id, "tool_name": "exec", "arguments": {"command": command}}],
                "assistant": {
                    "tool_calls": [
                        {"type": "tool_call", "tool_call_id": call_id, "tool_name": "exec", "arguments": {"command": command}}
                    ]
                },
            },
            {
                "source_entry_id": result_id,
                "role": "toolResult",
                "content": [{"type": "text", "text": result_text}],
                "tool_result": {
                    "tool_call_id": call_id,
                    "tool_name": "exec",
                    "is_error": False,
                    "details": {"exitCode": 0, "aggregated": result_text},
                },
            },
        ],
        "historical_compaction": {
            "control_event_ids": [],
            "raw_message_step_ids": [action_id, result_id],
        },
        "outcome": {"official_reward": "0", "result_present": True},
    }


def candidate_with_example(trace: dict, example: dict) -> dict:
    source_id = trace["source"]["canonical_instance_id"]
    action = next(step for step in trace["steps"] if step["role"] == "assistant")
    call_id = action["assistant"]["tool_calls"][0]["tool_call_id"]
    result = next(
        step
        for step in trace["steps"]
        if step["role"] == "toolResult" and step.get("tool_result", {}).get("tool_call_id") == call_id
    )
    return {
        "action": "generate",
        "skill": {
            "title": "Extract a compressed archive with Python",
            "granularity": "general",
            "when_to_apply": "When an archive needs a Python-based extraction fallback.",
            "rules": ["Use a structured extraction block and inspect the observed command result."],
            "code_examples": [example],
        },
        "candidate_context": {
            "task_goal": "Extract source archives",
            "hard_constraints": [],
            "environment_assumptions": ["Python is available"],
            "observed_results": ["The source operation exited zero"],
            "whole_task_outcome": "failed as supplied",
            "known_limitations": ["A local extraction result is not the whole-task result"],
        },
        "evidence": {
            "rule_evidence": [
                {
                    "rule_index": 0,
                    "sources": [
                        {
                            "canonical_instance_id": source_id,
                            "step_ids": [action["source_entry_id"], result["source_entry_id"]],
                        }
                    ],
                }
            ]
        },
    }


def python_example(trace: dict) -> dict:
    action = next(step for step in trace["steps"] if step["source_entry_id"] == S01_ACTION_ID)
    result = next(step for step in trace["steps"] if step["source_entry_id"] == S01_RESULT_ID)
    return {
        "language": "python",
        "purpose": "Extract one archive while keeping the archive path adjustable.",
        "source": {
            "canonical_instance_id": trace["source"]["canonical_instance_id"],
            "action_step_id": action["source_entry_id"],
            "tool_call_id": action["assistant"]["tool_calls"][0]["tool_call_id"],
            "result_step_id": result["source_entry_id"],
        },
        "generated_code": """from pathlib import Path
import tarfile

archive_path = Path("archive.tar.xz")
with tarfile.open(archive_path, "r:xz") as archive:
    archive.extractall(filter="data")""",
        "adaptations": [
            {
                "kind": "parameterized",
                "source_fragment": "pmars_0.9.4.orig.tar.xz",
                "generated_fragment": "archive_path",
                "rationale": "Expose the archive location for the solver to set from the current task.",
                "applicability": "Only when the archive path is not itself a fixed acceptance requirement.",
            },
            {
                "kind": "other_rewrite",
                "source_fragment": "t.extractall()",
                "generated_fragment": "archive.extractall(filter=\"data\")",
                "rationale": "Use the safer standard data filter instead of unrestricted extraction.",
                "applicability": "When the supported Python version provides the data extraction filter.",
            }
        ],
        "prerequisites": ["The selected archive is a trusted tar.xz file."],
        "known_limitations": ["Choose an extraction filter appropriate for the Python version and trust boundary."],
        "unknowns": ["The destination directory depends on the current task."],
    }


class CodeExampleTest(unittest.TestCase):
    def test_s01_raw_multiline_operation_is_programmatically_bound_and_rendered_separately(self) -> None:
        trace = trace_with_command()
        checked = validate_task_candidate_with_evidence(
            candidate_with_example(trace, python_example(trace)),
            trace,
            benchmark="terminal-bench",
        )
        example = checked["skill"]["code_examples"][0]
        self.assertEqual(example["source_operation"]["arguments"]["command"], S01_COMMAND)
        self.assertIn("\nwith tarfile.open", example["source_operation"]["arguments"]["command"])
        self.assertEqual(example["source_observation"]["content"][0]["text"], S01_RESULT)
        self.assertEqual(example["source_binding"]["tool_call_id"], S01_CALL_ID)
        self.assertEqual(example["source_operation"]["origin"], "programmatic_source_retrieval")
        self.assertEqual(example["generated_example"]["origin"], "manager_generated_adaptation")
        self.assertEqual(example["deterministic_checks"]["syntax"]["status"], "valid")
        self.assertEqual(example["evidence_boundary"]["local_operation_status"], "observed_exit_zero")
        self.assertEqual(example["evidence_boundary"]["whole_task_outcome"]["official_reward"], "0")

        bank = SkillBank.empty("terminal-bench")
        operation = bank.apply(
            operation_id="s01-code-example",
            decision="add",
            candidate=checked["skill"],
            source_instance_ids=["build-pmars"],
            evidence={"fixture_kind": "historical_s01_shape"},
        )
        active = next(skill for skill in bank.skills if skill.get("skill_id") == operation["result_skill_id"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bank.json"
            bank.save(path)
            loaded = SkillBank.load(path)
            reloaded = next(skill for skill in loaded.skills if skill.get("status") == "active")
        self.assertEqual(reloaded["code_examples"], active["code_examples"])
        self.assertEqual(exact_skill_schema(reloaded)["code_examples"], active["code_examples"])
        rendered = render_skill(reloaded)
        self.assertIn("```python", rendered)
        self.assertIn("archive_path = Path", rendered)
        self.assertIn("When to use:", rendered)
        self.assertIn("Prerequisites:", rendered)
        self.assertNotIn(S01_COMMAND, rendered)
        for internal_text in (
            "source_fragment",
            "programmatic_source_retrieval",
            "Evidence boundary",
            "observed_exit_zero",
            S01_ACTION_ID,
            S01_CALL_ID,
            example["source_operation"]["sha256"],
        ):
            self.assertNotIn(internal_text, rendered)

        initial_messages, initial_record = build_initial_messages("Extract it", [reloaded])
        self.assertIn(rendered, initial_record["block"])
        self.assertIn(rendered, initial_messages[0]["content"])
        self.assertNotIn(S01_CALL_ID, initial_messages[0]["content"])
        event_skill = {**deepcopy(reloaded), "granularity": "event"}
        event_messages, event_record = inject_event_message(
            [
                {"role": "assistant", "tool_calls": [{"id": "observed", "type": "function"}]},
                {"role": "tool", "tool_call_id": "observed", "content": "archive found"},
            ],
            event_skill,
            set(),
        )
        self.assertTrue(event_record["injected"])
        self.assertIn("archive_path = Path", event_messages[-1]["content"])
        self.assertNotIn("source_fragment", event_messages[-1]["content"])

        manager_trace = deepcopy(trace)
        manager_trace.update({"instruction": "Extract the source.", "text_manager_eligible": True})
        manager_trace["source"]["instance_id"] = "build-pmars"
        peer_trace = trace_with_command(
            "python3 -m tarfile -e source.tar.xz output",
            source_id="archive-peer",
            action_id="peer-action",
            result_id="peer-result",
            call_id="peer-call",
            result_text="extracted\n",
        )
        peer_trace.update({"instruction": "Extract another source.", "text_manager_eligible": True})
        peer_trace["source"]["instance_id"] = "archive-peer"
        merge_payload = task_candidate_merge_messages(
            [
                {"skill": checked["skill"], "candidate_id": "candidate-a"},
                {
                    "skill": {
                        "title": "Peer extraction",
                        "granularity": "task",
                        "when_to_apply": "When another archive needs extraction.",
                        "rules": ["Extract and inspect the result."],
                        "benchmark": "terminal-bench",
                    },
                    "candidate_id": "candidate-b",
                },
            ],
            [manager_trace, peer_trace],
            paper_prompt="merge",
        )[1]["content"]
        self.assertNotIn(S01_COMMAND, merge_payload)
        self.assertNotIn(S01_RESULT, merge_payload)
        self.assertIn("archive_path = Path", merge_payload)
        self.assertIn("source_evidence_hashes", merge_payload)

        tampered = deepcopy(reloaded)
        tampered["code_examples"][0]["source_operation"]["arguments"]["command"] += "\necho tampered"
        with self.assertRaisesRegex(BankError, "source operation hash"):
            validate_skill_candidate(tampered)

    def test_s01_bad_single_line_rewrite_fails_python_syntax(self) -> None:
        bad = "import lzma, tarfile; with tarfile.open('archive.tar.xz', 'r:xz') as t: t.extractall()"
        checked = check_code_syntax("python", bad)
        self.assertEqual(checked["status"], "invalid")
        self.assertEqual(checked["checker"], "python_ast")
        self.assertEqual(checked["error"]["lineno"], 1)
        self.assertEqual(check_code_syntax("javascript", "console.log('x')")["status"], "unsupported")

    def test_event_and_cross_source_task_paths_materialize_the_same_source_identity(self) -> None:
        trace = trace_with_command()
        trace["steps"] = [
            {"source_entry_id": "initial-user", "role": "user", "content": [{"type": "text", "text": "Extract the source."}]},
            {
                "source_entry_id": "s01-trigger",
                "role": "toolResult",
                "content": [{"type": "text", "text": "The package contains two tar.xz archives."}],
                "tool_result": {"tool_call_id": "probe-call", "tool_name": "exec", "is_error": False, "details": {"exitCode": 0}},
            },
            *trace["steps"],
        ]
        trace["historical_compaction"]["raw_message_step_ids"] = [
            "initial-user",
            "s01-trigger",
            S01_ACTION_ID,
            S01_RESULT_ID,
        ]
        event_value = {
            "action": "generate",
            "skill": {
                "title": "Respond to a discovered source archive",
                "granularity": "event-driven",
                "when_to_apply": "When source archives are observed during a build.",
                "rules": ["Extract the archive with a structured command and inspect its result."],
                "code_examples": [python_example(trace)],
            },
            "evidence": {
                "trigger_step_ids": ["s01-trigger"],
                "response_step_ids": [S01_ACTION_ID],
                "outcome_step_ids": [S01_RESULT_ID],
                "rule_evidence": [{"rule_index": 0, "step_ids": [S01_ACTION_ID, S01_RESULT_ID]}],
            },
        }
        event = validate_event_extraction_with_evidence(event_value, trace, benchmark="terminal-bench")
        self.assertEqual(event["skill"]["code_examples"][0]["source_binding"]["tool_call_id"], S01_CALL_ID)

        peer = trace_with_command(
            "python3 -m tarfile -e source.tar.xz output",
            source_id="archive-peer",
            action_id="peer-action",
            result_id="peer-result",
            call_id="peer-call",
            result_text="extracted\n",
        )
        formal_value = candidate_with_example(trace, python_example(trace))
        formal_value.pop("candidate_context")
        formal_value["evidence"]["rule_evidence"][0]["sources"].append(
            {"canonical_instance_id": "archive-peer", "step_ids": ["peer-action", "peer-result"]}
        )
        formal = validate_task_extraction_with_evidence(
            formal_value,
            [trace, peer],
            benchmark="terminal-bench",
        )
        self.assertEqual(formal["skill"]["code_examples"][0]["source_binding"]["action_step_id"], S01_ACTION_ID)

    def test_unknown_mismatched_or_uncited_source_reference_is_rejected(self) -> None:
        trace = trace_with_command()
        unknown = python_example(trace)
        unknown["source"]["result_step_id"] = "unknown-result"
        with self.assertRaisesRegex(ValueError, "unknown source IDs"):
            validate_task_candidate_with_evidence(candidate_with_example(trace, unknown), trace, benchmark="terminal-bench")

        mismatched = python_example(trace)
        mismatched["source"]["tool_call_id"] = "other-call"
        with self.assertRaisesRegex(ValueError, "exactly one call"):
            validate_task_candidate_with_evidence(candidate_with_example(trace, mismatched), trace, benchmark="terminal-bench")

        uncited = candidate_with_example(trace, python_example(trace))
        uncited["evidence"]["rule_evidence"][0]["sources"][0]["step_ids"] = [S01_ACTION_ID]
        with self.assertRaisesRegex(ValueError, "action followed by an observed tool result"):
            validate_task_candidate_with_evidence(uncited, trace, benchmark="terminal-bench")

    def test_parameterized_value_and_fixed_requirement_remain_distinct(self) -> None:
        command = """set -eu
input_path=input.txt
mkdir -p /app/required-output
cp "$input_path" /app/required-output/result.txt"""
        trace = trace_with_command(command, source_id="fixed-output", action_id="fixed-action", result_id="fixed-result", call_id="fixed-call", result_text="copied\n")
        action, result = trace["steps"]
        example = {
            "language": "bash",
            "purpose": "Copy a caller-selected input to a task-required output path.",
            "source": {
                "canonical_instance_id": "fixed-output",
                "action_step_id": action["source_entry_id"],
                "tool_call_id": "fixed-call",
                "result_step_id": result["source_entry_id"],
            },
            "generated_code": """set -eu
input_path=${1:?supply input path}
mkdir -p /app/required-output
cp "$input_path" /app/required-output/result.txt""",
            "adaptations": [
                {
                    "kind": "parameterized",
                    "source_fragment": "input_path=input.txt",
                    "generated_fragment": "input_path=${1:?supply input path}",
                    "rationale": "Let the solver select the current task input.",
                    "applicability": "When the input filename varies across tasks.",
                },
                {
                    "kind": "preserved_requirement",
                    "source_fragment": "/app/required-output",
                    "generated_fragment": "/app/required-output",
                    "rationale": "Keep the required acceptance path unchanged.",
                    "applicability": "When the task explicitly requires this output path.",
                },
            ],
            "prerequisites": ["The input exists."],
            "known_limitations": ["The fixed destination may overwrite an existing result."],
            "unknowns": [],
        }
        checked = validate_task_candidate_with_evidence(candidate_with_example(trace, example), trace, benchmark="terminal-bench")
        adaptations = checked["skill"]["code_examples"][0]["generated_example"]["adaptations"]
        self.assertEqual([item["kind"] for item in adaptations], ["parameterized", "preserved_requirement"])
        self.assertEqual(adaptations[1]["source_fragment"], adaptations[1]["generated_fragment"])

    def test_generated_archive_example_extracts_expected_fixture_content(self) -> None:
        code = python_example(trace_with_command())["generated_code"]
        self.assertEqual(check_code_syntax("python", code)["status"], "valid")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = root / "payload"
            payload.mkdir()
            (payload / "expected.txt").write_text("archive fixture\n", encoding="utf-8")
            with tarfile.open(root / "archive.tar.xz", "w:xz") as archive:
                archive.add(payload / "expected.txt", arcname="expected.txt")
            (payload / "expected.txt").unlink()
            subprocess.run([sys.executable, "-c", code], cwd=root, check=True, timeout=10)
            self.assertEqual((root / "expected.txt").read_text(encoding="utf-8"), "archive fixture\n")

    def test_stored_rendered_output_example_obeys_each_current_task_location(self) -> None:
        historical_input = "source-task/input.txt"
        historical_output = "source-task/required-output.txt"
        command = (
            "python -c \"from pathlib import Path; "
            f"Path('{historical_output}').write_bytes(Path('{historical_input}').read_bytes())\""
        )
        trace = trace_with_command(
            command,
            source_id="fixed-output",
            action_id="fixed-action",
            result_id="fixed-result",
            call_id="fixed-call",
            result_text="copied to source-task/required-output.txt\n",
        )
        generated_code = """from pathlib import Path
import sys

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_bytes(source.read_bytes())"""
        example = {
            "language": "python",
            "purpose": "Copy the current task input to its required destination.",
            "source": {
                "canonical_instance_id": "fixed-output",
                "action_step_id": "fixed-action",
                "tool_call_id": "fixed-call",
                "result_step_id": "fixed-result",
            },
            "generated_code": generated_code,
            "adaptations": [
                {
                    "kind": "parameterized",
                    "source_fragment": historical_input,
                    "generated_fragment": "Path(sys.argv[1])",
                    "rationale": "Use the input supplied by the current task.",
                    "applicability": "When the current task identifies a different input file.",
                },
                {
                    "kind": "parameterized",
                    "source_fragment": historical_output,
                    "generated_fragment": "Path(sys.argv[2])",
                    "rationale": "Require the current task destination instead of the historical one.",
                    "applicability": "When the current task specifies its required output location.",
                },
            ],
            "prerequisites": ["Pass the current input and required output paths as arguments."],
            "known_limitations": ["The caller must supply the exact current-task destination."],
            "unknowns": [],
        }
        checked = validate_task_candidate_with_evidence(
            candidate_with_example(trace, example),
            trace,
            benchmark="terminal-bench",
        )
        bank = SkillBank.empty("terminal-bench")
        operation = bank.apply(
            operation_id="fixed-output-example",
            decision="add",
            candidate=checked["skill"],
            source_instance_ids=["fixed-output"],
            evidence={"fixture": "safe current-task destinations"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bank_path = root / "bank.json"
            bank.save(bank_path)
            loaded = SkillBank.load(bank_path)
            stored = next(skill for skill in loaded.skills if skill.get("skill_id") == operation["result_skill_id"])
            messages, _ = build_initial_messages("Copy the current input.", [stored])
            solver_payload = messages[0]["content"]
            self.assertNotIn(historical_input, solver_payload)
            self.assertNotIn(historical_output, solver_payload)
            match = re.search(r"```python\n(.*?)\n```", solver_payload, flags=re.DOTALL)
            self.assertIsNotNone(match)
            rendered_code = match.group(1)
            self.assertEqual(check_code_syntax("python", rendered_code)["status"], "valid")
            task_a_root = root / "task-a"
            task_b_root = root / "task-b"
            task_a_root.mkdir()
            task_b_root.mkdir()
            task_a_source = task_a_root / "current-input.txt"
            task_b_source = task_b_root / "current-input.txt"
            task_a_source.write_text("task-a content\n", encoding="utf-8")
            task_b_source.write_text("task-b content\n", encoding="utf-8")
            task_a_destination = task_a_root / "required-a" / "result.txt"
            task_b_destination = task_b_root / "nested" / "required-b.txt"
            task_b_destination.parent.mkdir(parents=True)
            task_b_destination.write_text("task-b destination sentinel\n", encoding="utf-8")

            def file_snapshot() -> dict[Path, bytes]:
                return {
                    path.relative_to(root): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                }

            before_task_a = file_snapshot()
            subprocess.run(
                [sys.executable, "-c", rendered_code, str(task_a_source), str(task_a_destination)],
                cwd=task_a_root,
                check=True,
                timeout=10,
            )
            after_task_a = file_snapshot()
            self.assertEqual(set(after_task_a) - set(before_task_a), {task_a_destination.relative_to(root)})
            for path, content in before_task_a.items():
                self.assertEqual(after_task_a[path], content)
            self.assertEqual(task_a_destination.read_text(encoding="utf-8"), "task-a content\n")
            self.assertEqual(task_b_destination.read_text(encoding="utf-8"), "task-b destination sentinel\n")
            self.assertFalse((task_a_root / historical_output).resolve().exists())

            task_b_destination.unlink()
            before_task_b = file_snapshot()
            subprocess.run(
                [sys.executable, "-c", rendered_code, str(task_b_source), str(task_b_destination)],
                cwd=task_b_root,
                check=True,
                timeout=10,
            )
            after_task_b = file_snapshot()
            self.assertEqual(set(after_task_b) - set(before_task_b), {task_b_destination.relative_to(root)})
            for path, content in before_task_b.items():
                self.assertEqual(after_task_b[path], content)
            self.assertEqual(task_b_destination.read_text(encoding="utf-8"), "task-b content\n")
            self.assertEqual(task_a_destination.read_text(encoding="utf-8"), "task-a content\n")
            self.assertFalse((task_b_root / historical_output).resolve().exists())

    def test_method_only_skill_has_no_code_or_evidence_noise(self) -> None:
        skill = {
            "skill_id": "method-only",
            "version": 1,
            "title": "Inspect before changing",
            "when_to_apply": "When the cause is still unknown.",
            "rules": ["Inspect the relevant state before editing."],
        }
        rendered = render_skill(skill)
        self.assertNotIn("Code examples", rendered)
        self.assertNotIn("Evidence", rendered)
        self.assertNotIn("source", rendered.lower())

    def test_maintenance_and_evolution_explicitly_retain_revise_remove_or_merge_examples(self) -> None:
        trace = trace_with_command()
        checked = validate_task_candidate_with_evidence(candidate_with_example(trace, python_example(trace)), trace, benchmark="terminal-bench")
        candidate = checked["skill"]
        first = candidate["code_examples"][0]
        first_id = code_example_id(first)

        alternate_raw = python_example(trace)
        alternate_raw["purpose"] = "Extract the current input archive selected by the caller."
        alternate_raw["generated_code"] = alternate_raw["generated_code"].replace('Path("archive.tar.xz")', 'Path("current-input.tar.xz")')
        alternate_raw["adaptations"][0]["generated_fragment"] = "archive_path"
        alternate_checked = validate_task_candidate_with_evidence(
            candidate_with_example(trace, alternate_raw),
            trace,
            benchmark="terminal-bench",
        )["skill"]["code_examples"][0]
        alternate_id = code_example_id(alternate_checked)
        target = {
            **deepcopy(candidate),
            "code_examples": [alternate_checked],
            "skill_id": "target-skill",
            "version": 1,
            "status": "active",
        }
        merged = validate_maintenance(
            {
                "action": "merge",
                "reason": "same capability",
                "merge_target_skill_id": "target-skill",
                "evidence": {"source_skill_ids": ["candidate", "target-skill"],
                             "source_example_ids": [first_id, alternate_id]},
                "skill": {
                    "title": "Merged archive extraction",
                    "granularity": "general",
                    "when_to_apply": "When an archive needs a Python fallback.",
                    "rules": ["Use a structured extraction block and inspect the result."],
                },
                "code_example_changes": [
                    {"action": "retain", "example_id": first_id},
                    {"action": "retain", "example_id": alternate_id},
                ],
            },
            candidate=candidate,
            retrieved_skill_ids={"target-skill"},
            retrieved_skills=[target],
        )
        self.assertEqual(len(merged["skill"]["code_examples"]), 2)
        validate_skill_candidate(merged["skill"])

        supplied = [{"skill": target, "injection_evidence": [{"kind": "fixture"}]}]
        revised_fields = {
            "language": "python",
            "purpose": "Inspect archive members without extracting them.",
            "generated_code": """from pathlib import Path
import tarfile

archive_path = Path("archive.tar.xz")
with tarfile.open(archive_path, "r:xz") as archive:
    print("\\n".join(archive.getnames()))""",
            "adaptations": [
                {
                    "kind": "parameterized",
                    "source_fragment": "pmars_0.9.4.orig.tar.xz",
                    "generated_fragment": "archive_path",
                    "rationale": "Select the current archive.",
                    "applicability": "When the archive path varies by task.",
                },
                {
                    "kind": "other_rewrite",
                    "source_fragment": "t.extractall()",
                    "generated_fragment": "archive.getnames()",
                    "rationale": "Inspect only, matching the revised rule.",
                    "applicability": "When inspection is required and extraction is not allowed.",
                },
            ],
            "prerequisites": ["The archive is readable."],
            "known_limitations": ["Listing members does not validate their contents."],
            "unknowns": [],
        }
        evolved = validate_evolution_output(
            {
                "action": "evolve",
                "target_skill_id": "target-skill",
                "target_skill_version": 1,
                "reason": "clarify the rule",
                "skill": {
                    "title": "Clarified archive extraction",
                    "granularity": "general",
                    "when_to_apply": "When an archive needs a Python fallback.",
                    "rules": ["Inspect archive members without extracting files."],
                },
                "code_example_changes": [
                    {"action": "revise", "example_id": alternate_id, "example": revised_fields}
                ],
            },
            supplied=supplied,
            trajectory_evidence=trace,
        )
        evolved_code = evolved["skill"]["code_examples"][0]
        self.assertIn("archive.getnames()", evolved_code["generated_example"]["code"])
        self.assertNotIn("extractall", evolved_code["generated_example"]["code"])
        self.assertEqual(evolved_code["source_operation"], alternate_checked["source_operation"])
        self.assertEqual(evolved_code["revision_history"][-1]["action"], "revised")
        self.assertNotEqual(code_example_id(evolved_code), alternate_id)

        removed = validate_evolution_output(
            {
                "action": "evolve",
                "target_skill_id": "target-skill",
                "target_skill_version": 1,
                "reason": "the revised method no longer needs executable detail",
                "skill": {
                    "title": "Clarified archive inspection",
                    "granularity": "general",
                    "when_to_apply": "When an archive should only be inspected.",
                    "rules": ["Inspect metadata without running an extraction script."],
                },
                "code_example_changes": [{"action": "remove", "example_id": alternate_id}],
            },
            supplied=supplied,
            trajectory_evidence=trace,
        )
        self.assertNotIn("code_examples", removed["skill"])

        added_raw = python_example(trace)
        added_raw["purpose"] = "Extract a caller-selected archive in a separate reusable variant."
        added_raw["generated_code"] = added_raw["generated_code"].replace(
            'Path("archive.tar.xz")', 'Path("selected-input.tar.xz")'
        )
        added = validate_evolution_output(
            {
                "action": "evolve",
                "target_skill_id": "target-skill",
                "target_skill_version": 1,
                "reason": "add a distinct current-trajectory variant",
                "skill": {
                    "title": "Archive extraction variants",
                    "granularity": "general",
                    "when_to_apply": "When an archive requires a Python extraction path.",
                    "rules": ["Select the current archive path, then extract it safely."],
                },
                "code_example_changes": [
                    {"action": "retain", "example_id": alternate_id},
                    {"action": "add", "example": added_raw},
                ],
            },
            supplied=supplied,
            trajectory_evidence=trace,
        )
        self.assertEqual(len(added["skill"]["code_examples"]), 2)
        validate_skill_candidate(added["skill"])

        duplicate_add = deepcopy(alternate_raw)
        with self.assertRaisesRegex(Exception, "duplicate code-example revision"):
            validate_evolution_output(
                {
                    "action": "evolve",
                    "target_skill_id": "target-skill",
                    "target_skill_version": 1,
                    "reason": "attempt duplicate add",
                    "skill": {
                        "title": "Archive extraction variants",
                        "granularity": "general",
                        "when_to_apply": "When an archive requires extraction.",
                        "rules": ["Extract the selected archive."],
                    },
                    "code_example_changes": [
                        {"action": "retain", "example_id": alternate_id},
                        {"action": "add", "example": duplicate_add},
                    ],
                },
                supplied=supplied,
                trajectory_evidence=trace,
            )

        invalid_add = deepcopy(added_raw)
        invalid_add["source"]["result_step_id"] = "missing-result"
        with self.assertRaisesRegex(Exception, "unknown source IDs"):
            validate_evolution_output(
                {
                    "action": "evolve",
                    "target_skill_id": "target-skill",
                    "target_skill_version": 1,
                    "reason": "invalid added source",
                    "skill": {
                        "title": "Archive extraction variants",
                        "granularity": "general",
                        "when_to_apply": "When an archive requires extraction.",
                        "rules": ["Extract the selected archive."],
                    },
                    "code_example_changes": [
                        {"action": "retain", "example_id": alternate_id},
                        {"action": "add", "example": invalid_add},
                    ],
                },
                supplied=supplied,
                trajectory_evidence=trace,
            )

        with self.assertRaisesRegex(Exception, "explicitly cover"):
            validate_evolution_output(
                {
                    "action": "evolve",
                    "target_skill_id": "target-skill",
                    "target_skill_version": 1,
                    "reason": "change rules without deciding the example",
                    "skill": {
                        "title": "Unsafe stale revision",
                        "granularity": "general",
                        "when_to_apply": "When inspection only is required.",
                        "rules": ["Inspect only."],
                    },
                },
                supplied=supplied,
                trajectory_evidence=trace,
            )

        raw = {
            "action": "merge",
            "reason": "try to rewrite evidence",
            "merge_target_skill_id": "target-skill",
            "evidence": {"source_skill_ids": ["candidate", "target-skill"],
                         "source_example_ids": [code_example_id(item) for item in [*candidate["code_examples"], *target["code_examples"]]]},
            "skill": {
                "title": "Merged archive extraction",
                "granularity": "general",
                "when_to_apply": "When an archive needs a Python fallback.",
                "rules": ["Inspect the result."],
                "code_examples": merged["skill"]["code_examples"],
            },
        }
        with self.assertRaisesRegex(ValueError, "must use code_example_changes"):
            validate_maintenance(
                raw,
                candidate=candidate,
                retrieved_skill_ids={"target-skill"},
                retrieved_skills=[target],
            )

        prompt_payload = maintenance_messages(candidate, [target], paper_prompt="maintain")[1]["content"]
        self.assertNotIn(S01_COMMAND, prompt_payload)
        self.assertNotIn(S01_RESULT, prompt_payload)
        self.assertIn("archive_path = Path", prompt_payload)
        self.assertNotIn("source_evidence_hashes", prompt_payload)
        self.assertNotIn("source_binding", prompt_payload)
        self.assertNotIn("source_fragment", prompt_payload)

        evolution_payload = evolution_messages(
            supplied=supplied,
            trajectory_evidence={"source": {"canonical_instance_id": "new-task"}, "steps": []},
            paper_prompt="evolve",
        )[1]["content"]
        self.assertNotIn(S01_COMMAND, evolution_payload)
        self.assertNotIn(S01_RESULT, evolution_payload)
        self.assertIn(alternate_id, evolution_payload)
        self.assertIn("source_evidence_hashes", evolution_payload)

        event_trace = deepcopy(trace)
        event_trace.update({"instruction": "Inspect the archive.", "text_manager_eligible": True})
        prior_payload = event_extraction_with_evidence_messages(
            event_trace,
            runtime_prompt="event",
            prior_event_ids=["prior"],
            prior_event_candidates=[{"candidate_id": "prior", "skill": target}],
        )[1]["content"]
        self.assertNotIn(S01_COMMAND, prior_payload)
        self.assertNotIn(S01_RESULT, prior_payload)
        self.assertIn("archive_path = Path", prior_payload)
        self.assertIn("source_evidence_hashes", prior_payload)

        compact_input_payload = event_extraction_with_evidence_messages(
            event_trace,
            runtime_prompt="event",
            prior_event_ids=["prior"],
            prior_event_candidates=[
                {
                    "candidate_id": "prior",
                    "content": {
                        "title": "Prior archive response",
                        "when_to_apply": "When an archive appears.",
                        "rules": ["Inspect it."],
                        "raw_output": S01_RESULT,
                    },
                    "raw": {"model_output": S01_COMMAND},
                    "step_references": {"trigger_step_ids": ["s01-trigger"]},
                }
            ],
        )[1]["content"]
        self.assertNotIn(S01_COMMAND, compact_input_payload)
        self.assertNotIn(S01_RESULT, compact_input_payload)
        self.assertIn("Prior archive response", compact_input_payload)

    def test_maintenance_uses_visible_example_ids_for_adaptations(self) -> None:
        trace = trace_with_command()
        applicability_only = "Only when the archive is at /fixed/archive-root."
        example = python_example(trace)
        example["adaptations"][0]["applicability"] = applicability_only
        candidate = validate_task_candidate_with_evidence(
            candidate_with_example(trace, example), trace,
            benchmark="terminal-bench")["skill"]
        source = candidate["code_examples"][0]
        source_id = code_example_id(source)
        target = {key: deepcopy(candidate[key]) for key in (
            "title", "granularity", "when_to_apply", "rules", "benchmark")}
        target.update({"skill_id": "target-skill", "version": 1, "status": "active"})
        base = {
            "action": "merge", "reason": "combine the visible procedures",
            "merge_target_skill_id": "target-skill",
            "evidence": {"source_skill_ids": ["candidate", "target-skill"],
                         "source_example_ids": [source_id]},
            "skill": {"title": "Merged archive procedure", "granularity": "general",
                      "when_to_apply": "When inspecting a compressed archive.",
                      "rules": ["Inspect the archive before extraction."]},
        }

        def decide(changes: list[dict]) -> dict:
            return validate_maintenance(
                {**base, "code_example_changes": changes}, candidate=candidate,
                retrieved_skill_ids={"target-skill"}, retrieved_skills=[target])

        retained = decide([{"action": "retain", "example_id": source_id}])
        self.assertEqual(retained["skill"]["code_examples"], [source])
        removed = decide([{"action": "remove", "example_id": source_id}])
        self.assertNotIn("code_examples", removed["skill"])

        generated = source["generated_example"]
        replacement = {
            "language": "python", "purpose": "List archive members before choosing an extraction path.",
            "generated_code": "import tarfile\nwith tarfile.open('current.tar.xz') as archive:\n    print(archive.getnames())",
            "prerequisites": ["The current archive is readable.", applicability_only],
            "known_limitations": ["Listing does not extract files."], "unknowns": [],
        }
        revised = decide([{"action": "revise", "example_id": source_id,
                           "reason": "limit the example to inspection", "example": replacement}])
        revision = revised["skill"]["code_examples"][0]
        self.assertEqual(revision["schema_version"], 2)
        self.assertEqual(revision["maintenance_adaptation"]["source_example_id"], source_id)
        self.assertFalse(revision["maintenance_adaptation"]["source_execution_verified"])
        self.assertEqual(revision["source_operation"], source["source_operation"])
        self.assertEqual(revision["generated_example"]["adaptations"], [])
        self.assertEqual(revision["deterministic_checks"]["syntax"]["status"], "valid")
        self.assertNotIn(source_id, render_skill({**revised["skill"], "skill_id": "new", "version": 1}))
        self.assertIn(applicability_only,
                      render_skill({**revised["skill"], "skill_id": "new", "version": 1}))

        added = decide([{"action": "retain", "example_id": source_id},
                        {"action": "add", "source_example_id": source_id,
                         "reason": "include an inspection variant", "example": replacement}])
        self.assertEqual(len(added["skill"]["code_examples"]), 2)
        self.assertEqual(added["skill"]["code_examples"][1]["schema_version"], 2)
        self.assertEqual(added["skill"]["code_examples"][1]["revision_history"][-1]["context"]["phase"],
                         "maintenance_add")
        with self.assertRaisesRegex(ValueError, "unknown source example"):
            decide([{"action": "remove", "example_id": source_id},
                    {"action": "add", "source_example_id": "unknown", "reason": "bad", "example": replacement}])
        invalid = {**replacement, "generated_code": "if True:\n    print("}
        with self.assertRaisesRegex(ValueError, "syntax validation"):
            decide([{"action": "revise", "example_id": source_id,
                     "reason": "bad syntax", "example": invalid}])
        bad_skill = deepcopy(base)
        bad_skill["evidence"] = {"source_skill_ids": ["candidate", "unknown"],
                                 "source_example_ids": [source_id]}
        bad_skill["code_example_changes"] = [{"action": "retain", "example_id": source_id}]
        with self.assertRaisesRegex(ValueError, "source_skill_ids"):
            validate_maintenance(bad_skill, candidate=candidate,
                                 retrieved_skill_ids={"target-skill"}, retrieved_skills=[target])

        payload = json.loads(maintenance_messages(candidate, [target], paper_prompt="maintain")[1]["content"])
        self.assertEqual(payload["candidate_skill"]["code_examples"][0]["example_id"], source_id)
        self.assertEqual(payload["candidate_skill"]["code_examples"][0]["generated_code"], generated["code"])
        self.assertIn(applicability_only, payload["candidate_skill"]["code_examples"][0]["applicability"])
        self.assertNotIn("source_binding", json.dumps(payload))
        self.assertNotIn("source_operation", json.dumps(payload))
        self.assertNotIn("source_fragment", json.dumps(payload))
        self.assertNotIn("revision_history", json.dumps(payload))
        self.assertNotIn("provenance", json.dumps(payload))

        second_id = code_example_id(revision)
        second_candidate = revised["skill"]
        second_payload = json.loads(maintenance_messages(
            second_candidate, [target], paper_prompt="maintain")[1]["content"])
        second_example = second_payload["candidate_skill"]["code_examples"][0]
        self.assertEqual(second_example["example_id"], second_id)
        self.assertEqual(second_example["applicability"], [])
        self.assertIn(applicability_only, second_example["prerequisites"])
        self.assertNotIn("source_binding", json.dumps(second_payload))
        self.assertNotIn("source_operation", json.dumps(second_payload))
        self.assertNotIn("source_example_id", json.dumps(second_payload))
        self.assertNotIn("revision_history", json.dumps(second_payload))
        second_decision = {**base,
            "evidence": {"source_skill_ids": ["candidate", "target-skill"],
                         "source_example_ids": [second_id]},
            "code_example_changes": [{"action": "revise", "example_id": second_id,
                                      "reason": "clarify the visible precondition",
                                      "example": {**replacement,
                                                  "purpose": "Inspect members after checking the archive location."}}],
        }
        second = validate_maintenance(second_decision, candidate=second_candidate,
                                      retrieved_skill_ids={"target-skill"}, retrieved_skills=[target])
        second_revision = second["skill"]["code_examples"][0]
        self.assertEqual(second_revision["maintenance_adaptation"]["source_example_id"], second_id)
        self.assertEqual(second_revision["revision_history"][-1]["parent_example_id"], second_id)
        self.assertIn(applicability_only,
                      render_skill({**second["skill"], "skill_id": "next", "version": 2}))

    def test_driver_maintenance_persists_example_decisions_and_references(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch

        from scripts.run_r015_c_only_harbor_driver import _fig9_operation
        from codeskill_rebuild.types import sha256_file, write_json

        trace = trace_with_command()
        applicability_only = "Only when the archive is at /fixed/archive-root."
        source_example = python_example(trace)
        source_example["adaptations"][0]["applicability"] = applicability_only
        candidate = validate_task_candidate_with_evidence(
            candidate_with_example(trace, source_example), trace,
            benchmark="terminal-bench")["skill"]
        candidate["provenance"] = {"source_instance_ids": ["build-pmars"],
                                   "source_instance_ids_raw": ["build-pmars"],
                                   "parent_skill_ids": []}
        original = candidate["code_examples"][0]
        source_id = code_example_id(original)
        replacement = {
            "language": "python", "purpose": "Inspect members of the selected archive.",
            "generated_code": "import tarfile\nwith tarfile.open('current.tar.xz') as archive:\n    print(archive.getnames())",
            "prerequisites": ["The archive is readable.", applicability_only],
            "known_limitations": ["Listing does not extract files."], "unknowns": [],
        }

        class FixedEncoder:
            def index_skill(self, value: dict) -> tuple[list[float], dict]:
                return [1.0, 0.0], {"kind": "controlled", "granularity": value["granularity"]}

        changes_by_case = {
            "retain": [{"action": "retain", "example_id": source_id}],
            "revise": [{"action": "revise", "example_id": source_id,
                        "reason": "prefer inspection", "example": replacement}],
            "remove": [{"action": "remove", "example_id": source_id}],
            "add": [{"action": "retain", "example_id": source_id},
                    {"action": "add", "source_example_id": source_id,
                     "reason": "include an inspection variant", "example": replacement}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for case, changes in changes_by_case.items():
                bank = SkillBank.empty("terminal-bench")
                target = {key: deepcopy(candidate[key]) for key in (
                    "title", "granularity", "when_to_apply", "rules", "benchmark")}
                target_op = bank.apply(operation_id=f"target-{case}", decision="add",
                                       candidate=target, source_instance_ids=["other-task"],
                                       evidence={"fixture": case})
                target_id = target_op["result_skill_id"]
                decision = {
                    "action": "merge", "reason": f"controlled {case} merge",
                    "merge_target_skill_id": target_id,
                    "evidence": {"source_skill_ids": ["candidate", target_id],
                                 "source_example_ids": [source_id]},
                    "skill": {"title": "Merged archive procedure", "granularity": "general",
                              "when_to_apply": "When inspecting an archive.",
                              "rules": ["Inspect before extraction."]},
                    "code_example_changes": changes,
                }
                response_path = root / f"{case}-response.json"
                write_json(response_path, decision)
                response_ref = {"call_id": f"call-{case}", "path": str(response_path),
                                "sha256": sha256_file(response_path)}

                def controlled_call(*args, **kwargs):
                    del args
                    payload = json.loads(kwargs["messages"][1]["content"])
                    self.assertEqual(payload["candidate_skill"]["code_examples"][0]["example_id"], source_id)
                    self.assertNotIn("source_binding", json.dumps(payload))
                    self.assertNotIn(S01_COMMAND, json.dumps(payload))
                    return ({"call_id": f"call-{case}", "json": decision},
                            {"response": response_ref}, None)

                with patch("scripts.run_r015_c_only_harbor_driver._manager_call",
                           side_effect=controlled_call), patch(
                           "scripts.run_r015_c_only_harbor_driver._finish_manager_journal"):
                    operation = _fig9_operation(
                        context={"trial_id": f"trial-{case}", "task_id": "build-pmars"},
                        executor=SimpleNamespace(encoder=FixedEncoder()), bank=bank,
                        candidate=candidate, source_instance_ids=["build-pmars"],
                        operation_id=f"merge-{case}", phase="fig9-extraction-001",
                        purpose_prefix="controlled", extra_evidence={"fixture": case})
                self.assertEqual(operation["evidence"]["decision_references"], decision["evidence"])
                path = root / f"{case}-bank.json"
                bank.save(path)
                loaded = SkillBank.load(path)
                merged_skill = next(skill for skill in loaded.skills
                                    if skill.get("skill_id") == operation["applied_preview"]["result_skill_id"])
                examples = merged_skill.get("code_examples", [])
                self.assertEqual(len(examples), {"retain": 1, "revise": 1,
                                                "remove": 0, "add": 2}[case])
                if case in {"revise", "add"}:
                    self.assertEqual(examples[-1]["maintenance_adaptation"]["source_example_id"], source_id)
                    self.assertFalse(examples[-1]["evidence_boundary"]["maintenance_generated_code_executed"])
                self.assertNotIn(source_id, render_skill(merged_skill))
                if case == "revise":
                    self.assertIn(applicability_only, render_skill(merged_skill))
                    first_revision_id = code_example_id(examples[0])
                    next_candidate = {key: deepcopy(value) for key, value in merged_skill.items()
                                      if key not in {"skill_id", "version", "status", "created_sequence"}}
                    next_decision = {
                        "action": "merge", "reason": "clarify the adapted example",
                        "merge_target_skill_id": merged_skill["skill_id"],
                        "evidence": {"source_skill_ids": ["candidate", merged_skill["skill_id"]],
                                     "source_example_ids": [first_revision_id]},
                        "skill": {"title": "Merged archive procedure", "granularity": "general",
                                  "when_to_apply": "When inspecting an archive.",
                                  "rules": ["Inspect before extraction."]},
                        "code_example_changes": [{"action": "revise", "example_id": first_revision_id,
                                                  "reason": "state the archive precondition explicitly",
                                                  "example": {**replacement,
                                                              "purpose": "Inspect after checking the archive location."}}],
                    }
                    next_response = root / "second-response.json"
                    write_json(next_response, next_decision)
                    next_ref = {"call_id": "call-second", "path": str(next_response),
                                "sha256": sha256_file(next_response)}

                    def second_call(*args, **kwargs):
                        del args
                        payload = json.loads(kwargs["messages"][1]["content"])
                        visible = payload["candidate_skill"]["code_examples"][0]
                        self.assertEqual(visible["example_id"], first_revision_id)
                        self.assertEqual(visible["applicability"], [])
                        self.assertIn(applicability_only, visible["prerequisites"])
                        self.assertNotIn("source_binding", json.dumps(payload))
                        self.assertNotIn("source_example_id", json.dumps(payload))
                        self.assertNotIn("revision_history", json.dumps(payload))
                        return ({"call_id": "call-second", "json": next_decision},
                                {"response": next_ref}, None)

                    with patch("scripts.run_r015_c_only_harbor_driver._manager_call",
                               side_effect=second_call), patch(
                               "scripts.run_r015_c_only_harbor_driver._finish_manager_journal"):
                        next_operation = _fig9_operation(
                            context={"trial_id": "trial-second", "task_id": "build-pmars"},
                            executor=SimpleNamespace(encoder=FixedEncoder()), bank=loaded,
                            candidate=next_candidate, source_instance_ids=["build-pmars"],
                            operation_id="merge-second", phase="fig9-extraction-001",
                            purpose_prefix="controlled")
                    next_skill = next(skill for skill in loaded.skills
                                      if skill.get("skill_id") == next_operation["applied_preview"]["result_skill_id"])
                    next_example = next_skill["code_examples"][0]
                    self.assertEqual(next_example["maintenance_adaptation"]["source_example_id"],
                                     first_revision_id)
                    self.assertEqual(next_example["revision_history"][-1]["parent_example_id"],
                                     first_revision_id)
                    self.assertIn(applicability_only, render_skill(next_skill))

            drop_decision = {"action": "drop", "reason": "already covered",
                             "evidence": {"source_skill_ids": ["candidate"],
                                          "source_example_ids": [source_id]}}
            drop_response = root / "drop-response.json"
            write_json(drop_response, drop_decision)
            drop_ref = {"call_id": "call-drop", "path": str(drop_response),
                        "sha256": sha256_file(drop_response)}
            with patch("scripts.run_r015_c_only_harbor_driver._manager_call",
                       return_value=({"call_id": "call-drop", "json": drop_decision},
                                     {"response": drop_ref}, None)), patch(
                       "scripts.run_r015_c_only_harbor_driver._finish_manager_journal"):
                drop_operation = _fig9_operation(
                    context={"trial_id": "trial-drop", "task_id": "build-pmars"},
                    executor=SimpleNamespace(encoder=FixedEncoder()),
                    bank=SkillBank.empty("terminal-bench"), candidate=candidate,
                    source_instance_ids=["build-pmars"], operation_id="drop-example",
                    phase="fig9-extraction-001", purpose_prefix="controlled")
            self.assertEqual(drop_operation["evidence"]["decision_references"], drop_decision["evidence"])
            self.assertEqual(drop_operation["decision"], "drop")

    def test_metadata_only_revision_merges_to_bank_and_renders_only_selected_constraints(self) -> None:
        trace = trace_with_command()
        checked = validate_task_candidate_with_evidence(
            candidate_with_example(trace, python_example(trace)),
            trace,
            benchmark="terminal-bench",
        )
        bank = SkillBank.empty("terminal-bench")
        initial_operation = bank.apply(
            operation_id="metadata-revision-base",
            decision="add",
            candidate=checked["skill"],
            source_instance_ids=["build-pmars"],
            evidence={"fixture": "metadata-only revision"},
        )
        target = deepcopy(next(skill for skill in bank.skills if skill.get("skill_id") == initial_operation["result_skill_id"]))
        original = target["code_examples"][0]
        original_id = code_example_id(original)
        generated = original["generated_example"]
        revised_fields = {
            "language": original["language"],
            "purpose": original["purpose"],
            "generated_code": generated["code"],
            "adaptations": deepcopy(generated["adaptations"]),
            "prerequisites": ["The archive is trusted and Python supports the data extraction filter."],
            "known_limitations": deepcopy(generated["known_limitations"]),
            "unknowns": deepcopy(generated["unknowns"]),
        }
        retained = validate_evolution_output(
            {
                "action": "evolve",
                "target_skill_id": target["skill_id"],
                "target_skill_version": target["version"],
                "reason": "retain the stored example without citing new trajectory bytes",
                "skill": {
                    "title": target["title"],
                    "granularity": "general",
                    "when_to_apply": target["when_to_apply"],
                    "rules": deepcopy(target["rules"]),
                },
                "code_example_changes": [{"action": "retain", "example_id": original_id}],
            },
            supplied=[{"skill": target, "injection_evidence": [{"kind": "fixture"}]}],
            trajectory_evidence=trace,
            visible_step_ids_by_source={"build-pmars": []},
        )
        self.assertEqual(retained["skill"]["code_examples"], target["code_examples"])
        evolved = validate_evolution_output(
            {
                "action": "evolve",
                "target_skill_id": target["skill_id"],
                "target_skill_version": target["version"],
                "reason": "make the runtime prerequisite explicit",
                "skill": {
                    "title": target["title"],
                    "granularity": "general",
                    "when_to_apply": target["when_to_apply"],
                    "rules": deepcopy(target["rules"]),
                },
                "code_example_changes": [
                    {"action": "revise", "example_id": original_id, "example": revised_fields}
                ],
            },
            supplied=[{"skill": target, "injection_evidence": [{"kind": "fixture"}]}],
            trajectory_evidence=trace,
            visible_step_ids_by_source={"build-pmars": []},
        )
        revised = evolved["skill"]["code_examples"][0]
        revised_id = code_example_id(revised)
        self.assertNotEqual(revised_id, original_id)
        self.assertEqual(revised["generated_example"]["code"], original["generated_example"]["code"])
        self.assertEqual(revised["source_operation"], original["source_operation"])
        self.assertEqual(revised["source_observation"], original["source_observation"])
        self.assertEqual(len(revised["revision_history"]), 2)
        self.assertEqual(revised["revision_history"][-1]["parent_example_id"], original_id)
        self.assertEqual(revised["revision_history"][-1]["context"]["source_skill_version"], target["version"])

        maintenance_payload = maintenance_messages(evolved["skill"], [target], paper_prompt="maintain")[1]["content"]
        self.assertIn(revised_id, maintenance_payload)
        self.assertIn(original_id, maintenance_payload)
        self.assertNotIn("source_binding", maintenance_payload)
        self.assertNotIn("source_evidence_hashes", maintenance_payload)

        merge_value = {
            "action": "merge",
            "reason": "replace the old example contract with its selected revision",
            "merge_target_skill_id": target["skill_id"],
            "evidence": {"source_skill_ids": ["candidate", target["skill_id"]],
                         "source_example_ids": [revised_id, original_id]},
            "skill": {
                "title": target["title"],
                "granularity": "general",
                "when_to_apply": target["when_to_apply"],
                "rules": deepcopy(target["rules"]),
            },
            "code_example_changes": [
                {"action": "retain", "example_id": revised_id},
                {"action": "remove", "example_id": original_id},
            ],
        }
        missing_old_decision = deepcopy(merge_value)
        missing_old_decision["code_example_changes"] = [{"action": "retain", "example_id": revised_id}]
        with self.assertRaisesRegex(ValueError, "omitted existing examples"):
            validate_maintenance(
                missing_old_decision,
                candidate=evolved["skill"],
                retrieved_skill_ids={target["skill_id"]},
                retrieved_skills=[target],
            )
        merged = validate_maintenance(
            merge_value,
            candidate=evolved["skill"],
            retrieved_skill_ids={target["skill_id"]},
            retrieved_skills=[target],
        )
        applied = bank.apply(
            operation_id="metadata-revision-merge",
            decision="merge",
            candidate=merged["skill"],
            source_instance_ids=["build-pmars", "current-task"],
            merge_target_id=target["skill_id"],
            evidence={"fixture": "selected metadata revision"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            bank_path = Path(tmp) / "bank.json"
            bank.save(bank_path)
            loaded = SkillBank.load(bank_path)
        current = next(skill for skill in loaded.skills if skill.get("skill_id") == applied["result_skill_id"])
        historical = next(
            skill
            for skill in loaded.skills
            if skill.get("skill_id") == target["skill_id"] and skill.get("version") == target["version"]
        )
        self.assertEqual(historical["code_examples"], target["code_examples"])
        self.assertEqual(len(current["code_examples"]), 1)
        self.assertEqual(code_example_id(current["code_examples"][0]), revised_id)
        rendered = render_skill(current)
        self.assertIn("The archive is trusted and Python supports the data extraction filter.", rendered)
        self.assertNotIn("The selected archive is a trusted tar.xz file.", rendered)
        self.assertNotIn(S01_COMMAND, rendered)

        conflicting = deepcopy(original)
        conflicting["source_operation"]["arguments"]["command"] += "\n# independently changed raw evidence"
        operation_payload = {key: deepcopy(value) for key, value in conflicting["source_operation"].items() if key != "sha256"}
        conflicting["source_operation"]["sha256"] = sha256_text(canonical_json(operation_payload))
        validate_materialized_code_examples([conflicting])
        self.assertEqual(code_example_id(conflicting), original_id)
        with self.assertRaisesRegex(CodeExampleError, "conflicting records"):
            combine_materialized_code_examples([original], [conflicting])


if __name__ == "__main__":
    unittest.main()
