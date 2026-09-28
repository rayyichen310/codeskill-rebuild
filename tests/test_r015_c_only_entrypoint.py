from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from codeskill_rebuild.c_only_protocol import COnlyProtocol, COnlyProtocolError
from codeskill_rebuild.types import canonical_json, sha256_file, sha256_text, write_json
from scripts.run_r015_c_only import (
    _apply_driver_output,
    _driver_process_path,
    _driver_stage_path,
    _invoke_driver,
    _recover_driver_stages,
    _run_driver,
    _trial_stage_is_safe_continuation,
    _write_driver_input,
    _validate_driver_payload_refs,
    _ensure_runtime_parity_gate,
)
from scripts.run_r015_c_only_harbor_driver import _effective_task_limits
from scripts.run_r015_c_only_harbor_driver import (
    COnlyHarborDriverError,
    _import_official_evidence,
    _official_failure_packet,
    _prepare_shared_permit_directory,
    _result_exception_info,
    _read_attempts,
    _snapshot_manager_ledger,
)
from scripts.run_r015_c_only_harbor_driver import _resolve_executable_path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "docs" / "baselines" / "r015-legacy-coding-baseline-20260913.json"
ENTRYPOINT = ROOT / "scripts" / "run_r015_c_only.py"


class R015COnlyEntrypointTest(unittest.TestCase):
    def test_shared_permit_directory_preserves_host_gid_on_posix(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "permits"
            with (
                patch("scripts.run_r015_c_only_harbor_driver.os.name", "posix"),
                patch.object(Path, "chmod") as chmod,
            ):
                _prepare_shared_permit_directory(target)
            self.assertTrue(target.is_dir())
            chmod.assert_called_once_with(0o2770)

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src")
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        return environment

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ENTRYPOINT), *args],
            cwd=ROOT,
            env=self._environment(),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def _prepared_fixture(self) -> tuple[tempfile.TemporaryDirectory[str], Path, Path, Path]:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        config = root / "config.json"
        state = root / "state.json"
        prepared = self._run(
            "prepare",
            "--baseline-manifest",
            str(BASELINE),
            "--output-config",
            str(config),
            "--state",
            str(state),
        )
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        return tmp, config, state, root / "run"

    def test_driver_attempt_loader_accepts_only_a_bound_preflight_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "overlay-state.json"
            sidecar = root / "sidecar"
            attempts = sidecar / "upstream_requests"
            attempts.mkdir(parents=True)
            trial_id = "r1:C:fixture"
            session_id = "session-fixture"
            normal = {
                "kind": "r012_actual_upstream_request",
                "trial_id": trial_id,
                "attempt_ordinal": 1,
                "state_path": str(state_path.resolve()),
                "normal_call_boundary": {
                    "kind": "r012_public_plugin_normal_call_boundary",
                    "trial_id": trial_id,
                    "session_id": session_id,
                },
            }
            terminal = {
                "schema_version": 3,
                "kind": "r012_actual_upstream_request",
                "trial_id": trial_id,
                "attempt_ordinal": 2,
                "forwarded_request_ordinal": 2,
                "native_request": {"messages": [{"role": "user", "content": "native"}]},
                "preflight_candidate_forwarded_request": {
                    "messages": [{"role": "user", "content": "candidate"}],
                },
                "exact_forwarded_input_tokens": 11,
                "max_input_tokens": 10,
                "token_count_scope": "complete_forwarded_openai_payload",
                "uncommitted_selection": {},
                "outcome": "context_or_overlay_error",
                "error_type": "OverlayInputLimitError",
                "error": "forwarded input 11 exceeds configured input budget 10",
                "state_path": str(state_path.resolve()),
            }
            (attempts / "attempt-0001.json").write_text(json.dumps(normal), encoding="utf-8")
            (attempts / "attempt-0002.json").write_text(json.dumps(terminal), encoding="utf-8")
            loaded = _read_attempts(
                sidecar,
                trial_id=trial_id,
                session_id=session_id,
                state_path=state_path,
            )
            self.assertEqual(len(loaded), 2)
            self.assertEqual(
                loaded[1]["_non_forwarded_terminal"]["reason"],
                "input_budget_rejected_before_provider_boundary",
            )
            self.assertNotIn("normal_call_boundary", loaded[1])

            ambiguous = dict(terminal)
            ambiguous["forwarded_request"] = {"messages": []}
            (attempts / "attempt-0002.json").write_text(json.dumps(ambiguous), encoding="utf-8")
            with self.assertRaisesRegex(COnlyHarborDriverError, "validated public boundary"):
                _read_attempts(sidecar, trial_id=trial_id, session_id=session_id, state_path=state_path)

            ambiguous = dict(terminal)
            ambiguous["normal_call_boundary"] = {
                "kind": "r012_public_plugin_normal_call_boundary",
                "trial_id": trial_id,
                "session_id": session_id,
            }
            (attempts / "attempt-0002.json").write_text(json.dumps(ambiguous), encoding="utf-8")
            with self.assertRaisesRegex(COnlyHarborDriverError, "mixes a public normal_call_boundary"):
                _read_attempts(sidecar, trial_id=trial_id, session_id=session_id, state_path=state_path)

            wrong_path = dict(terminal)
            wrong_path["state_path"] = str((root / "other-overlay-state.json").resolve())
            (attempts / "attempt-0002.json").write_text(json.dumps(wrong_path), encoding="utf-8")
            with self.assertRaisesRegex(COnlyHarborDriverError, "state path differs"):
                _read_attempts(sidecar, trial_id=trial_id, session_id=session_id, state_path=state_path)

    def test_check_and_resume_keep_formal_campaign_not_started(self) -> None:
        tmp, config, state, run_dir = self._prepared_fixture()
        self.addCleanup(tmp.cleanup)
        checked = self._run(
            "check",
            "--config",
            str(config),
            "--baseline-manifest",
            str(BASELINE),
            "--state",
            str(state),
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)
        check_value = json.loads(checked.stdout)
        self.assertEqual(check_value["formal_campaign"], "not_started")
        self.assertEqual(check_value["current_round"], 1)
        resumed = self._run(
            "resume",
            "--config",
            str(config),
            "--baseline-manifest",
            str(BASELINE),
            "--state",
            str(state),
            "--run-dir",
            str(run_dir),
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        resume_value = json.loads(resumed.stdout)
        self.assertEqual(resume_value["formal_campaign"], "not_started")
        self.assertEqual(resume_value["current_task"], "build-pmars")

    def test_start_cannot_open_formal_gate_without_explicit_confirmation(self) -> None:
        tmp, config, state, run_dir = self._prepared_fixture()
        self.addCleanup(tmp.cleanup)
        result = self._run(
            "start",
            "--config",
            str(config),
            "--baseline-manifest",
            str(BASELINE),
            "--state",
            str(state),
            "--run-dir",
            str(run_dir),
            "--trial-driver",
            str(ROOT / "missing-approved-driver"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("formal start is gated", result.stderr + result.stdout)
        state_value = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(state_value["formal_campaign"], "not_started")

    def test_resume_stops_on_partial_state_without_relaunching_paid_driver(self) -> None:
        tmp, config, state, run_dir = self._prepared_fixture()
        self.addCleanup(tmp.cleanup)
        protocol = COnlyProtocol.load(state, config, BASELINE)
        protocol.authorize_start()
        task_id = protocol.current_task_id
        self.assertEqual(task_id, "build-pmars")
        assignment = protocol.freeze_task(task_id)
        protocol.save(state)
        trajectory_path = Path(tmp.name) / "trajectory.json"
        write_json(
            trajectory_path,
            {
                "r015_binding": {
                    "round_id": 1,
                    "task_id": task_id,
                    "trial_id": assignment["trial_id"],
                    "session_id": "partial-session",
                },
                "source": {"canonical_instance_id": task_id},
            },
        )
        protocol.record_trial(
            task_id,
            outcome="completed",
            trajectory={
                "round_id": 1,
                "task_id": task_id,
                "trial_id": assignment["trial_id"],
                "session_id": "partial-session",
                "complete": True,
                "path": str(trajectory_path),
                "sha256": sha256_file(trajectory_path),
            },
            raw_evidence={
                "condition": "C-only",
                "round_id": 1,
                "task_id": task_id,
                "trial_id": assignment["trial_id"],
                "session_id": "partial-session",
                "official_harbor_trial": True,
            },
        )
        protocol.save(state)
        fake_driver = Path(tmp.name) / "fake-driver.py"
        fake_driver.write_text(
            "from pathlib import Path\nPath(r'" + str(Path(tmp.name) / "launched") + "').write_text('launched')\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(COnlyProtocolError, "automatic paid retry is disabled"):
            _run_driver(protocol, fake_driver, run_dir, state)
        self.assertFalse((Path(tmp.name) / "launched").exists())
        self.assertTrue((run_dir / "round-1" / task_id / "reconcile-required.json").is_file())

    def test_resume_reuses_a_completed_atomic_driver_output_after_state_write(self) -> None:
        """A crash after an atomic driver exit must not launch a second trial."""
        tmp, config, state, run_dir = self._prepared_fixture()
        self.addCleanup(tmp.cleanup)
        protocol = COnlyProtocol.load(state, config, BASELINE)
        protocol.authorize_start()
        task_id = protocol.current_task_id
        self.assertEqual(task_id, "build-pmars")
        assignment = protocol.freeze_task(task_id)
        protocol.save(state)
        # The launch intent is written while the assignment is still pending;
        # the driver may then finish atomically before the coordinator writes
        # its first phase.  Resuming must accept the expected state digest
        # evolution without changing this original assignment snapshot.
        input_path = _write_driver_input(protocol, assignment, run_dir, state)
        output_path = input_path.with_name("driver-output.json")
        trajectory_path = Path(tmp.name) / "trajectory.json"
        write_json(
            trajectory_path,
            {
                "r015_binding": {
                    "round_id": 1,
                    "task_id": task_id,
                    "trial_id": assignment["trial_id"],
                    "session_id": "atomic-session",
                },
                "source": {"canonical_instance_id": task_id},
            },
        )
        trajectory = {
            "round_id": 1,
            "task_id": task_id,
            "trial_id": assignment["trial_id"],
            "session_id": "atomic-session",
            "complete": True,
            "path": str(trajectory_path),
            "sha256": sha256_file(trajectory_path),
        }
        raw = {
            "condition": "C-only",
            "round_id": 1,
            "task_id": task_id,
            "trial_id": assignment["trial_id"],
            "session_id": "atomic-session",
            "official_harbor_trial": True,
        }
        protocol.record_trial(task_id, outcome="completed", trajectory=trajectory, raw_evidence=raw)
        protocol.save(state)
        extraction = {
            "condition": "C-only",
            "round_id": 1,
            "task_id": task_id,
            "decision": "skip",
            "reason": "controlled atomic-resume fixture",
            "candidates": [],
            "trajectory_ref": trajectory,
            "description_records": [],
            "evidence": {"fixture": False},
        }
        publication = {
            "condition": "C-only",
            "round_id": 1,
            "task_id": task_id,
            "operations": [],
            "manager_decisions": [],
        }
        output = {
            "schema_version": 1,
            "kind": "r015_c_only_trial_driver_output",
            "condition": "C-only",
            "round_id": 1,
            "task_id": task_id,
            "trial": deepcopy(protocol._assignment(task_id)["trial_evidence"]),
            "event_attempts": [],
            "extraction": extraction,
            "publication": publication,
            "official_process": {"classification": "completed"},
            "historical_baseline_used": False,
        }
        # Keep the test's atomic output independent of any executable driver.
        # _invoke_driver must consume the verified hashes and _run_driver must
        # finish the remaining protocol phases without spawning a child.
        write_json(output_path, output)
        self.assertTrue(output_path.is_file(), output_path)
        write_json(
            _driver_process_path(input_path),
            {
                "schema_version": 1,
                "kind": "r015_c_only_driver_process",
                "status": "succeeded",
                "condition": "C-only",
                "round_id": 1,
                "task_id": task_id,
                "trial_id": assignment["trial_id"],
                "command": ["unavailable-after-crash"],
                "input_path": str(input_path),
                "input_sha256": sha256_file(input_path),
                "output_path": str(output_path),
                "output_sha256": sha256_file(output_path),
                "returncode": 0,
                "timed_out": False,
            },
        )
        fake_driver = Path(tmp.name) / "must-not-launch.py"
        fake_driver.write_text(
            "from pathlib import Path\nPath(r'" + str(Path(tmp.name) / "launched") + "').write_text('launched')\n",
            encoding="utf-8",
        )
        reused, _process = _invoke_driver(
            driver=fake_driver,
            input_path=input_path,
            output_path=output_path,
            assignment=assignment,
            protocol=protocol,
        )
        _apply_driver_output(protocol, reused, assignment, state, allow_test_fixture=True)
        self.assertFalse((Path(tmp.name) / "launched").exists())
        restored = COnlyProtocol.load(state, config, BASELINE)
        self.assertEqual(restored.current_task_id, "cancel-async-tasks")
        self.assertEqual(restored._round(1)["status"], "active")

    def test_phase_resume_accepts_current_task_pool_records_derived_after_extraction(self) -> None:
        """The immutable launch input survives a stateful extraction resume."""
        tmp, config, state, run_dir = self._prepared_fixture()
        self.addCleanup(tmp.cleanup)
        protocol = COnlyProtocol.load(state, config, BASELINE)
        protocol.authorize_start()
        task_id = protocol.current_task_id
        self.assertEqual(task_id, "build-pmars")
        assignment = protocol.freeze_task(task_id)
        protocol.save(state)
        input_path = _write_driver_input(protocol, assignment, run_dir, state)
        trajectory_path = Path(tmp.name) / "trajectory.json"
        write_json(
            trajectory_path,
            {
                "r015_binding": {
                    "round_id": 1,
                    "task_id": task_id,
                    "trial_id": assignment["trial_id"],
                    "session_id": "phase-resume-session",
                },
                "source": {"canonical_instance_id": task_id},
            },
        )
        trajectory = {
            "round_id": 1,
            "task_id": task_id,
            "trial_id": assignment["trial_id"],
            "session_id": "phase-resume-session",
            "complete": True,
            "path": str(trajectory_path),
            "sha256": sha256_file(trajectory_path),
        }
        protocol.record_trial(
            task_id,
            outcome="completed",
            trajectory=trajectory,
            raw_evidence={
                "condition": "C-only",
                "round_id": 1,
                "task_id": task_id,
                "trial_id": assignment["trial_id"],
                "session_id": "phase-resume-session",
                "official_harbor_trial": True,
            },
        )
        protocol.extract_after_task(
            task_id,
            candidates=[],
            trajectory_ref=trajectory,
            extraction_evidence={"status": "skip", "reason": "controlled phase-resume fixture"},
            decision="skip",
            reason="controlled phase-resume fixture",
        )
        protocol.save(state)
        # The state now includes this task in trajectory_pool, but the launch
        # input remains bound to the pre-trial pool.  Rebuilding the input must
        # accept only that expected derived material and preserve its path.
        self.assertEqual(_write_driver_input(protocol, assignment, run_dir, state), input_path)

    def test_recovery_accepts_only_the_input_bound_launch_state_after_coordinator_save(self) -> None:
        """A coordinator save may move the state hash without widening refs."""
        tmp, config, state, run_dir = self._prepared_fixture()
        self.addCleanup(tmp.cleanup)
        protocol = COnlyProtocol.load(state, config, BASELINE)
        protocol.authorize_start()
        task_id = protocol.current_task_id
        self.assertEqual(task_id, "build-pmars")
        assignment = protocol.freeze_task(task_id)
        protocol.save(state)
        input_path = _write_driver_input(protocol, assignment, run_dir, state)
        output_path = input_path.with_name("driver-output.json")
        frozen_state_hash = json.loads(input_path.read_text(encoding="utf-8"))["state"]["sha256"]

        # This is the normal coordinator transition that caused the original
        # recovery failure: the launch input remains immutable while the
        # mutable state file receives a later save.
        protocol.save(state)
        self.assertNotEqual(sha256_file(state), frozen_state_hash)
        lifecycle_state = {"path": str(state.resolve()), "sha256": frozen_state_hash}
        payload = {
            "trial": {
                "condition": "C-only",
                "round_id": assignment["round_id"],
                "task_id": task_id,
                "trial_id": assignment["trial_id"],
                "outcome": "completed",
            },
            "trace": {},
            "proxy_attempt_records": [
                {"task_selection": {"query": {"lifecycle_state": lifecycle_state}}}
            ],
            "official_process": {"official_trial_boundary_started": True},
        }
        stage_path = _driver_stage_path(input_path, "trial")
        write_json(
            stage_path,
            {
                "schema_version": 1,
                "kind": "r015_c_only_driver_stage",
                "status": "complete",
                "phase": "trial",
                "condition": "C-only",
                "round_id": assignment["round_id"],
                "task_id": task_id,
                "trial_id": assignment["trial_id"],
                "input_path": str(input_path),
                "input_sha256": sha256_file(input_path),
                "payload_sha256": sha256_text(canonical_json(payload)),
                "payload": payload,
            },
        )
        write_json(
            _driver_process_path(input_path),
            {
                "schema_version": 1,
                "kind": "r015_c_only_driver_process",
                "status": "failed",
                "condition": "C-only",
                "round_id": assignment["round_id"],
                "task_id": task_id,
                "trial_id": assignment["trial_id"],
                "input_path": str(input_path),
                "input_sha256": sha256_file(input_path),
                "output_path": str(output_path),
                "returncode": 1,
                "timed_out": False,
            },
        )
        self.assertTrue(
            _trial_stage_is_safe_continuation(
                input_path=input_path,
                output_path=output_path,
                assignment=assignment,
                allow_test_fixture=True,
            )
        )

        valid_stage = json.loads(stage_path.read_text(encoding="utf-8"))
        for invalid_path, invalid_hash in ((str(state.resolve()), "0" * 64), (str(config.resolve()), frozen_state_hash)):
            invalid = deepcopy(valid_stage)
            lifecycle = invalid["payload"]["proxy_attempt_records"][0]["task_selection"]["query"]["lifecycle_state"]
            lifecycle["path"] = invalid_path
            lifecycle["sha256"] = invalid_hash
            invalid["payload_sha256"] = sha256_text(canonical_json(invalid["payload"]))
            write_json(stage_path, invalid)
            with self.assertRaisesRegex(COnlyProtocolError, "does not match its evidence file"):
                _trial_stage_is_safe_continuation(
                    input_path=input_path,
                    output_path=output_path,
                    assignment=assignment,
                    allow_test_fixture=True,
                )

    def test_active_resume_selects_the_built_in_driver_without_external_path(self) -> None:
        """An active campaign can recover through the production default driver."""
        tmp, config, state, run_dir = self._prepared_fixture()
        self.addCleanup(tmp.cleanup)
        protocol = COnlyProtocol.load(state, config, BASELINE)
        protocol.authorize_start()
        protocol.save(state)
        from scripts.run_r015_c_only import resume

        args = type("ResumeArgs", (), {})()
        args.state = state
        args.config = config
        args.baseline_manifest = BASELINE
        args.trial_driver = None
        args.confirm_user_start = False
        args.accept_runtime_deviation = True
        args.run_dir = run_dir
        with patch("scripts.run_r015_c_only._run_driver") as run_driver:
            result = resume(args)
        run_driver.assert_called_once()
        self.assertEqual(Path(run_driver.call_args.args[1]).name, "run_r015_c_only_harbor_driver.py")
        self.assertEqual(result["status"], "active")

    def test_active_resume_passes_reconciliation_manifest_to_built_in_driver(self) -> None:
        """An explicit audit must reach the production driver unchanged."""
        tmp, config, state, run_dir = self._prepared_fixture()
        self.addCleanup(tmp.cleanup)
        protocol = COnlyProtocol.load(state, config, BASELINE)
        protocol.authorize_start()
        protocol.save(state)
        from scripts.run_r015_c_only import resume

        run_dir.mkdir(parents=True)
        manifest = run_dir / "manager-reconciliation.json"
        manifest.write_text("{}", encoding="utf-8")
        args = type("ResumeArgs", (), {})()
        args.state = state
        args.config = config
        args.baseline_manifest = BASELINE
        args.trial_driver = None
        args.confirm_user_start = False
        args.accept_runtime_deviation = True
        args.run_dir = run_dir
        args.reconciliation_manifest = manifest
        with patch("scripts.run_r015_c_only._run_driver") as run_driver:
            result = resume(args)
        run_driver.assert_called_once()
        self.assertEqual(
            run_driver.call_args.kwargs["reconciliation_manifest_path"],
            manifest.resolve(),
        )
        self.assertEqual(result["status"], "active")

    def test_prepared_resume_gate_selects_the_built_in_driver(self) -> None:
        """The first gated resume also has a production driver default."""
        tmp, config, state, run_dir = self._prepared_fixture()
        self.addCleanup(tmp.cleanup)
        from scripts.run_r015_c_only import resume

        args = type("ResumeArgs", (), {})()
        args.state = state
        args.config = config
        args.baseline_manifest = BASELINE
        args.trial_driver = None
        args.confirm_user_start = True
        args.accept_runtime_deviation = True
        args.run_dir = run_dir
        with patch("scripts.run_r015_c_only._run_driver") as run_driver:
            result = resume(args)
        run_driver.assert_called_once()
        self.assertEqual(Path(run_driver.call_args.args[1]).name, "run_r015_c_only_harbor_driver.py")
        self.assertEqual(result["status"], "active")
        self.assertEqual(COnlyProtocol.load(state, config, BASELINE).state["formal_campaign"], "active")

    def test_runtime_parity_gate_blocks_without_explicit_deviation_acceptance(self) -> None:
        """A prepared version mismatch cannot become a silent formal run."""
        tmp, config, state, run_dir = self._prepared_fixture()
        self.addCleanup(tmp.cleanup)
        protocol = COnlyProtocol.load(state, config, BASELINE)
        with self.assertRaisesRegex(COnlyProtocolError, "runtime parity gate is blocked"):
            _ensure_runtime_parity_gate(protocol, accept_runtime_deviation=False, state_path=state)
        self.assertNotIn("runtime_parity_decision", protocol.state)
        _ensure_runtime_parity_gate(protocol, accept_runtime_deviation=True, state_path=state)
        reloaded = COnlyProtocol.load(state, config, BASELINE)
        self.assertEqual(reloaded.state["runtime_parity_decision"]["status"], "approved_deviation")

    def test_effective_task_limits_materialize_public_task_values_and_job_multipliers(self) -> None:
        metadata = {
            "public_environment": {
                "agent_timeout_sec": 900,
                "verifier_timeout_sec": 120,
                "build_timeout_sec": 60,
            }
        }
        limits = _effective_task_limits(
            metadata,
            job_value={
                "agent_timeout_multiplier": 4.0,
                "verifier_timeout_multiplier": 1.5,
                "environment_build_timeout_multiplier": 2.0,
            },
            agent_setup_timeout_seconds=360,
        )
        self.assertEqual(limits["task_base_seconds"], {"agent": 900, "agent_setup": None, "verifier": 120, "environment_build": 60})
        self.assertEqual(limits["effective_seconds"], {"agent": 3600.0, "agent_setup": 360.0, "verifier": 180.0, "environment_build": 120.0})

    def test_driver_output_boundary_accepts_a_valid_changed_title_merge(self) -> None:
        """A Fig.9 merge binds extraction input and final bank candidate separately."""
        tmp, config, state, run_dir = self._prepared_fixture()
        self.addCleanup(tmp.cleanup)
        # Use only the first two configured tasks while retaining the real
        # production config's task identities through a small protocol config
        # fixture would make the baseline hash invalid.  Instead run the
        # actual first two assignments and stop after the second publication.
        protocol = COnlyProtocol.load(state, config, BASELINE)
        protocol.authorize_start()
        protocol.save(state)

        def trajectory(task_id: str, assignment: dict[str, object]) -> dict[str, object]:
            path = Path(tmp.name) / f"{task_id}-trajectory.json"
            value = {
                "r015_binding": {
                    "round_id": assignment["round_id"],
                    "task_id": task_id,
                    "trial_id": assignment["trial_id"],
                    "session_id": f"driver-boundary-{task_id}",
                },
                "source": {"canonical_instance_id": task_id},
            }
            write_json(path, value)
            return {
                "round_id": assignment["round_id"],
                "task_id": task_id,
                "trial_id": assignment["trial_id"],
                "session_id": f"driver-boundary-{task_id}",
                "complete": True,
                "path": str(path),
                "sha256": sha256_file(path),
            }

        def skill(title: str, task_id: str) -> dict[str, object]:
            return {
                "title": title,
                "granularity": "event",
                "when_to_apply": "When a reproducible terminal repair needs a validation loop.",
                "rules": ["Inspect the failure, make the bounded repair, and rerun validation."],
                "benchmark": "terminal-bench",
                "provenance": {
                    "source_instance_ids": [task_id],
                    "source_instance_ids_raw": [f"terminal-bench/{task_id}"],
                    "parent_skill_ids": [],
                },
            }

        def output_for(task_id: str, assignment: dict[str, object], *, final_skill: dict[str, object] | None = None, merge_target: dict[str, object] | None = None) -> dict[str, object]:
            trace = trajectory(task_id, assignment)
            raw = {
                "official_harbor_trial": True,
                "condition": "C-only",
                "round_id": assignment["round_id"],
                "task_id": task_id,
                "trial_id": assignment["trial_id"],
                "session_id": trace["session_id"],
                "historical_baseline_used": False,
            }
            extracted = skill(f"{task_id} extracted", task_id)
            response = Path(tmp.name) / f"{task_id}-fig9-response.json"
            write_json(response, {"kind": "controlled-fig9", "task_id": task_id})
            response_hash = sha256_file(response)
            candidate = final_skill or extracted
            original_fp = sha256_text(canonical_json(extracted))
            final_fp = sha256_text(canonical_json(candidate))
            evidence = {
                "kind": "controlled-fig9",
                "manager_response_sha256": response_hash,
                "manager_response_path": str(response),
                "fig9_response_sha256": response_hash,
                "fig9_response_path": str(response),
                "original_candidate_fingerprint": original_fp,
                "merged_candidate_fingerprint": final_fp,
                "extraction_candidate_fingerprint": original_fp,
            }
            operation: dict[str, object] = {
                "operation_id": f"driver-boundary-fig9-{task_id}",
                "source_kind": "extraction",
                "candidate_id": "candidate-1",
                "original_candidate": extracted,
                "candidate": candidate,
                "decision": "merge" if merge_target is not None else "add",
                "source_instance_ids": [task_id],
                "evidence": evidence,
            }
            if merge_target is not None:
                operation["merge_target_id"] = merge_target["skill_id"]
                evidence["merge_target_skill"] = deepcopy(merge_target)
                evidence["merge_target_skill_fingerprint"] = sha256_text(canonical_json(merge_target))
            trial = {
                "condition": "C-only",
                "round_id": assignment["round_id"],
                "task_id": task_id,
                "trial_id": assignment["trial_id"],
                "outcome": "completed",
                "trajectory": trace,
                "supplied_skills": [],
                "raw_evidence": raw,
            }
            return {
                "schema_version": 1,
                "kind": "r015_c_only_trial_driver_output",
                "condition": "C-only",
                "round_id": assignment["round_id"],
                "task_id": task_id,
                "trial": trial,
                "event_attempts": [],
                "extraction": {
                    "condition": "C-only",
                    "round_id": assignment["round_id"],
                    "task_id": task_id,
                    "decision": "extract",
                    "reason": None,
                    "candidates": [{"candidate_id": "candidate-1", "skill": extracted}],
                    "trajectory_ref": trace,
                    "description_records": [],
                    "evidence": {"kind": "controlled-extraction"},
                },
                "publication": {
                    "condition": "C-only",
                    "round_id": assignment["round_id"],
                    "task_id": task_id,
                    "operations": [operation],
                    "manager_decisions": [],
                },
                "official_process": {"classification": "controlled_fixture", "returncode": 0},
                "historical_baseline_used": False,
            }

        first_assignment = protocol.freeze_task("build-pmars")
        first_output = output_for("build-pmars", first_assignment)
        _apply_driver_output(protocol, first_output, first_assignment, state, allow_test_fixture=True)
        second_assignment = protocol.freeze_task("cancel-async-tasks")
        target = second_assignment["frozen_bank"]["skills"][0]
        merged = skill("cancel-async-tasks merged", "cancel-async-tasks")
        second_output = output_for("cancel-async-tasks", second_assignment, final_skill=merged, merge_target=target)
        _apply_driver_output(protocol, second_output, second_assignment, state, allow_test_fixture=True)
        active = [item for item in protocol._round()["bank"]["skills"] if item.get("status") == "active"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["title"], "cancel-async-tasks merged")
        self.assertEqual(set(active[0]["provenance"]["source_instance_ids"]), {"build-pmars", "cancel-async-tasks"})

    def test_production_driver_boundary_completes_two_rounds_and_resume_does_not_relaunch(self) -> None:
        """The coordinator/driver boundary is isolated across both rounds."""
        tmp, config, state, run_dir = self._prepared_fixture()
        self.addCleanup(tmp.cleanup)
        protocol = COnlyProtocol.load(state, config, BASELINE)
        protocol.authorize_start()
        protocol.save(state)
        invocation_log = Path(tmp.name) / "driver-invocations.log"
        fake_driver = Path(tmp.name) / "controlled-driver.py"
        fake_driver.write_text(
            """import hashlib
import json
import sys
from pathlib import Path

args = sys.argv
input_path = Path(args[args.index('--input') + 1])
output_path = Path(args[args.index('--output') + 1])
value = json.loads(input_path.read_text(encoding='utf-8'))
assignment = value['assignment']
task_id = assignment['task_id']
round_id = assignment['round_id']
run_root = output_path.parents[2]
counter = run_root.parent / 'driver-invocations.log'
with counter.open('a', encoding='utf-8') as stream:
    stream.write(f"{round_id}:{task_id}\\n")
session_id = f"controlled-r{round_id}-{task_id}"
trajectory = None
outcome = 'infra_failure' if task_id == 'polyglot-c-py' else 'completed'
if outcome == 'completed':
    trajectory_path = output_path.parent / 'trajectory.json'
    trajectory_value = {
        'r015_binding': {
            'round_id': round_id,
            'task_id': task_id,
            'trial_id': assignment['trial_id'],
            'session_id': session_id,
        },
        'source': {'canonical_instance_id': task_id},
        'controlled_fixture': True,
    }
    trajectory_path.write_text(json.dumps(trajectory_value, sort_keys=True), encoding='utf-8')
    trajectory = {
        'round_id': round_id,
        'task_id': task_id,
        'trial_id': assignment['trial_id'],
        'session_id': session_id,
        'complete': True,
        'path': str(trajectory_path),
        'sha256': hashlib.sha256(trajectory_path.read_bytes()).hexdigest(),
    }
raw = {
    'official_harbor_trial': True,
    'condition': 'C-only',
    'round_id': round_id,
    'task_id': task_id,
    'trial_id': assignment['trial_id'],
    'session_id': session_id,
    'historical_baseline_used': False,
    'controlled_fixture': True,
}
if outcome == 'infra_failure':
    raw['official_failure'] = {'exception_type': 'ControlledSetupFailure', 'message': 'fixture-only official boundary'}
trial = {
    'condition': 'C-only',
    'round_id': round_id,
    'task_id': task_id,
    'trial_id': assignment['trial_id'],
    'outcome': outcome,
    'trajectory': trajectory,
    'supplied_skills': [],
    'raw_evidence': raw,
}
extraction = {
    'condition': 'C-only',
    'round_id': round_id,
    'task_id': task_id,
    'decision': 'skip',
    'reason': 'controlled integration fixture; no extraction candidate',
    'candidates': [],
    'trajectory_ref': trajectory,
    'description_records': [],
    'evidence': {'kind': 'controlled-production-boundary-fixture', 'historical_baseline_used': False},
}
publication = {
    'condition': 'C-only',
    'round_id': round_id,
    'task_id': task_id,
    'operations': [],
    'manager_decisions': [],
    'evidence': {'kind': 'controlled-production-boundary-fixture', 'historical_baseline_used': False},
}
output = {
    'schema_version': 1,
    'kind': 'r015_c_only_trial_driver_output',
    'condition': 'C-only',
    'round_id': round_id,
    'task_id': task_id,
    'trial': trial,
    'event_attempts': [],
    'extraction': extraction,
    'publication': publication,
    'official_process': {'classification': 'controlled_fixture', 'returncode': 0},
    'historical_baseline_used': False,
}
output_path.write_text(json.dumps(output, sort_keys=True), encoding='utf-8')
""",
            encoding="utf-8",
        )
        _run_driver(protocol, fake_driver, run_dir, state, allow_test_fixture=True)
        invocations = invocation_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(invocations), 24)
        self.assertEqual(protocol.state["formal_campaign"], "complete")
        self.assertEqual(protocol.current_round_id, 2)
        for round_id in (1, 2):
            round_state = protocol.state["rounds"][str(round_id)]
            self.assertEqual(round_state["status"], "complete")
            self.assertEqual(len(round_state["trajectory_pool"]), 11)
            self.assertEqual(len(round_state["description_pool"]), 0)
            self.assertTrue(all(item["round_id"] == round_id for item in round_state["trajectory_pool"]))
        restored = COnlyProtocol.load(state, config, BASELINE)
        _run_driver(restored, fake_driver, run_dir, state, allow_test_fixture=True)
        self.assertEqual(len(invocation_log.read_text(encoding="utf-8").splitlines()), 24)
        self.assertEqual(COnlyProtocol.load(state, config, BASELINE).state["formal_campaign"], "complete")






    def test_failed_driver_does_not_apply_a_pre_failure_stage_or_advance(self) -> None:
        """A manager/orchestration failure remains a reconciliation boundary."""
        tmp, config, state, run_dir = self._prepared_fixture()
        self.addCleanup(tmp.cleanup)
        protocol = COnlyProtocol.load(state, config, BASELINE)
        protocol.authorize_start()
        task_id = protocol.current_task_id
        assignment = protocol.freeze_task(task_id)
        protocol.save(state)
        input_path = _write_driver_input(protocol, assignment, run_dir, state)
        process_path = _driver_process_path(input_path)
        write_json(
            process_path,
            {
                "schema_version": 1,
                "kind": "r015_c_only_driver_process",
                "status": "failed",
                "condition": "C-only",
                "round_id": assignment["round_id"],
                "task_id": task_id,
                "trial_id": assignment["trial_id"],
                "returncode": 1,
                "timed_out": False,
                "error": "manager request failed after trial phase",
            },
        )
        stage_path = _driver_stage_path(input_path, "trial")
        write_json(
            stage_path,
            {
                "schema_version": 1,
                "kind": "r015_c_only_driver_stage",
                "status": "complete",
                "phase": "trial",
                "condition": "C-only",
                "round_id": assignment["round_id"],
                "task_id": task_id,
                "trial_id": assignment["trial_id"],
                "input_path": str(input_path),
                "input_sha256": sha256_file(input_path),
                "payload_sha256": "0" * 64,
                "payload": {},
            },
        )
        with self.assertRaisesRegex(COnlyProtocolError, "failed; durable phases require reconciliation"):
            _recover_driver_stages(
                protocol,
                assignment=assignment,
                input_path=input_path,
                state_path=state,
            )
        restored = COnlyProtocol.load(state, config, BASELINE)
        self.assertEqual(restored.current_task_id, task_id)
        self.assertEqual(restored._assignment(task_id)["status"], "pending")
        self.assertTrue((input_path.parent / "reconcile-required.json").is_file())

    def test_harbor_nonzero_without_official_result_stops_for_reconciliation(self) -> None:
        """A nonzero CLI with no result schema cannot masquerade as task infra."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = {
                "trial_id": "r1:C:build-pmars",
                "task_id": "build-pmars",
                "session_id": "session-build-pmars",
                "artifact_root": root,
                "sidecar_dir": root / "sidecar",
                "jobs_dir": root / "harbor" / "jobs",
                "task_name": "terminal-bench/build-pmars",
                "task_metadata": {},
                "job_config": root / "harbor" / "job.json",
                "sidecar_config": root / "sidecar.json",
                "host_config": root / "openclaw-host.json",
                "database": root / "openclaw-agent.sqlite",
            }
            process = {
                "classification": "harbor_nonzero",
                "official_trial_boundary_started": True,
                "harbor_returncode": 1,
            }
            with patch("scripts.run_r015_c_only_harbor_driver._find_harbor_trial", return_value=None):
                with self.assertRaisesRegex(COnlyHarborDriverError, "without an importable result directory"):
                    _import_official_evidence(context, process)
            failure = json.loads((root / "import-failure.json").read_text(encoding="utf-8"))
            self.assertEqual(failure["process"]["classification"], "harbor_nonzero")
            self.assertEqual(failure["trial_id"], "r1:C:build-pmars")

    def test_structured_harbor_task_id_failure_binds_result_identity_and_digest(self) -> None:
        """The Harbor 0.17 result schema uses task_id and keeps identity anchors."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs_dir = root / "harbor" / "jobs"
            trial_dir = jobs_dir / "polyglot-c-py__setup-timeout"
            trial_dir.mkdir(parents=True)
            expected_digest = "sha256:" + "c" * 64
            write_json(
                trial_dir / "config.json",
                {
                    "trial_name": trial_dir.name,
                    "task": {"name": "terminal-bench/polyglot-c-py", "ref": expected_digest},
                    "agent": {"kwargs": {"session_id": "r015-session", "trial_id": "r1:C:polyglot-c-py"}},
                },
            )
            write_json(
                trial_dir / "result.json",
                {
                    "id": "result-setup-timeout",
                    "trial_uri": "harbor://r1:C:polyglot-c-py",
                    "task_id": {"org": "terminal-bench", "name": "polyglot-c-py", "ref": expected_digest},
                    "task_name": "terminal-bench/polyglot-c-py",
                    "task_checksum": "d" * 64,
                    "trial_name": trial_dir.name,
                    "agent_result": None,
                    "verifier_result": None,
                    "exception_info": {
                        "exception_type": "AgentSetupTimeoutError",
                        "exception_message": "Agent setup timed out after 360.0 seconds",
                    },
                },
            )
            (trial_dir / "exception.txt").write_text("Agent setup timed out after 360.0 seconds\n", encoding="utf-8")
            context = {
                "trial_id": "r1:C:polyglot-c-py",
                "task_id": "polyglot-c-py",
                "session_id": "r015-session",
                "artifact_root": root / "artifact",
                "sidecar_dir": root / "sidecar",
                "jobs_dir": jobs_dir,
                "task_name": "terminal-bench/polyglot-c-py",
                "task_metadata": {"expected_dataset_task_digest": expected_digest},
                "job_config": root / "job.json",
                "sidecar_config": root / "sidecar.json",
                "host_config": root / "openclaw-host.json",
                "database": root / "openclaw-agent.sqlite",
            }
            context["artifact_root"].mkdir()
            outcome = _import_official_evidence(
                context,
                {
                    "kind": "r015_c_only_official_process",
                    "classification": "harbor_nonzero",
                    "official_trial_boundary_started": True,
                    "harbor_returncode": 1,
                },
            )
            self.assertEqual(outcome["outcome"], "infra_failure")
            failure = outcome["official_failure"]
            self.assertEqual(failure["result_id"], "result-setup-timeout")
            self.assertEqual(failure["result_trial_uri"], "harbor://r1:C:polyglot-c-py")
            self.assertEqual(failure["result_task_identity"]["ref"], expected_digest)
            self.assertEqual(failure["task_ref"], expected_digest)

    def test_production_driver_response_refs_bind_to_the_current_manager_call(self) -> None:
        """A valid hash alone cannot relabel another run's manager response."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager_root = root / "manager"
            call_dir = manager_root / "model_calls" / "call-0001"
            call_dir.mkdir(parents=True)
            response = call_dir / "response.json"
            write_json(response, {"kind": "live_manager_call", "purpose": "r015_c_only_fig9:test"})
            ref = {"call_id": "call-0001", "path": str(response), "sha256": sha256_file(response)}
            _validate_driver_payload_refs(
                {"manager_call_id": "call-0001", "response": ref},
                field="driver-output",
                strict_manager=True,
                manager_root=manager_root,
            )
            with self.assertRaisesRegex(COnlyProtocolError, "no call id binding"):
                _validate_driver_payload_refs(
                    {"manager_response_path": str(response), "manager_response_sha256": ref["sha256"]},
                    field="driver-output",
                    strict_manager=True,
                    manager_root=manager_root,
                )
            mismatched = dict(ref)
            mismatched["call_id"] = "call-0002"
            with self.assertRaisesRegex(COnlyProtocolError, "call_id differs"):
                _validate_driver_payload_refs(
                    {"manager_call_id": "call-0001", "response": mismatched},
                    field="driver-output",
                    strict_manager=True,
                    manager_root=manager_root,
                )
            outside = root / "other-run" / "model_calls" / "call-0003" / "response.json"
            outside.parent.mkdir(parents=True)
            write_json(outside, {"kind": "live_manager_call", "purpose": "other"})
            with self.assertRaisesRegex(COnlyProtocolError, "outside the current run manager root"):
                _validate_driver_payload_refs(
                    {"manager_call_id": "call-0003", "response": {"call_id": "call-0003", "path": str(outside), "sha256": sha256_file(outside)}},
                    field="driver-output",
                    strict_manager=True,
                    manager_root=manager_root,
                )

    def test_publication_uses_an_immutable_per_trial_ledger_snapshot(self) -> None:
        """Later calls cannot mutate the ledger referenced by an old output."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "official-harbor"
            artifact_root.mkdir()
            source = root / "manager-ledger.json"
            write_json(source, {"calls": [{"call_id": "call-0001"}]})
            reference = _snapshot_manager_ledger(
                {"artifact_root": artifact_root},
                {"manager_ledger": source, "manager_ledger_before_calls": 0},
            )
            original_hash = reference["sha256"]
            write_json(source, {"calls": [{"call_id": "call-0001"}, {"call_id": "call-0002"}]})
            self.assertEqual(reference["sha256"], original_hash)
            self.assertEqual(sha256_file(Path(reference["path"])), original_hash)
            _validate_driver_payload_refs(reference, field="publication.manager_ledger")

    def test_structured_harbor_setup_failure_becomes_official_infra_without_trajectory(self) -> None:
        """A real Harbor setup result uses the infra packet, not the success importer."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs_dir = root / "harbor" / "jobs"
            trial_dir = jobs_dir / "polyglot-c-py__setup-timeout"
            trial_dir.mkdir(parents=True)
            expected_digest = "sha256:" + "a" * 64
            write_json(
                trial_dir / "config.json",
                {
                    "trial_name": trial_dir.name,
                    "task": {"name": "terminal-bench/polyglot-c-py", "ref": expected_digest},
                    "agent": {"kwargs": {"session_id": "r015-session"}},
                },
            )
            write_json(
                trial_dir / "result.json",
                {
                    "task": {"name": "terminal-bench/polyglot-c-py", "ref": expected_digest},
                    "task_checksum": "b" * 64,
                    "trial_name": trial_dir.name,
                    "agent_result": None,
                    "verifier_result": None,
                    "exception_info": {
                        "exception_type": "AgentSetupTimeoutError",
                        "exception_message": "Agent setup timed out after 360.0 seconds",
                    },
                },
            )
            (trial_dir / "exception.txt").write_text("Agent setup timed out after 360.0 seconds\n", encoding="utf-8")
            context = {
                "trial_id": "r1:C:polyglot-c-py",
                "task_id": "polyglot-c-py",
                "session_id": "r015-session",
                "artifact_root": root / "artifact",
                "sidecar_dir": root / "sidecar",
                "jobs_dir": jobs_dir,
                "task_name": "terminal-bench/polyglot-c-py",
                "task_metadata": {"expected_dataset_task_digest": expected_digest},
                "job_config": root / "job.json",
                "sidecar_config": root / "sidecar.json",
                "host_config": root / "openclaw-host.json",
                "database": root / "openclaw-agent.sqlite",
            }
            context["artifact_root"].mkdir()
            outcome = _import_official_evidence(
                context,
                {
                    "kind": "r015_c_only_official_process",
                    "classification": "harbor_nonzero",
                    "official_trial_boundary_started": True,
                    "harbor_returncode": 1,
                },
            )
            self.assertEqual(outcome["outcome"], "infra_failure")
            self.assertIsNone(outcome["trajectory"])
            self.assertEqual(outcome["official_failure"]["exception_type"], "AgentSetupTimeoutError")
            packet = json.loads((context["artifact_root"] / "official-infra-result.json").read_text(encoding="utf-8"))
            self.assertEqual(packet["raw_evidence"]["official_failure"]["task_ref"], expected_digest)
            self.assertEqual(packet["raw_evidence"]["session_binding"]["status"], "expected_assignment_only")

    def test_reward_file_does_not_hide_official_agent_exception(self) -> None:
        """The raw exception is retained without excluding a complete trace."""
        failure = {
            "exception_info": {
                "exception_type": "NonZeroAgentExitCodeError",
                "exception_message": "agent exited with status 1",
            },
            "agent_result": {"n_input_tokens": 12},
            "verifier_result": {"rewards": {"reward": 0.0}},
        }
        observed = _result_exception_info(failure)
        self.assertEqual(observed["field"], "exception_info")
        self.assertEqual(observed["exception_type"], "NonZeroAgentExitCodeError")
        self.assertEqual(observed["message"], "agent exited with status 1")
        self.assertIsNone(_result_exception_info({"exception_info": None}))
        with self.assertRaisesRegex(COnlyHarborDriverError, "incomplete exception"):
            _result_exception_info({"exception_info": {"exception_type": "NonZeroAgentExitCodeError"}})

    def test_unproven_nonzero_result_does_not_become_infrastructure_failure(self) -> None:
        """A task-level agent error without a reward cannot use the setup packet."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_path = root / "build-pmars"
            task_path.mkdir()
            trial_dir = root / "jobs" / "build-pmars__agent-failure"
            trial_dir.mkdir(parents=True)
            write_json(
                trial_dir / "config.json",
                {
                    "trial_name": trial_dir.name,
                    "task": {"name": None, "path": str(task_path), "ref": None},
                },
            )
            write_json(
                trial_dir / "result.json",
                {
                    "task_id": {"path": str(task_path)},
                    "task_name": "terminal-bench/build-pmars",
                    "trial_name": trial_dir.name,
                    "agent_result": {"status": "failed", "error": "provider malformed tool arguments"},
                    "verifier_result": None,
                    "exception_info": {
                        "exception_type": "NonZeroAgentExitCodeError",
                        "exception_message": "agent exited with status 1",
                    },
                },
            )
            context = {
                "trial_id": "r1:C:build-pmars",
                "task_id": "build-pmars",
                "session_id": "r015-session",
                "artifact_root": root / "artifact",
                "sidecar_dir": root / "sidecar",
                "jobs_dir": trial_dir.parent,
                "task_name": "terminal-bench/build-pmars",
                "task_metadata": {"task_name": "terminal-bench/build-pmars", "task_path": str(task_path)},
                "job_value": {"tasks": [{"name": None, "path": str(task_path), "ref": None}]},
                "job_config": root / "job.json",
                "sidecar_config": root / "sidecar.json",
                "host_config": root / "openclaw-host.json",
                "database": root / "openclaw-agent.sqlite",
            }
            with self.assertRaisesRegex(COnlyHarborDriverError, "without a proven pre-agent setup boundary"):
                _official_failure_packet(
                    context=context,
                    process={"classification": "harbor_nonzero", "official_trial_boundary_started": True},
                    trial_dir=trial_dir,
                )

    def test_importable_nonzero_agent_result_remains_current_round_trajectory(self) -> None:
        """A solver/tool failure with reward and a complete session stays eligible."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trial_dir = root / "harbor" / "jobs" / "build-pmars__agent-failure"
            (trial_dir / "agent").mkdir(parents=True)
            (trial_dir / "verifier").mkdir()
            session_id = "session-build-pmars"
            expected_digest = "sha256:" + "e" * 64
            write_json(
                trial_dir / "config.json",
                {
                    "trial_name": trial_dir.name,
                    "task": {"name": "terminal-bench/build-pmars", "ref": expected_digest},
                    "agents": [{"kwargs": {"session_id": session_id}}],
                },
            )
            (trial_dir / "agent" / "instruction.txt").write_text("Repair the controlled task.\n", encoding="utf-8")
            events = [
                {"type": "session", "id": session_id},
                {"type": "message", "id": "u", "parentId": None, "message": {"role": "user", "content": "Repair it."}},
                {"type": "message", "id": "a", "parentId": "u", "message": {"role": "assistant", "content": "The tool failed, but the trace is complete.", "stopReason": "error"}},
            ]
            (trial_dir / "agent" / "openclaw.session.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
            )
            write_json(
                trial_dir / "result.json",
                {
                    "status": "completed",
                    "task_name": "terminal-bench/build-pmars",
                    # Harbor's public PackageTaskId includes its organization
                    # when the result is serialized.  Keep this fixture bound
                    # to the same identity as the config/context rather than
                    # relying on a short unqualified name.
                    "task_id": {"org": "terminal-bench", "name": "build-pmars", "ref": expected_digest},
                    "trial_name": trial_dir.name,
                    "agent_result": {"status": "failed", "error": "malformed tool arguments"},
                    "verifier_result": {"rewards": {"reward": 0.0}},
                    "exception_info": {
                        "exception_type": "NonZeroAgentExitCodeError",
                        "exception_message": "provider returned a malformed tool call",
                    },
                },
            )
            (trial_dir / "verifier" / "reward.txt").write_text("0\n", encoding="utf-8")
            sidecar_dir = root / "sidecar"
            (sidecar_dir / "upstream_requests").mkdir(parents=True)
            (sidecar_dir / "overlay-state.json").write_text("{}\n", encoding="utf-8")
            write_json(
                sidecar_dir / "upstream_requests" / "attempt-0001.json",
                {
                    "trial_id": "r1:C:build-pmars",
                    "attempt_ordinal": 1,
                    "state_path": str((sidecar_dir / "overlay-state.json").resolve()),
                    "proxy_outcome": "stream_forwarded",
                    "normal_call_boundary": {
                        "kind": "r012_public_plugin_normal_call_boundary",
                        "trial_id": "r1:C:build-pmars",
                        "session_id": session_id,
                    },
                },
            )
            artifact_root = root / "artifact"
            artifact_root.mkdir()
            context = {
                "trial_id": "r1:C:build-pmars",
                "task_id": "build-pmars",
                "session_id": session_id,
                "artifact_root": artifact_root,
                "sidecar_dir": sidecar_dir,
                "jobs_dir": trial_dir.parent,
                "task_name": "terminal-bench/build-pmars",
                "task_metadata": {"task_name": "terminal-bench/build-pmars", "expected_dataset_task_digest": expected_digest},
                "job_config": root / "job.json",
                "sidecar_config": root / "sidecar.json",
                "host_config": root / "openclaw-host.json",
                "database": root / "openclaw-agent.sqlite",
            }
            for path in (context["job_config"], context["sidecar_config"], context["host_config"], context["database"]):
                Path(path).write_text("{}\n", encoding="utf-8")
            imported = _import_official_evidence(
                context,
                {
                    "kind": "r015_c_only_official_process",
                    "classification": "harbor_nonzero",
                    "official_trial_boundary_started": True,
                    "harbor_returncode": 1,
                },
            )
            self.assertEqual(imported["outcome"], "completed")
            self.assertIsNotNone(imported["trajectory"])
            evidence = imported["raw_evidence"]
            self.assertEqual(evidence["classification"], "agent_failure")
            self.assertEqual(evidence["official_exception"]["exception_type"], "NonZeroAgentExitCodeError")
            self.assertEqual(evidence["trajectory_eligibility"]["status"], "eligible_current_round_trace")
            self.assertTrue(evidence["trajectory_eligibility"]["manager_phases_allowed"])

    def test_actual_harbor_local_taskconfig_shape_binds_path_for_success_and_failure(self) -> None:
        """Harbor 0.17.x LocalTaskId has no package name/ref to compare."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_path = root / "build-pmars"
            task_path.mkdir()
            trial_dir = root / "jobs" / "build-pmars__attempt-1"
            trial_dir.mkdir(parents=True)
            local_task = {"download_dir": None, "git_commit_id": None, "git_url": None, "name": None, "overwrite": False, "path": str(task_path), "ref": None, "source": None}
            write_json(
                trial_dir / "config.json",
                {"trial_name": trial_dir.name, "task": local_task, "agent": {"kwargs": {"session_id": "r015-session"}}},
            )
            result = {
                "id": "result-local-task",
                "trial_uri": "harbor://r1:C:build-pmars",
                "task_id": {"path": str(task_path)},
                "task_name": "terminal-bench/build-pmars",
                "task_checksum": "b" * 64,
                "trial_name": trial_dir.name,
                "agent_result": None,
                "verifier_result": None,
            }
            write_json(trial_dir / "result.json", result)
            context = {
                "trial_id": "r1:C:build-pmars",
                "task_id": "build-pmars",
                "session_id": "r015-session",
                "artifact_root": root / "artifact",
                "sidecar_dir": root / "sidecar",
                "jobs_dir": root / "jobs",
                "task_name": "terminal-bench/build-pmars",
                "task_metadata": {
                    "task_name": "terminal-bench/build-pmars",
                    "task_path": str(task_path),
                    "expected_dataset_task_digest": "sha256:" + "d" * 64,
                },
                "job_value": {"tasks": [local_task]},
                "job_config": root / "job.json",
                "sidecar_config": root / "sidecar.json",
                "host_config": root / "openclaw-host.json",
                "database": root / "openclaw-agent.sqlite",
            }
            for artifact in (context["job_config"], context["sidecar_config"], context["host_config"], context["database"]):
                Path(artifact).write_text("{}", encoding="utf-8")
            # A normal local TrialResult must reach the success importer path
            # (the helper returns None because there is no exception_info).
            self.assertIsNone(
                _official_failure_packet(
                    context=context,
                    process={"classification": "completed", "official_trial_boundary_started": True},
                    trial_dir=trial_dir,
                )
            )
            result["exception_info"] = {
                "exception_type": "AgentSetupTimeoutError",
                "exception_message": "Agent setup timed out",
            }
            write_json(trial_dir / "result.json", result)
            packet = _official_failure_packet(
                context=context,
                process={"classification": "harbor_nonzero", "official_trial_boundary_started": True},
                trial_dir=trial_dir,
            )
            self.assertIsNotNone(packet)
            assert packet is not None
            self.assertEqual(packet["official_failure"]["launch_identity"]["mode"], "local")
            self.assertEqual(packet["official_failure"]["launch_identity"]["local_task_path"], str(task_path.resolve()))
            self.assertIsNone(packet["official_failure"]["task_ref"])
            # Exercise the production importer boundary as well as the helper:
            # a genuine local Harbor setup result is routed to the structured
            # infra packet before the success-only session importer.
            context["jobs_dir"] = root / "jobs"
            context["artifact_root"].mkdir()
            imported = _import_official_evidence(
                context,
                {"classification": "harbor_nonzero", "official_trial_boundary_started": True},
            )
            self.assertEqual(imported["outcome"], "infra_failure")
            self.assertEqual(imported["official_failure"]["launch_identity"]["mode"], "local")
            self.assertTrue((context["artifact_root"] / "official-infra-result.json").is_file())

    def test_executable_resolution_preserves_virtualenv_symlink(self) -> None:
        """Do not resolve a venv interpreter to its system target."""
        if os.name == "nt":
            self.skipTest("Windows test environments do not guarantee symlink creation")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "system-python"
            target.write_text("#!/bin/sh\n", encoding="utf-8")
            link = root / "venv-python"
            link.symlink_to(target)
            resolved = _resolve_executable_path(str(link), base=root, field="python_executable")
            self.assertEqual(resolved, link.absolute())
            self.assertTrue(resolved.is_symlink())


if __name__ == "__main__":
    unittest.main()
