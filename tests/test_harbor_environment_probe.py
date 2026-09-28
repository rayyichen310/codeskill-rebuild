from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace

from codeskill_rebuild.harbor_environment_probe import (
    CODESKILLHarborEnvironmentProbe,
    _git_repository_probe_command,
    _python_command,
)
from codeskill_rebuild.harbor_openclaw_adapter import CODESKILLHarborOpenClaw


class HarborEnvironmentProbeTest(unittest.TestCase):
    def test_probe_uses_the_production_adapter_install_path(self) -> None:
        self.assertTrue(issubclass(CODESKILLHarborEnvironmentProbe, CODESKILLHarborOpenClaw))
        source = inspect.getsource(CODESKILLHarborEnvironmentProbe.install)
        self.assertIn("await super().install(environment)", source)

    def test_probe_declares_zero_model_calls(self) -> None:
        source = inspect.getsource(CODESKILLHarborEnvironmentProbe.run)
        self.assertIn('"model_calls": 0', source)
        self.assertNotIn("openclaw agent", source)
        self.assertIn("writeLifecycleRecord", source)

    def test_result_preserves_command_evidence(self) -> None:
        value = CODESKILLHarborEnvironmentProbe._result(
            SimpleNamespace(return_code=0, stdout="visible", stderr="")
        )
        self.assertEqual(value, {"return_code": 0, "stdout": "visible", "stderr": ""})

    def test_nonzero_check_fails_the_environment_acceptance(self) -> None:
        failures = CODESKILLHarborEnvironmentProbe._critical_failures(
            ("agent", {"git_repository": {"return_code": 128}}),
            ("verifier", {"git_repository": {"return_code": 0}}),
        )
        self.assertEqual(failures, ["agent.git_repository"])

    def test_python_command_shell_quotes_the_source(self) -> None:
        command = _python_command("print('quoted')")
        self.assertTrue(command.startswith("python3 -c "))
        self.assertIn("quoted", command)

    def test_git_probe_only_reports_readable_after_a_successful_status(self) -> None:
        command = _git_repository_probe_command("/tmp/repository with spaces")
        self.assertIn("if git -C '/tmp/repository with spaces' status --short", command)
        self.assertIn("git_status=unreadable return_code=%s", command)
        self.assertIn('exit "$git_status"', command)
        self.assertLess(command.index("if git -C"), command.index("git_status=readable"))


if __name__ == "__main__":
    unittest.main()
