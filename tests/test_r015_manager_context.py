from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codeskill_rebuild.manager import ContextBlocked, ManagerClient, ManagerProfile, TokenizationError
from codeskill_rebuild.manager_projection import HISTORICAL_THINKING_POLICY_VERSION
from codeskill_rebuild.pipeline import task_extraction_messages
from codeskill_rebuild.r012_execution import R012EvolutionMaintenanceExecutor
from scripts import run_r015_c_only_harbor_driver as driver
from tests.test_manager import FakeResponse


class R015ManagerContextTest(unittest.TestCase):
    def _summary_live_parts(self, root, *, policy="keep", max_output_tokens=100):
        trace = {
            "source": {"instance_id": "task", "canonical_instance_id": "task"},
            "instruction": "Repair task",
            "text_manager_eligible": True,
            "outcome": {"reward": 0},
            "steps": [
                {"source_entry_id": "a", "role": "assistant", "content": [
                    {"type": "tool_call", "tool_call_id": "one", "name": "exec", "arguments": {"cmd": "check"}}
                ]},
                {"source_entry_id": "r", "role": "toolResult", "content": [],
                 "tool_result": {"tool_call_id": "one"}},
            ],
        }

        def counter(messages, *, request_options):
            payload = json.loads(messages[-1]["content"])
            if "segment_steps" in payload:
                return 100
            return 100 if all(
                value.get("trajectory_input_mode") == "evidence_compacted"
                for value in payload["trajectories"]
            ) else 2000

        profile = json.loads(
            (Path(driver.ROOT) / "configs/r015-c-only-coding.json").read_text()
        )["retrieval_profile"]
        manager = ManagerClient(
            ManagerProfile(
                base_url="http://example.invalid/v1",
                model="controlled",
                max_output_tokens=max_output_tokens,
                manager_context_tokens=1200,
                safety_tokens=100,
                reasoning_effort="max",
            ),
            Path(root) / "manager",
            {},
            Path(root) / "ledger.json",
            counter,
        )
        executor = R012EvolutionMaintenanceExecutor(
            manager=manager,
            encoder=None,
            journal_root=Path(root) / "journals",
            instance_id="task",
            profile=profile,
            selections={},
        )
        context = {
            "artifact_root": str(root),
            "trial_id": "r1:C:task",
            "task_id": "task",
            "driver_config": {"historical_thinking_policy": policy},
        }
        builder = lambda values: [
            {"role": "user", "content": json.dumps({"trajectories": values})}
        ]
        summary = {
            "summary": "The check completed.",
            "covered_step_ids": ["a", "r"],
            "verbatim_evidence_step_ids": ["r"],
        }
        return trace, executor, context, builder, summary

    @staticmethod
    def _summary_http_response(summary):
        return FakeResponse({
            "choices": [{"message": {"content": json.dumps(summary)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        })

    def _seed_legacy_completed_summary(
        self, *, trace, executor, context, summary, corrupt_source_key=False
    ):
        policy = {
            "policy": "keep",
            "policy_version": HISTORICAL_THINKING_POLICY_VERSION,
        }
        manager_trace = driver.project_historical_thinking(trace, policy="keep")["manager_trace"]
        source_identity = {
            "immutable_trace_sha256": driver._hash_json(trace),
            "manager_view_sha256": driver._hash_json(manager_trace),
            "historical_thinking_policy": "keep",
            "historical_thinking_policy_version": HISTORICAL_THINKING_POLICY_VERSION,
        }
        source_key = driver._hash_json(source_identity)
        prompt = driver._prompt_path("custom/m2_evidence_compaction.md").read_text(encoding="utf-8")
        prompt += (
            "\n\nD03 runtime: apply this V02 evidence contract only after full input exceeds "
            "the configured manager budget. The normal context, output reserve, and reasoning "
            "settings remain in force."
        )
        steps = manager_trace["steps"]
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps({
                "task_context": trace["instruction"],
                "source": trace["source"],
                "segment_steps": steps,
                "final_segment": True,
                "required_covered_step_ids": [str(step["source_entry_id"]) for step in steps],
                "coverage_instruction": (
                    "Summarize all supplied steps, cite the essential original evidence, and "
                    "retain observed final results. The driver records complete segment coverage "
                    "separately."
                ),
            }, ensure_ascii=False)},
        ]
        segment_key = driver._hash_json({
            "source": source_key,
            "historical_thinking_policy": "keep",
            "historical_thinking_policy_version": HISTORICAL_THINKING_POLICY_VERSION,
            "step_ids": [str(step["source_entry_id"]) for step in steps],
            "messages": messages,
        })
        metadata = {
            "condition": "C-only",
            "trajectory_input_mode": "evidence_compacted",
            "historical_thinking_policy": policy,
            "source_trace_sha256": "corrupt-source-key" if corrupt_source_key else source_key,
            "segment_step_ids": [str(step["source_entry_id"]) for step in steps],
            "final_segment": True,
            "segment_allowance_tokens": 800,
        }
        call, journal, invalid = driver._manager_call(
            executor,
            trial_id=context["trial_id"],
            phase=f"context-summary-{segment_key[:20]}",
            purpose=f"r015_c_only_evidence_summary:{context['trial_id']}:{segment_key}",
            messages=messages,
            metadata=metadata,
        )
        self.assertIsNone(invalid)
        self.assertIsNotNone(call)
        finished = driver._finish_manager_journal(
            executor,
            journal,
            status="context_summary_validated",
            value={"call_id": call["call_id"], "summary": summary, "model_output": call["json"]},
        )
        return Path(finished["path"]), executor.manager.run_dir / "model_calls/call-0001/request.json"

    def test_tokenizer_failure_is_preserved_before_any_manager_call(self):
        trace = {
            "source": {"instance_id": "task", "canonical_instance_id": "task"},
            "instruction": "Repair task",
            "text_manager_eligible": True,
            "outcome": {"reward": 1},
            "steps": [{"source_entry_id": "u", "role": "user", "content": [{"type": "text", "text": "repair"}]}],
        }

        class FailingCounter:
            last_exchange = {"endpoint": "http://unresolvable/v1/tokenize", "http_status": None, "raw_response": "dns failure"}

            def __call__(self, messages, *, request_options):
                raise TokenizationError("message-aware /tokenize failed; raw exchange is preserved")

        executor = SimpleNamespace(manager=SimpleNamespace(
            profile=SimpleNamespace(model="controlled", temperature=0, max_output_tokens=100,
                                    manager_context_tokens=1200, safety_tokens=100, reasoning_effort="max"),
            exact_token_counter=FailingCounter(),
        ))
        with TemporaryDirectory() as tmp:
            context = {"artifact_root": tmp, "trial_id": "r1:task"}
            with self.assertRaisesRegex(ContextBlocked, "exact tokenizer failed"):
                driver._prepare_trajectory_messages(
                    context=context,
                    executor=executor,
                    phase="task",
                    traces=[trace],
                    builder=lambda values: [{"role": "user", "content": json.dumps(values)}],
                )
            record = json.loads((Path(tmp) / "manager-context" / "task.json").read_text())
            self.assertEqual(record["state"], "tokenizer_unavailable")
            self.assertEqual(record["failed_count_stage"], "full_after_historical_policy")
            self.assertEqual(record["tokenizer_exchange"]["raw_response"], "dns failure")

    def test_existing_manager_context_is_not_overwritten_by_retry_tokenizer_failure(self):
        trace = {
            "source": {"instance_id": "task", "canonical_instance_id": "task"},
            "instruction": "Repair task",
            "text_manager_eligible": True,
            "outcome": {"reward": 1},
            "steps": [{"source_entry_id": "u", "role": "user", "content": []}],
        }

        class FailingCounter:
            def __init__(self):
                self.calls = 0

            def __call__(self, messages, *, request_options):
                self.calls += 1
                raise TokenizationError("retry must not reach tokenizer")

        manager = SimpleNamespace(
            profile=SimpleNamespace(model="controlled", temperature=0, max_output_tokens=100,
                                    manager_context_tokens=1200, safety_tokens=100, reasoning_effort="max"),
            exact_token_counter=lambda messages, *, request_options: 100,
        )
        executor = SimpleNamespace(manager=manager)
        builder = lambda values: [{"role": "user", "content": json.dumps(values)}]
        with TemporaryDirectory() as tmp:
            context = {"artifact_root": tmp, "trial_id": "r1:task"}
            _, context_ref, _ = driver._prepare_trajectory_messages(
                context=context, executor=executor, phase="task", traces=[trace], builder=builder,
            )
            original_bytes = Path(context_ref["path"]).read_bytes()
            failing = FailingCounter()
            manager.exact_token_counter = failing
            with self.assertRaisesRegex(driver.COnlyHarborDriverError, "refusing replay"):
                driver._prepare_trajectory_messages(
                    context=context, executor=executor, phase="task", traces=[trace], builder=builder,
                )
            self.assertEqual(failing.calls, 0)
            self.assertEqual(Path(context_ref["path"]).read_bytes(), original_bytes)
            self.assertEqual(json.loads(original_bytes)["state"], "within_budget")

    def test_exclude_policy_is_applied_before_counting_and_binds_context_identity(self):
        trace = {
            "source": {"instance_id": "task", "canonical_instance_id": "task"},
            "instruction": "Repair task",
            "text_manager_eligible": True,
            "outcome": {"reward": 1},
            "steps": [
                {"source_entry_id": "a", "role": "assistant", "content": [
                    {"type": "thinking", "text": "private historical reasoning"},
                    {"type": "text", "text": "visible answer mentions thinking"},
                    {"type": "tool_call", "tool_call_id": "one", "arguments": {"cmd": "check"}},
                ], "assistant": {
                    "thinking": ["private historical reasoning"],
                    "text": ["visible answer mentions thinking"],
                    "tool_calls": [{"type": "tool_call", "tool_call_id": "one", "arguments": {"cmd": "check"}}],
                }},
                {"source_entry_id": "r", "role": "toolResult", "content": [
                    {"type": "text", "text": "tool result mentions thinking"}],
                    "tool_result": {"tool_call_id": "one", "is_error": False}},
            ],
        }
        seen = []

        def counter(messages, *, request_options):
            seen.append(deepcopy(messages))
            return 100

        executor = SimpleNamespace(manager=SimpleNamespace(
            profile=SimpleNamespace(model="controlled", temperature=0, max_output_tokens=100,
                                    manager_context_tokens=1200, safety_tokens=100, reasoning_effort="max"),
            exact_token_counter=counter,
        ))
        builder = lambda values: [
            {"role": "system", "content": "Extract."},
            {"role": "user", "content": json.dumps({"trajectories": values})},
        ]
        with TemporaryDirectory() as tmp:
            context = {
                "artifact_root": tmp,
                "trial_id": "r1:task",
                "driver_config": {"historical_thinking_policy": "exclude"},
            }
            messages, context_ref, visible = driver._prepare_trajectory_messages(
                context=context,
                executor=executor,
                phase="task",
                traces=[trace],
                builder=builder,
            )
            payload = json.loads(messages[-1]["content"])["trajectories"][0]
            rendered = json.dumps(payload)
            self.assertNotIn("private historical reasoning", rendered)
            self.assertIn("visible answer mentions thinking", rendered)
            self.assertIn("tool result mentions thinking", rendered)
            self.assertEqual(visible, {"task": ["a", "r"]})
            record = json.loads(Path(context_ref["path"]).read_text())
            self.assertEqual(record["historical_thinking_policy"], "exclude")
            self.assertEqual(record["historical_thinking_policy_version"], HISTORICAL_THINKING_POLICY_VERSION)
            self.assertEqual(record["original_full_request_tokens"], 100)
            self.assertNotEqual(record["sources"][0]["trace_sha256"], record["sources"][0]["manager_view_sha256"])
            self.assertTrue(record["sources"][0]["historical_thinking_projection"]["recognized_fields_removed"])
        self.assertEqual(len(seen), 1)

    def test_summary_short_ids_expand_only_when_unambiguous(self):
        steps = [{"source_entry_id": "12345678-aaaa", "role": "user", "content": []}]
        value = {"summary": "Observed input.", "evidence_step_ids": ["12345678"]}
        original = deepcopy(value)
        summary, fragments = driver._validate_context_summary(value, segment_steps=steps, final_segment=False)
        self.assertEqual(summary["evidence_id_expansions"], {"12345678": "12345678-aaaa"})
        self.assertEqual(fragments, steps)
        self.assertEqual(value, original)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            driver._validate_context_summary(value, segment_steps=steps + [
                {"source_entry_id": "12345678-bbbb", "role": "user", "content": []}], final_segment=False)

    def test_formal_v02_keeps_cited_tool_batches_and_final_result_without_stress_cap(self):
        steps = [{"source_entry_id": "a", "role": "assistant", "content": [
            {"type": "tool_call", "tool_call_id": str(i)} for i in range(4)]}]
        steps.extend({"source_entry_id": f"r{i}", "role": "toolResult", "content": [],
                      "tool_result": {"tool_call_id": str(i)}} for i in range(4))
        steps.extend([
            {"source_entry_id": "final-action", "role": "assistant", "content": [
                {"type": "tool_call", "tool_call_id": "final"}]},
            {"source_entry_id": "final-result", "role": "toolResult", "content": [],
             "tool_result": {"tool_call_id": "final"}},
        ])
        summary, fragments = driver._validate_context_summary(
            {"summary": "Observed the first batch.", "evidence_step_ids": ["r0"]},
            segment_steps=steps, final_segment=True,
        )
        self.assertEqual(fragments, steps)
        self.assertEqual(summary["covered_step_ids"], [s["source_entry_id"] for s in steps])
        with self.assertRaisesRegex(ValueError, "unknown"):
            driver._validate_context_summary({"summary": "Invented.", "evidence_step_ids": ["unknown"]},
                                             segment_steps=steps, final_segment=True)

    def test_saved_summary_reuse_checks_original_response_source_and_round(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = json.loads((Path(driver.ROOT) / "configs/r015-c-only-coding.json").read_text())["retrieval_profile"]
            executor = SimpleNamespace(journal_root=root / "journals", profile=profile,
                                       manager=SimpleNamespace(run_dir=root / "manager"))
            steps = [{"source_entry_id": "r", "role": "toolResult", "content": [], "tool_result": {}}]
            payload = {"task_context": "repair", "source": {"instance_id": "task"}, "segment_steps": steps, "final_segment": True}
            messages = [{"role": "user", "content": json.dumps(payload)}]
            options = {"reasoning_effort": "max", "max_tokens": 8192}
            summary = {"summary": "The observed check failed.", "covered_step_ids": ["r"], "verbatim_evidence_step_ids": ["r"]}
            policy = {
                "policy": "keep",
                "policy_version": HISTORICAL_THINKING_POLICY_VERSION,
            }
            source_identity = {
                "immutable_trace_sha256": "source-hash",
                "manager_view_sha256": "manager-view-hash",
                "historical_thinking_policy": "keep",
                "historical_thinking_policy_version": HISTORICAL_THINKING_POLICY_VERSION,
            }
            legacy_metadata = {
                "condition": "C-only",
                "trajectory_input_mode": "evidence_compacted",
                "historical_thinking_policy": policy,
                "source_trace_sha256": driver._hash_json(source_identity),
                "segment_step_ids": ["r"],
                "final_segment": True,
                "segment_allowance_tokens": 8192,
            }
            journal_path = executor.journal_root / driver.sha256_text("r1:task")[:20] / "context-summary-saved.json"
            call_dir = executor.manager.run_dir / "model_calls/call-0001"
            journal_path.parent.mkdir(parents=True)
            call_dir.mkdir(parents=True)
            journal = {"trial_id": "r1:task", "status": "context_summary_validated", "call_metadata": legacy_metadata,
                       "messages": messages, "messages_sha256": driver._hash_json(messages), "profile_sha256": driver.profile_sha256(profile),
                       "call_id": "call-0001", "summary": summary}
            journal_path.write_text(json.dumps(journal))
            (call_dir / "request.json").write_text(json.dumps({
                "call_metadata": legacy_metadata,
                "request": {**options, "messages": messages},
            }))
            (call_dir / "response.json").write_text(json.dumps({"finish_reason": "stop", "parsed_response": {
                "choices": [{"message": {"content": json.dumps(summary)}}]}}))
            counter = SimpleNamespace(method="POST", base_url="http://tokenizer")
            request_identity = driver._summary_request_identity(
                messages=messages,
                options=options,
                historical_thinking_policy=policy,
                source_identity=source_identity,
                counter=counter,
            )
            args = dict(trial_id="r1:task", messages=messages, options=options,
                        request_identity=request_identity)
            reused = driver._reuse_context_summary(executor, **args)
            self.assertEqual(reused[0], summary)
            self.assertEqual(reused[1], steps)
            excluded = driver._reuse_context_summary(
                executor,
                **args,
                historical_thinking_policy={
                    "policy": "exclude",
                    "policy_version": HISTORICAL_THINKING_POLICY_VERSION,
                },
            )
            self.assertIsNone(excluded)
            # Explicit recovery can revalidate a completed output after a
            # validator fix; its original rejection and response stay intact.
            journal["status"] = "context_summary_rejected"
            journal["purpose"] = "saved-summary"
            journal.pop("call_id")
            journal_path.write_text(json.dumps(journal))
            executor.manager.ledger_path = root / "ledger.json"
            executor.manager.ledger_path.write_text(json.dumps({"calls": [
                {"call_id": "call-0001", "purpose": "saved-summary", "status": "succeeded"}]}))
            self.assertEqual(driver._reuse_context_summary(executor, **args)[2]["original_journal_status"], "context_summary_rejected")
            journal.update(status="context_summary_validated", call_id="call-0001")
            journal_path.write_text(json.dumps(journal))
            self.assertIsNone(driver._reuse_context_summary(executor, **{**args, "trial_id": "r2:task"}))
            changed_source = {**payload, "source": {"instance_id": "different-task"}}
            changed_source_messages = [{"role": "user", "content": json.dumps(changed_source)}]
            self.assertIsNone(driver._reuse_context_summary(
                executor,
                **{**args, "messages": changed_source_messages,
                   "request_identity": driver._summary_request_identity(
                       messages=changed_source_messages, options=options,
                       historical_thinking_policy={"policy": "keep", "policy_version": HISTORICAL_THINKING_POLICY_VERSION},
                       source_identity={"immutable_trace_sha256": "different-source"}, counter=counter)},
            ))
            changed = deepcopy(payload)
            changed["segment_steps"][0]["content"] = [{"type": "text", "text": "different result"}]
            changed_messages = [{"role": "user", "content": json.dumps(changed)}]
            self.assertIsNone(driver._reuse_context_summary(
                executor,
                **{**args, "messages": changed_messages,
                   "request_identity": driver._summary_request_identity(
                       messages=changed_messages, options=options,
                       historical_thinking_policy={"policy": "keep", "policy_version": HISTORICAL_THINKING_POLICY_VERSION},
                       source_identity={"immutable_trace_sha256": "source-hash"}, counter=counter)},
            ))
            changed_options = {**options, "max_tokens": 4096}
            self.assertIsNone(driver._reuse_context_summary(
                executor,
                **{**args, "options": changed_options,
                   "request_identity": driver._summary_request_identity(
                       messages=messages, options=changed_options,
                       historical_thinking_policy={"policy": "keep", "policy_version": HISTORICAL_THINKING_POLICY_VERSION},
                       source_identity={"immutable_trace_sha256": "source-hash"}, counter=counter)},
            ))
            journal["summary"]["summary"] = "Invented replacement."
            journal_path.write_text(json.dumps(journal))
            with self.assertRaisesRegex(driver.COnlyHarborDriverError, "original response"):
                driver._reuse_context_summary(executor, **args)

    def test_summary_reuse_checks_immutable_request_policy_and_accepts_true_exclude(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace, executor, context, builder, summary = self._summary_live_parts(
                root, policy="exclude"
            )
            with patch(
                "codeskill_rebuild.manager.urlopen",
                return_value=self._summary_http_response(summary),
            ):
                driver._prepare_trajectory_messages(
                    context=context, executor=executor, phase="first",
                    traces=[trace], builder=builder,
                )
            journal_path = next(executor.journal_root.rglob("context-summary-*.json"))
            journal = driver.read_json(journal_path)
            request_path = executor.manager.run_dir / "model_calls/call-0001/request.json"
            request_record = driver.read_json(request_path)
            options = {
                key: value for key, value in request_record["request"].items()
                if key != "messages"
            }
            identity = journal["call_metadata"]["summary_request_identity"]
            policy = {
                "policy": "exclude",
                "policy_version": HISTORICAL_THINKING_POLICY_VERSION,
            }
            self.assertEqual(
                request_record["call_metadata"]["historical_thinking_policy"], policy
            )
            self.assertIsNotNone(driver._reuse_context_summary(
                executor,
                trial_id=context["trial_id"],
                messages=journal["messages"],
                options=options,
                request_identity=identity,
                historical_thinking_policy=policy,
            ))

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace, executor, context, builder, summary = self._summary_live_parts(root)
            with patch(
                "codeskill_rebuild.manager.urlopen",
                return_value=self._summary_http_response(summary),
            ):
                driver._prepare_trajectory_messages(
                    context=context, executor=executor, phase="first",
                    traces=[trace], builder=builder,
                )
            journal_path = next(executor.journal_root.rglob("context-summary-*.json"))
            journal = driver.read_json(journal_path)
            request_path = executor.manager.run_dir / "model_calls/call-0001/request.json"
            request_hash = driver.sha256_file(request_path)
            request_record = driver.read_json(request_path)
            options = {
                key: value for key, value in request_record["request"].items()
                if key != "messages"
            }
            exclude_policy = {
                "policy": "exclude",
                "policy_version": HISTORICAL_THINKING_POLICY_VERSION,
            }
            source_identity = deepcopy(
                journal["call_metadata"]["summary_request_identity"]["source_identity"]
            )
            source_identity["historical_thinking_policy"] = "exclude"
            exclude_identity = driver._summary_request_identity(
                messages=journal["messages"],
                options=options,
                historical_thinking_policy=exclude_policy,
                source_identity=source_identity,
                counter=executor.manager.exact_token_counter,
            )
            journal["call_metadata"]["historical_thinking_policy"] = exclude_policy
            journal["call_metadata"]["summary_request_identity"] = exclude_identity
            driver.write_json(journal_path, journal)
            self.assertIsNone(driver._reuse_context_summary(
                executor,
                trial_id=context["trial_id"],
                messages=journal["messages"],
                options=options,
                request_identity=exclude_identity,
                historical_thinking_policy=exclude_policy,
            ))
            self.assertEqual(driver.sha256_file(request_path), request_hash)
            self.assertEqual(
                request_record["call_metadata"]["historical_thinking_policy"]["policy"],
                "keep",
            )

    def test_full_summary_caller_blocks_old_and_current_uncertain_journals(self):
        with TemporaryDirectory() as seed_tmp:
            seed_root = Path(seed_tmp)
            trace, seed_executor, seed_context, builder, summary = self._summary_live_parts(seed_root)
            with patch(
                "codeskill_rebuild.manager.urlopen",
                return_value=self._summary_http_response(summary),
            ):
                driver._prepare_trajectory_messages(
                    context=seed_context, executor=seed_executor, phase="seed",
                    traces=[trace], builder=builder,
                )
            seed_journal = driver.read_json(
                next(seed_executor.journal_root.rglob("context-summary-*.json"))
            )
            messages = seed_journal["messages"]
            identity = seed_journal["call_metadata"]["summary_request_identity"]

            for identity_kind in ("legacy", "current"):
                for status in ("prepared_before_manager_call", "manager_call_raised"):
                    with self.subTest(identity_kind=identity_kind, status=status), TemporaryDirectory() as tmp:
                        root = Path(tmp)
                        trace2, executor, context, builder2, summary2 = self._summary_live_parts(root)
                        if identity_kind == "legacy":
                            source_key = driver._hash_json(identity["source_identity"])
                            segment_key = driver._hash_json({
                                "source": source_key,
                                "historical_thinking_policy": "keep",
                                "historical_thinking_policy_version": HISTORICAL_THINKING_POLICY_VERSION,
                                "step_ids": ["a", "r"],
                                "messages": messages,
                            })
                            metadata = {
                                "historical_thinking_policy": {
                                    "policy": "keep",
                                    "policy_version": HISTORICAL_THINKING_POLICY_VERSION,
                                },
                                "source_trace_sha256": source_key,
                                "segment_step_ids": ["a", "r"],
                                "final_segment": True,
                            }
                        else:
                            segment_key = driver._hash_json({"summary_request_identity": identity})
                            metadata = deepcopy(seed_journal["call_metadata"])
                        phase = f"context-summary-{segment_key[:20]}"
                        journal_path = executor._prepare_call(
                            trial_id=context["trial_id"],
                            phase=phase,
                            value={
                                "purpose": f"r015_c_only_evidence_summary:{context['trial_id']}:{segment_key}",
                                "messages": messages,
                                "messages_sha256": driver._hash_json(messages),
                                "call_metadata": metadata,
                            },
                        )
                        if status == "manager_call_raised":
                            executor._finish_journal(
                                journal_path,
                                status=status,
                                value={"error_type": "TimeoutError", "automatic_retry": False},
                            )
                        original = journal_path.read_bytes()
                        with patch(
                            "codeskill_rebuild.manager.urlopen",
                            return_value=self._summary_http_response(summary2),
                        ) as http:
                            with self.assertRaisesRegex(
                                driver.COnlyHarborDriverError,
                                "explicit reconciliation is required",
                            ):
                                driver._prepare_trajectory_messages(
                                    context=context, executor=executor, phase="resumed",
                                    traces=[trace2], builder=builder2,
                                )
                        self.assertEqual(http.call_count, 0)
                        self.assertEqual(journal_path.read_bytes(), original)
                        self.assertEqual(
                            len(list(executor.journal_root.rglob("context-summary-*.json"))),
                            1,
                        )

    def test_full_summary_caller_reuses_exact_legacy_completed_and_reconciles_bad_hash(self):
        for corrupt_source_key in (False, True):
            with self.subTest(corrupt_source_key=corrupt_source_key), TemporaryDirectory() as tmp:
                root = Path(tmp)
                trace, executor, context, builder, summary = self._summary_live_parts(root)
                with patch(
                    "codeskill_rebuild.manager.urlopen",
                    return_value=self._summary_http_response(summary),
                ):
                    journal_path, request_path = self._seed_legacy_completed_summary(
                        trace=trace,
                        executor=executor,
                        context=context,
                        summary=summary,
                        corrupt_source_key=corrupt_source_key,
                    )
                journal_bytes = journal_path.read_bytes()
                request_bytes = request_path.read_bytes()
                context.pop("trajectory_summary_cache", None)
                with patch(
                    "codeskill_rebuild.manager.urlopen",
                    return_value=self._summary_http_response(summary),
                ) as http:
                    if corrupt_source_key:
                        with self.assertRaisesRegex(
                            driver.COnlyHarborDriverError,
                            "explicit reconciliation is required",
                        ):
                            driver._prepare_trajectory_messages(
                                context=context,
                                executor=executor,
                                phase="after-upgrade",
                                traces=[trace],
                                builder=builder,
                            )
                    else:
                        driver._prepare_trajectory_messages(
                            context=context,
                            executor=executor,
                            phase="after-upgrade",
                            traces=[trace],
                            builder=builder,
                        )
                self.assertEqual(http.call_count, 0)
                self.assertEqual(journal_path.read_bytes(), journal_bytes)
                self.assertEqual(request_path.read_bytes(), request_bytes)
                self.assertEqual(
                    len(list(executor.journal_root.rglob("context-summary-*.json"))),
                    1,
                )

    def test_full_summary_caller_allows_same_messages_with_different_options(self):
        with TemporaryDirectory() as seed_tmp, TemporaryDirectory() as tmp:
            seed_root = Path(seed_tmp)
            trace, seed_executor, seed_context, builder, summary = self._summary_live_parts(seed_root)
            with patch(
                "codeskill_rebuild.manager.urlopen",
                return_value=self._summary_http_response(summary),
            ):
                driver._prepare_trajectory_messages(
                    context=seed_context, executor=seed_executor, phase="seed",
                    traces=[trace], builder=builder,
                )
            seed_journal = driver.read_json(
                next(seed_executor.journal_root.rglob("context-summary-*.json"))
            )
            old_identity = seed_journal["call_metadata"]["summary_request_identity"]

            root = Path(tmp)
            trace2, executor, context, builder2, summary2 = self._summary_live_parts(
                root, max_output_tokens=101
            )
            old_key = driver._hash_json({"summary_request_identity": old_identity})
            executor._prepare_call(
                trial_id=context["trial_id"],
                phase=f"context-summary-{old_key[:20]}",
                value={
                    "purpose": f"r015_c_only_evidence_summary:{context['trial_id']}:{old_key}",
                    "messages": seed_journal["messages"],
                    "messages_sha256": driver._hash_json(seed_journal["messages"]),
                    "call_metadata": deepcopy(seed_journal["call_metadata"]),
                },
            )
            with patch(
                "codeskill_rebuild.manager.urlopen",
                return_value=self._summary_http_response(summary2),
            ) as http:
                driver._prepare_trajectory_messages(
                    context=context, executor=executor, phase="different-options",
                    traces=[trace2], builder=builder2,
                )
            self.assertEqual(http.call_count, 1)
            self.assertEqual(
                len(list(executor.journal_root.rglob("context-summary-*.json"))),
                2,
            )

    def test_combined_task_payload_compacts_each_source_without_changing_it(self):
        for count in (2, 3):
            with self.subTest(sources=count):
                self._exercise(count, final_overflow=False)

    def test_summary_cache_identity_tracks_prompt_options_and_tokenizer_template(self):
        messages = [{"role": "system", "content": "prompt-v1"}, {"role": "user", "content": "payload"}]
        options = {"model": "controlled", "max_tokens": 100}
        policy = {"policy": "keep", "policy_version": HISTORICAL_THINKING_POLICY_VERSION}
        source = {"immutable_trace_sha256": "trace", "manager_view_sha256": "view"}
        counter = SimpleNamespace(method="POST", base_url="http://tokenizer", template_identity="template-v1")
        baseline = driver._summary_request_identity(
            messages=messages, options=options, historical_thinking_policy=policy,
            source_identity=source, counter=counter,
        )
        self.assertEqual(baseline["summary_output_schema"]["name"], "r015_d03_v02_evidence_summary")
        self.assertEqual(
            baseline,
            driver._summary_request_identity(
                messages=messages, options=options, historical_thinking_policy=policy,
                source_identity=source, counter=counter,
            ),
        )
        variants = [
            {"messages": [{"role": "system", "content": "prompt-v2"}, messages[1]]},
            {"options": {**options, "max_tokens": 101}},
            {"counter": SimpleNamespace(method="POST", base_url="http://tokenizer", template_identity="template-v2")},
        ]
        for change in variants:
            with self.subTest(change=change):
                identity = driver._summary_request_identity(
                    messages=change.get("messages", messages),
                    options=change.get("options", options),
                    historical_thinking_policy=policy,
                    source_identity=source,
                    counter=change.get("counter", counter),
                )
                self.assertNotEqual(identity["identity_sha256"], baseline["identity_sha256"])

    def test_in_memory_summary_reuse_requires_exact_prompt_and_request_options(self):
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
        prompt = {"value": "prompt-v1"}

        class PromptPath:
            @staticmethod
            def read_text(*, encoding):
                return prompt["value"]

        def counter(messages, *, request_options):
            payload = json.loads(messages[-1]["content"])
            if "segment_steps" in payload:
                return 100
            trajectories = payload["trajectories"]
            return 100 if all(value.get("trajectory_input_mode") == "evidence_compacted" for value in trajectories) else 2000

        calls = []

        def summarize(executor, **kwargs):
            calls.append(deepcopy(kwargs["messages"]))
            payload = json.loads(kwargs["messages"][-1]["content"])
            return {
                "call_id": f"summary-{len(calls)}",
                "json": {
                    "summary": "The check completed.",
                    "covered_step_ids": [step["source_entry_id"] for step in payload["segment_steps"]],
                    "verbatim_evidence_step_ids": ["result"],
                },
            }, {"path": "controlled-journal", "sha256": "controlled", "response": {}}, None

        manager = SimpleNamespace(
            profile=SimpleNamespace(model="controlled", temperature=0, max_output_tokens=100,
                                    manager_context_tokens=1200, safety_tokens=100, reasoning_effort="max"),
            exact_token_counter=counter,
        )
        executor = SimpleNamespace(manager=manager)
        builder = lambda values: [
            {"role": "system", "content": "Extract procedures."},
            {"role": "user", "content": json.dumps({"trajectories": values})},
        ]
        with TemporaryDirectory() as tmp, patch.object(driver, "_prompt_path", return_value=PromptPath()), patch.object(
            driver, "_manager_call", side_effect=summarize
        ), patch.object(driver, "_finish_manager_journal", side_effect=lambda executor, journal, **kwargs: journal):
            context = {"artifact_root": tmp, "trial_id": "r1:task"}
            for phase in ("one", "two"):
                driver._prepare_trajectory_messages(
                    context=context, executor=executor, phase=phase, traces=[trace], builder=builder,
                )
            self.assertEqual(len(calls), 1, "identical request must reuse the in-memory summary")
            prompt["value"] = "prompt-v2"
            driver._prepare_trajectory_messages(
                context=context, executor=executor, phase="three", traces=[trace], builder=builder,
            )
            self.assertEqual(len(calls), 2, "prompt drift must create a new summary call")
            manager.profile.max_output_tokens = 101
            driver._prepare_trajectory_messages(
                context=context, executor=executor, phase="four", traces=[trace], builder=builder,
            )
            self.assertEqual(len(calls), 3, "request-option drift must create a new summary call")

    def test_uncertain_summary_journal_is_never_treated_as_reusable(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial_id = "r1:task"
            journal_dir = root / "journals" / driver.sha256_text(trial_id)[:20]
            journal_dir.mkdir(parents=True)
            messages = [{"role": "user", "content": json.dumps({
                "segment_steps": [], "final_segment": False,
            })}]
            (journal_dir / "context-summary-uncertain.json").write_text(json.dumps({
                "trial_id": trial_id,
                "status": "manager_call_raised",
                "messages": messages,
                "messages_sha256": driver._hash_json(messages),
            }))
            executor = SimpleNamespace(journal_root=root / "journals")
            identity = driver._summary_request_identity(
                messages=messages,
                options={"max_tokens": 100},
                historical_thinking_policy={"policy": "keep", "policy_version": HISTORICAL_THINKING_POLICY_VERSION},
                source_identity={"immutable_trace_sha256": "trace"},
                counter=SimpleNamespace(method="POST", base_url="http://tokenizer"),
            )
            self.assertIsNone(driver._reuse_context_summary(
                executor,
                trial_id=trial_id,
                messages=messages,
                options={"max_tokens": 100},
                request_identity=identity,
            ))

    def test_remaining_overflow_is_recorded_and_does_not_become_a_skip(self):
        self._exercise(2, final_overflow=True)

    def _exercise(self, count, *, final_overflow):
        traces = [{
            "source": {"instance_id": f"task-{i}", "canonical_instance_id": f"task-{i}"},
            "instruction": f"Repair task {i}", "text_manager_eligible": True,
            "outcome": {"reward": 0},
            "steps": [
                {"source_entry_id": "u", "role": "user", "content": [{"type": "text", "text": "repair"}]},
                {"source_entry_id": "a", "role": "assistant", "content": [
                    {"type": "tool_call", "tool_call_id": "one", "name": "exec", "arguments": {"cmd": f"check-{i}"}}]},
                {"source_entry_id": "r", "role": "toolResult", "content": [{"type": "text", "text": f"failure-{i}"}],
                 "tool_result": {"tool_call_id": "one"}},
            ],
        } for i in range(count)]
        original = deepcopy(traces)
        options_seen = []

        def counter(messages, *, request_options):
            options_seen.append(request_options)
            payload = json.loads(messages[-1]["content"])
            if "segment_steps" in payload:
                return 600  # Each source fits; their combined request does not.
            compact = all(t.get("evidence_compacted") for t in payload["trajectories"])
            return 1100 if compact and final_overflow else (400 if compact else 600 * len(payload["trajectories"]))

        summary_sources = []

        def summarize(executor, **kwargs):
            payload = json.loads(kwargs["messages"][-1]["content"])
            summary_sources.append(payload["source"]["canonical_instance_id"])
            return {"call_id": f"summary-{len(summary_sources)}", "json": {
                "summary": "The original check returned a failure.",
                "covered_step_ids": [s["source_entry_id"] for s in payload["segment_steps"]],
                "verbatim_evidence_step_ids": ["r"],
            }}, {"path": "controlled-journal", "sha256": "controlled", "response": {}}, None

        executor = SimpleNamespace(manager=SimpleNamespace(
            profile=SimpleNamespace(model="controlled", temperature=0, max_output_tokens=100,
                                    manager_context_tokens=1200, safety_tokens=100, reasoning_effort="max"),
            exact_token_counter=counter,
        ))
        builder = lambda values: task_extraction_messages(values, paper_prompt="Extract common procedures.")
        with TemporaryDirectory() as tmp, patch.object(driver, "_manager_call", side_effect=summarize), patch.object(
            driver, "_finish_manager_journal", side_effect=lambda executor, journal, **kwargs: journal
        ):
            context = {"artifact_root": tmp, "trial_id": "r1:current"}
            args = dict(context=context, executor=executor, traces=traces, builder=builder)
            if final_overflow:
                with self.assertRaises(ContextBlocked):
                    driver._prepare_trajectory_messages(phase="task", **args)
            else:
                messages, _, visible = driver._prepare_trajectory_messages(phase="task", **args)
                evidence = json.loads(messages[-1]["content"])["trajectories"]
                for i, value in enumerate(evidence):
                    self.assertEqual(value["original_cited_fragments"], original[i]["steps"][1:])
                    self.assertEqual(value["evidence_summaries"][0]["covered_step_ids"], ["u", "a", "r"])
                    self.assertEqual(visible[f"task-{i}"], ["a", "r"])
                driver._prepare_trajectory_messages(phase="task-next", **args)
            record = json.loads((Path(tmp) / "manager-context" / "task.json").read_text())
            self.assertEqual(record["original_full_request_tokens"], 600 * count)
            self.assertEqual(record["state"], "context_blocked" if final_overflow else "within_budget")
            self.assertEqual(record["trajectory_input_mode"], "evidence_compacted")
        self.assertEqual(summary_sources, [f"task-{i}" for i in range(count)])
        self.assertEqual(traces, original)
        self.assertTrue(all(o["reasoning_effort"] == "max" and o["max_tokens"] == 100 for o in options_seen))
