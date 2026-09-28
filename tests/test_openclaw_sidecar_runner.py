from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class OpenClawSidecarRunnerTest(unittest.TestCase):
    @staticmethod
    def runner_module(root: Path):
        spec = importlib.util.spec_from_file_location("codeskill_sidecar_runner_test", root / "scripts" / "run_openclaw_r012_sidecar.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def openclaw_config(directory: Path, *, port: int, primary: str = "codeskill-r012/controlled") -> tuple[Path, Path]:
        plugin = directory / "plugin"
        plugin.mkdir(parents=True)
        (plugin / "openclaw.plugin.json").write_text(
            json.dumps({"id": "codeskill-r012-sidecar", "providers": ["codeskill-r012"]}), encoding="utf-8"
        )
        path = directory / "openclaw.json"
        path.write_text(
            json.dumps(
                {
                    "models": {
                        "providers": {
                            "codeskill-r012": {
                                "baseUrl": f"http://127.0.0.1:{port}/v1",
                                "models": [{"id": "controlled"}],
                            }
                        }
                    },
                    "plugins": {
                        "allow": ["codeskill-r012-sidecar"],
                        "load": {"paths": [str(plugin)]},
                        "entries": {
                            "codeskill-r012-sidecar": {
                                "enabled": True,
                                "config": {
                                    "trialId": "trial-sidecar",
                                    "sessionId": "session-sidecar",
                                    "permitDirectory": str(directory / "permits"),
                                },
                            }
                        },
                    },
                    "agents": {"defaults": {"model": {"primary": primary}}},
                }
            ),
            encoding="utf-8",
        )
        return path, plugin

    @classmethod
    def fixture_config(cls, directory: Path, *, primary: str = "codeskill-r012/controlled") -> dict[str, object]:
        openclaw, plugin = cls.openclaw_config(directory, port=18080, primary=primary)
        return {
            "trialId": "trial-sidecar",
            "sessionId": "session-sidecar",
            "sessionMarker": "sqlite:main:session-sidecar:/tmp/sessions.json",
            "permitDirectory": str(directory / "permits"),
            "overlay": {
                "statePath": str(directory / "state.json"),
                "evidenceDirectory": str(directory / "evidence"),
                "maxInputTokens": 250000,
            },
            "tokenizer": {"baseUrl": "http://127.0.0.1:9", "timeoutSeconds": 1},
            "upstream": {"endpoint": "http://127.0.0.1:9/v1/chat/completions", "timeoutSeconds": 1},
            "listen": {"host": "127.0.0.1", "port": 18080},
            "openclaw": {
                "configPath": str(openclaw),
                "pluginPath": str(plugin),
                "providerId": "codeskill-r012",
                "modelId": "controlled",
            },
            "selection": {"mode": "fixture-test-only", "acknowledgement": "not-a-retrieval-or-lifecycle-run"},
        }

    def test_check_config_builds_an_isolated_sidecar_without_network_or_listener(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            config = self.fixture_config(directory)
            config_path = directory / "sidecar.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(root / "src")
            result = subprocess.run(
                [sys.executable, str(root / "scripts" / "run_openclaw_r012_sidecar.py"), "--config", str(config_path), "--check-config"],
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"status": "valid", "trial_id": "trial-sidecar", "session_id": "session-sidecar", "selection_mode": "fixture-test-only"},
        )

    def test_build_service_accepts_a_complete_fixture_binding_without_contacting_network(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            config = self.fixture_config(Path(tmp))
            service = self.runner_module(root).build_service(config)
        self.assertEqual(service.overlay.trial_id, "trial-sidecar")
        self.assertIsNone(service.overlay.task_selector)
        self.assertIsNone(service.overlay.event_selector)

    def test_check_config_rejects_an_openclaw_model_that_bypasses_the_sidecar_provider(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            config_path = directory / "sidecar.json"
            config_path.write_text(json.dumps(self.fixture_config(directory, primary="other/controlled")), encoding="utf-8")
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(root / "src")
            result = subprocess.run(
                [sys.executable, str(root / "scripts" / "run_openclaw_r012_sidecar.py"), "--config", str(config_path), "--check-config"],
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("agents.defaults.model.primary", result.stderr)

    def test_check_config_rejects_a_non_codeskill_provider_even_when_its_url_matches(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            config = self.fixture_config(directory)
            config["openclaw"]["providerId"] = "other-provider"  # type: ignore[index]
            config_path = directory / "sidecar.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(root / "src")
            result = subprocess.run([sys.executable, str(root / "scripts" / "run_openclaw_r012_sidecar.py"), "--config", str(config_path), "--check-config"], text=True, capture_output=True, env=environment, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("providerId must be the public codeskill-r012", result.stderr)

    def test_check_config_rejects_disabled_plugin_and_mismatched_plugin_session_or_permit(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(root / "src")
            for mutation, expected in (
                (lambda value: value["plugins"]["entries"]["codeskill-r012-sidecar"].update({"enabled": False}), "plugin entry must be enabled"),
                (lambda value: value["plugins"]["load"].update({"paths": []}), "plugins.load.paths must load"),
                (lambda value: value["plugins"]["entries"]["codeskill-r012-sidecar"]["config"].update({"sessionId": "other-session"}), "plugin sessionId must equal"),
                (lambda value: value["plugins"]["entries"]["codeskill-r012-sidecar"]["config"].update({"permitDirectory": str(directory / "other-permits")}), "permitDirectory must equal"),
            ):
                config = self.fixture_config(directory / expected.replace(" ", "-"))
                openclaw = Path(config["openclaw"]["configPath"])  # type: ignore[index]
                value = json.loads(openclaw.read_text(encoding="utf-8"))
                mutation(value)
                openclaw.write_text(json.dumps(value), encoding="utf-8")
                config_path = openclaw.parent / "sidecar.json"
                config_path.write_text(json.dumps(config), encoding="utf-8")
                result = subprocess.run([sys.executable, str(root / "scripts" / "run_openclaw_r012_sidecar.py"), "--config", str(config_path), "--check-config"], text=True, capture_output=True, env=environment, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)

    def test_check_config_accepts_a_distinct_container_advertised_host(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            config = self.fixture_config(directory)
            config["listen"] = {"host": "0.0.0.0", "advertisedHost": "172.17.0.1", "port": 18080}
            openclaw = Path(config["openclaw"]["configPath"])  # type: ignore[index]
            value = json.loads(openclaw.read_text(encoding="utf-8"))
            value["models"]["providers"]["codeskill-r012"]["baseUrl"] = "http://172.17.0.1:18080/v1"
            openclaw.write_text(json.dumps(value), encoding="utf-8")
            config_path = directory / "sidecar.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(root / "src")
            result = subprocess.run(
                [sys.executable, str(root / "scripts" / "run_openclaw_r012_sidecar.py"), "--config", str(config_path), "--check-config"],
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
