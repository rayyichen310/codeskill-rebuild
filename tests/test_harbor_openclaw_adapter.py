from __future__ import annotations

import inspect
import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from codeskill_rebuild.harbor_openclaw_adapter import CODESKILLHarborOpenClaw


class HarborOpenClawAdapterTest(unittest.TestCase):
    def _adapter_without_harbor(self) -> CODESKILLHarborOpenClaw:
        # The binding renderer is intentionally independent of Harbor so the
        # repository can verify public-plugin invariants without installing a
        # task container runtime.
        adapter = object.__new__(CODESKILLHarborOpenClaw)
        adapter._prompt_template_path = None
        adapter._codeskill_sidecar_base_url = "http://127.0.0.1:18080/v1"
        adapter._codeskill_sidecar_model_id = "fixture-model"
        adapter._codeskill_plugin_path = "/opt/codeskill/plugin"
        adapter._codeskill_permit_directory = "/state/permits/session"
        adapter._codeskill_plugin_audit_path = "/state/permits/session/audit.jsonl"
        adapter._codeskill_trial_id = "password-recovery:C:development"
        adapter._codeskill_session_id = "session-fixture"
        adapter._codeskill_context_tokens = 524288
        adapter._codeskill_max_output_tokens = 8192
        adapter._codeskill_provider_timeout_seconds = None
        return adapter

    def test_public_plugin_sidecar_binding_uses_no_openclaw_source_mount(self) -> None:
        adapter = self._adapter_without_harbor()
        rendered = adapter._bind_public_plugin({"plugins": {"allow": ["other"], "load": {"paths": ["/other"]}}, "models": {}})
        self.assertEqual(rendered["agents"]["defaults"]["model"]["primary"], "codeskill-r012/fixture-model")
        self.assertNotIn("contextTokens", rendered["agents"]["defaults"])
        self.assertEqual(rendered["models"]["providers"]["codeskill-r012"]["baseUrl"], "http://127.0.0.1:18080/v1")
        self.assertEqual(rendered["models"]["providers"]["codeskill-r012"]["models"][0]["contextTokens"], 524288)
        self.assertIn("codeskill-r012-sidecar", rendered["plugins"]["allow"])
        self.assertIn("/opt/codeskill/plugin", rendered["plugins"]["load"]["paths"])
        entry = rendered["plugins"]["entries"]["codeskill-r012-sidecar"]
        self.assertTrue(entry["enabled"])
        self.assertEqual(entry["config"]["sessionId"], "session-fixture")
        self.assertEqual(adapter.codeskill_binding["openclaw_source"], "Harbor-installed official package")
        self.assertEqual(adapter.codeskill_binding["source_mount"], "forbidden")

    def test_baseline_reasoning_profile_is_rendered_through_public_config(self) -> None:
        adapter = self._adapter_without_harbor()
        adapter._codeskill_thinking = "high"
        adapter._codeskill_reasoning_effort = "max"
        adapter._codeskill_temperature = 1.0
        adapter._codeskill_top_p = 0.95
        adapter._codeskill_provider_timeout_seconds = 900
        rendered = adapter._bind_public_plugin({"plugins": {}, "models": {}})
        model = rendered["models"]["providers"]["codeskill-r012"]["models"][0]
        self.assertTrue(model["reasoning"])
        self.assertEqual(model["compat"]["thinkingFormat"], "deepseek")
        self.assertEqual(model["compat"]["supportedReasoningEfforts"], ["off", "low", "medium", "high"])
        self.assertEqual(rendered["agents"]["defaults"]["thinkingDefault"], "high")
        self.assertEqual(rendered["models"]["providers"]["codeskill-r012"]["timeoutSeconds"], 900)
        params = rendered["agents"]["defaults"]["models"]["codeskill-r012/fixture-model"]["params"]
        self.assertEqual(params, {"temperature": 1.0, "topP": 0.95, "extra_body": {"reasoning_effort": "max"}})
        self.assertEqual(adapter.codeskill_binding["reasoning_effort"], "max")

    def test_node_bootstrap_uses_supported_nvm_install_syntax(self) -> None:
        source = inspect.getsource(CODESKILLHarborOpenClaw.install)
        self.assertIn("nvm install {runtime}", source)
        self.assertNotIn("nvm install --delete-prefix", source)

    def test_headless_setup_command_acknowledges_risk_and_skips_external_services(self) -> None:
        command = CODESKILLHarborOpenClaw._noninteractive_setup_command()
        self.assertIn("openclaw setup --workspace . --non-interactive --accept-risk", command)
        for option in ("--skip-daemon", "--skip-health", "--skip-ui", "--skip-hooks"):
            self.assertIn(option, command)

    def test_uploaded_config_targets_the_selected_openclaw_state_directory(self) -> None:
        command = CODESKILLHarborOpenClaw._copy_upload_config_command()
        self.assertIn('"${OPENCLAW_STATE_DIR:-$HOME/.openclaw}"', command)
        self.assertIn('"$state_dir/openclaw.json"', command)

    def test_public_plugin_is_copied_from_read_only_mount_to_runtime_path(self) -> None:
        adapter = self._adapter_without_harbor()
        command = adapter._install_public_plugin_command()
        self.assertIn(CODESKILLHarborOpenClaw.PLUGIN_SOURCE_PATH, command)
        self.assertIn("cp -a", command)
        self.assertIn("chown -R 0:0", command)
        self.assertIn("chmod -R u=rwX,go=rX", command)
        self.assertEqual(
            adapter.codeskill_binding["plugin_source_path"],
            CODESKILLHarborOpenClaw.PLUGIN_SOURCE_PATH,
        )

    def test_agent_environment_uses_container_root_home_without_forwarding_secrets(self) -> None:
        rendered = CODESKILLHarborOpenClaw._agent_environment({"CODESKILL_FLAG": "present"})
        self.assertEqual(rendered["HOME"], "/root")
        self.assertEqual(rendered["NVM_DIR"], "/root/.nvm")
        self.assertEqual(rendered["CODESKILL_FLAG"], "present")

    def test_agent_preserves_container_default_user_and_workspace_ownership(self) -> None:
        self.assertIsNone(CODESKILLHarborOpenClaw.AGENT_USER)
        source = inspect.getsource(CODESKILLHarborOpenClaw)
        self.assertNotIn("chown -R 1000:1000", source)
        self.assertNotIn("git config --global", source)

    def test_version_probe_resolves_the_container_default_users_nvm_install(self) -> None:
        adapter = self._adapter_without_harbor()
        command = adapter.get_version_command()
        self.assertIn("HOME=/root", command)
        self.assertIn("NVM_DIR=/root/.nvm", command)
        self.assertIn('openclaw --version', command)

    def test_agent_operations_explicitly_select_the_container_default_user(self) -> None:
        source = inspect.getsource(CODESKILLHarborOpenClaw)
        self.assertGreaterEqual(source.count("with environment.with_default_user(self.AGENT_USER)"), 2)

    def test_rebuild_raw_session_uses_bound_session_when_stdout_has_trailing_diagnostics(self) -> None:
        adapter = self._adapter_without_harbor()
        with tempfile.TemporaryDirectory() as raw:
            logs = Path(raw)
            state = logs / "codeskill-openclaw-state"
            state.mkdir()
            db = state / "openclaw-agent.sqlite"
            connection = sqlite3.connect(db)
            try:
                connection.execute("create table transcript_events (session_id text, seq integer, event_json text)")
                connection.execute(
                    "insert into transcript_events values (?, ?, ?)",
                    ("session-fixture", 0, json.dumps({"type": "session", "id": "session-fixture"})),
                )
                connection.commit()
            finally:
                connection.close()
            adapter.logs_dir = logs
            with mock.patch("codeskill_rebuild.harbor_openclaw_adapter.sqlite3.connect", wraps=sqlite3.connect) as connect:
                adapter._rebuild_raw_session_jsonl()
            self.assertEqual(connect.call_args.kwargs, {"uri": True})
            self.assertTrue(connect.call_args.args[0].endswith("?mode=ro"))
            output = logs / "openclaw.session.jsonl"
            self.assertTrue(output.is_file())
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["id"], "session-fixture")

    def test_sqlite_export_gives_only_the_host_log_owner_access_to_the_copy(self) -> None:
        adapter = self._adapter_without_harbor()
        with tempfile.TemporaryDirectory() as raw:
            adapter.logs_dir = Path(raw)
            owner = adapter.logs_dir.stat()
            command = adapter._agent_state_export_command()
            owner_spec = f"{owner.st_uid}:{owner.st_gid}"
            self.assertIn(f'chown {owner_spec} "$dst/$name"', command)
            self.assertIn(f'chown {owner_spec} "$dst"', command)
            self.assertNotIn("chown -R", command)

    def test_solver_failure_still_runs_raw_session_export_and_preserves_original_exception(self) -> None:
        class Logger:
            def debug(self, *_args: object, **_kwargs: object) -> None:
                return

        class Environment:
            @contextmanager
            def with_default_user(self, _user: int):
                yield self

        async def exercise() -> tuple[BaseException, list[str], list[str]]:
            adapter = self._adapter_without_harbor()
            with tempfile.TemporaryDirectory() as raw:
                adapter.logs_dir = Path(raw)
                adapter.model_name = "codeskill-r012/fixture-model"
                adapter.logger = Logger()
                adapter._provider_env_keys = lambda _provider: ()
                adapter._validate_provider = lambda _provider: None
                adapter._get_env = lambda _key: None
                adapter._build_full_openclaw_config = lambda: {}
                adapter._build_register_skills_command = lambda: None
                adapter.build_cli_flags = lambda: ""
                copy_calls: list[str] = []
                commands: list[str] = []

                async def copy_session(_environment: object, _env: dict[str, str]) -> None:
                    copy_calls.append("copy")

                async def exec_as_agent(_environment: object, command: str, **_kwargs: object) -> None:
                    commands.append(command)
                    if "openclaw agent" in command:
                        raise TimeoutError("solver timeout")

                adapter._copy_openclaw_session_file_to_agent_logs = copy_session
                adapter.exec_as_agent = exec_as_agent
                adapter._rebuild_raw_session_jsonl = lambda: {"status": "written", "event_count": 1}
                try:
                    await adapter.run("repair", Environment(), None)
                except BaseException as error:
                    return error, commands, copy_calls
                raise AssertionError("solver should fail")

        error, commands, copy_calls = __import__("asyncio").run(exercise())
        self.assertIsInstance(error, TimeoutError)
        self.assertEqual(str(error), "solver timeout")
        self.assertTrue(copy_calls)
        self.assertTrue(any("openclaw-agent.sqlite" in command for command in commands))

    def test_export_failure_is_reported_after_success_without_replacing_solver_evidence(self) -> None:
        class Logger:
            def debug(self, *_args: object, **_kwargs: object) -> None:
                return

        class Environment:
            @contextmanager
            def with_default_user(self, _user: int):
                yield self

        async def exercise() -> tuple[BaseException, list[str]]:
            adapter = self._adapter_without_harbor()
            with tempfile.TemporaryDirectory() as raw:
                adapter.logs_dir = Path(raw)
                adapter.model_name = "codeskill-r012/fixture-model"
                adapter.logger = Logger()
                adapter._provider_env_keys = lambda _provider: ()
                adapter._validate_provider = lambda _provider: None
                adapter._get_env = lambda _key: None
                adapter._build_full_openclaw_config = lambda: {}
                adapter._build_register_skills_command = lambda: None
                adapter.build_cli_flags = lambda: ""
                commands: list[str] = []

                async def copy_session(_environment: object, _env: dict[str, str]) -> None:
                    return

                async def exec_as_agent(_environment: object, command: str, **_kwargs: object) -> None:
                    commands.append(command)
                    if "openclaw-agent.sqlite" in command:
                        raise OSError("copy failed")

                adapter._copy_openclaw_session_file_to_agent_logs = copy_session
                adapter.exec_as_agent = exec_as_agent
                adapter._rebuild_raw_session_jsonl = lambda: {"status": "written", "event_count": 1}
                try:
                    await adapter.run("repair", Environment(), None)
                except BaseException as error:
                    return error, commands
                raise AssertionError("export failure should be surfaced")

        error, commands = __import__("asyncio").run(exercise())
        self.assertIn("raw session export failed", str(error))
        self.assertTrue(any("openclaw-agent.sqlite" in command for command in commands))


if __name__ == "__main__":
    unittest.main()
