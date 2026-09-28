from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codeskill_rebuild.bank import SkillBank
from codeskill_rebuild.r012_execution import profile_sha256
from codeskill_rebuild.trial_schedule import InstanceBankFreeze
from codeskill_rebuild.types import read_json, sha256_file, write_json


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from run_m3_r015_development import (  # noqa: E402
    DevelopmentLauncherError,
    _build_trial_context,
    _extract_arm_a_candidates,
    _find_harbor_trial,
    _release_instance,
    _sidecar_check,
    _trace_for_a,
    _validate_profile,
)
from run_openclaw_r012_sidecar import load_config  # noqa: E402


class M3R015DevelopmentLauncherTest(unittest.TestCase):
    def _profile(self) -> dict[str, object]:
        value = json.loads((ROOT / "configs" / "m3-r015-development.json").read_text(encoding="utf-8"))
        return _validate_profile(value, minilm_revision=None)

    def _coordinator(self, instance_id: str = "terminal-bench/password-recovery") -> InstanceBankFreeze:
        coordinator = InstanceBankFreeze(
            {
                "A": SkillBank.empty("terminal-bench"),
                "B": SkillBank.empty("terminal-bench"),
                "C": SkillBank.empty("terminal-bench"),
            },
            repeat_ids=("development",),
        )
        coordinator.freeze(instance_id)
        return coordinator

    def test_trial_context_is_accepted_by_the_real_sidecar_config_validator(self) -> None:
        with self.subTest("sidecar binding"):
            from tempfile import TemporaryDirectory

            with TemporaryDirectory() as raw:
                run_dir = Path(raw)
                profile = self._profile()
                coordinator = self._coordinator()
                state_path = run_dir / "lifecycle.json"
                state = {
                    "kind": "r012_instance_lifecycle_state",
                    "profile": profile,
                    "profile_sha256": profile_sha256(profile),
                    "coordinator": coordinator.to_dict(),
                }
                write_json(state_path, state)
                task_path = run_dir / "task"
                task_path.mkdir()
                (task_path / "task.toml").write_text("[task]\n", encoding="utf-8")
                args = SimpleNamespace(
                    run_dir=run_dir,
                    sidecar_port_base=18600,
                    sidecar_advertised_host="172.17.0.1",
                    sidecar_listen_host="0.0.0.0",
                    plugin_path=ROOT / "openclaw_plugin",
                )
                assignment = coordinator._assignment("password-recovery:B:development")
                context = _build_trial_context(
                    args=args,
                    profile=profile,
                    profile_hash=profile_sha256(profile),
                    state_path=state_path,
                    state_hash=sha256_file(state_path),
                    assignment=assignment,
                    service={"base_url": "http://127.0.0.1:31000/v1", "model_id": "fixture-model"},
                    task_path=task_path,
                    ordinal=1,
                )
                validated = load_config(context["sidecar_config_path"])
                self.assertEqual(validated["trialId"], "password-recovery:B:development")
                job = read_json(context["job_config_path"])
                self.assertEqual(job["environment"]["env"]["OPENCLAW_STATE_DIR"], "/var/lib/codeskill/openclaw-state")
                self.assertEqual(job["agents"][0]["kwargs"]["session_id"], context["session_id"])
                self.assertEqual(job["agents"][0]["kwargs"]["thinking"], "off")
                mounts = job["environment"]["mounts"]
                self.assertEqual(mounts[0]["target"], "/opt/codeskill/openclaw-sidecar-src")
                self.assertTrue(mounts[0]["read_only"])
                self.assertEqual(job["agents"][0]["kwargs"]["plugin_path"], "/opt/codeskill/openclaw-sidecar")
                self.assertTrue((context["openclaw_state_host"] / "agents" / "main" / "agent").is_dir())
                seeded_database = context["openclaw_state_host"] / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
                self.assertTrue(seeded_database.is_file())
                self.assertEqual(seeded_database.stat().st_size, 0)
                intent = read_json(context["trial_root"] / "launch-intent.json")
                self.assertEqual(intent["native_database_seed"]["size_bytes"], 0)

    def test_sidecar_check_records_spawn_failure_without_losing_the_trial_boundary(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw:
            root = Path(raw)
            args = SimpleNamespace(sidecar_script=root / "missing-sidecar.py")
            context = {"trial_root": root, "sidecar_config_path": root / "sidecar.json"}
            with patch("run_m3_r015_development.subprocess.run", side_effect=FileNotFoundError("missing")):
                ok, evidence = _sidecar_check(args, context, root)
            self.assertFalse(ok)
            self.assertEqual(evidence["error_type"], "FileNotFoundError")
            self.assertEqual(read_json(root / "sidecar-check.json")["return_code"], None)

    def test_harbor_trial_finder_accepts_local_task_path_when_name_is_null(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw:
            root = Path(raw)
            jobs = root / "jobs"
            trial = jobs / "job" / "trial"
            trial.mkdir(parents=True)
            write_json(trial / "config.json", {"task": {"name": None, "path": "/tasks/password-recovery"}})
            write_json(trial / "result.json", {"task_name": "terminal-bench/password-recovery"})
            self.assertEqual(_find_harbor_trial(jobs, "terminal-bench/password-recovery"), trial)

    def test_arm_a_trace_requires_the_immutable_packet_reference(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw:
            root = Path(raw)
            trace_path = root / "trajectory-evidence.json"
            trace_path.write_text("{}\n", encoding="utf-8")
            coordinator = self._coordinator()
            evidence = {
                "classification": "official_harbor_trial",
                "trajectory_evidence": {"source": {"canonical_instance_id": "terminal-bench/password-recovery"}},
                "packet_manifest": {
                    "trajectory_evidence": {"path": str(trace_path), "sha256": sha256_file(trace_path)}
                },
            }
            coordinator.finish("password-recovery:A:development", result_evidence=evidence)
            self.assertEqual(_trace_for_a(coordinator, "terminal-bench/password-recovery"), trace_path)
            evidence.pop("packet_manifest")
            coordinator = self._coordinator()
            coordinator.finish("password-recovery:A:development", result_evidence=evidence)
            with self.assertRaises(DevelopmentLauncherError):
                _trace_for_a(coordinator, "terminal-bench/password-recovery")

    def test_arm_a_extraction_lets_the_child_create_its_run_directory(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw:
            root = Path(raw)
            instance_root = root / "instances" / "password-recovery"
            instance_root.mkdir(parents=True)
            trace_path = root / "trajectory-evidence.json"
            trace_path.write_text("{}\n", encoding="utf-8")
            profile = self._profile()
            extraction_dir = instance_root / "event-extraction"
            args = SimpleNamespace(
                run_dir=root,
                repo_root=root,
                event_runner=root / "event-runner.py",
                event_prompt=root / "event-prompt.md",
                evidence_compaction_prompt=root / "compaction-prompt.md",
                spec=root / "spec.md",
                decisions=root / "decisions.md",
                model_config=root / "model-config.json",
                ledger=root / "ledger.json",
                python=Path(sys.executable),
                extraction_timeout_seconds=30,
            )

            def fake_event_runner(command: list[str], **kwargs: object) -> SimpleNamespace:
                del kwargs
                self.assertFalse(extraction_dir.exists())
                child_run_dir = Path(command[command.index("--run-dir") + 1])
                child_run_dir.mkdir(parents=True)
                write_json(child_run_dir / "run-status.json", {"status": "completed", "schedules": []})
                return SimpleNamespace(returncode=0)

            with patch("run_m3_r015_development.subprocess.run", side_effect=fake_event_runner):
                candidates = _extract_arm_a_candidates(
                    args=args,
                    state={},
                    coordinator=self._coordinator(),
                    instance_id="password-recovery",
                    profile=profile,
                    profile_hash=profile_sha256(profile),
                    trace_ref=trace_path,
                )

            self.assertEqual(candidates, [])
            self.assertTrue((extraction_dir / "run-status.json").is_file())
            process = read_json(extraction_dir / "process.json")
            self.assertEqual(process["return_code"], 0)
            self.assertTrue((root / "instances" / "password-recovery" / "event-extraction-launch" / "process.stdout.log").is_file())
            self.assertEqual(read_json(instance_root / "arm-a-candidate-records.json")["status"], "completed")

    def test_release_does_not_publish_after_an_infrastructure_trial_failure(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw:
            root = Path(raw)
            coordinator = self._coordinator()
            for trial_id in coordinator.instances["password-recovery"]["assignments"]:
                coordinator.finish(
                    trial_id,
                    result_evidence={
                        "classification": "infra_failure",
                        "trial_id": trial_id,
                        "proxy_attempt_records": [],
                        "trajectory_evidence": None,
                        "infra_failure": {
                            "trial_id": trial_id,
                            "instance_id": "password-recovery",
                            "error_type": "FixtureFailure",
                            "error": "fixture failure",
                            "raw_evidence": {"path": "failure.json"},
                        },
                    },
                )
            with self.assertRaisesRegex(DevelopmentLauncherError, "cannot publish next-instance banks"):
                _release_instance(
                    args=SimpleNamespace(run_dir=root, state_path=root / "lifecycle.json"),
                    state={"kind": "fixture"},
                    coordinator=coordinator,
                    instance_id="password-recovery",
                    profile=self._profile(),
                    profile_hash=profile_sha256(self._profile()),
                    candidates=[],
                )
            self.assertFalse(coordinator.instances["password-recovery"]["released"])
            self.assertEqual(
                coordinator.instances["password-recovery"]["release_state"],
                "blocked_after_infrastructure_failure",
            )
            self.assertEqual(
                read_json(root / "lifecycle.json")["coordinator"]["instances"]["password-recovery"]["release_state"],
                "blocked_after_infrastructure_failure",
            )


if __name__ == "__main__":
    unittest.main()
