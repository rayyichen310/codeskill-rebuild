"""Deterministic Harbor probe for the adapter's execution-user contract.

This agent deliberately makes no model call and does not attempt the benchmark
task.  It is only for validating environment behavior through Harbor's real
agent/container path before a solver calibration is authorized.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from .harbor_openclaw_adapter import CODESKILLHarborOpenClaw


def _python_command(source: str) -> str:
    return "python3 -c " + shlex.quote(source)


def _git_repository_probe_command(repository: str = "/app/repo") -> str:
    quoted = shlex.quote(repository)
    git_directory = shlex.quote(f"{repository}/.git")
    return (
        f"if [ -d {git_directory} ]; then "
        f"stat -c 'repository_owner=%u:%g repository_mode=%a' {quoted}; "
        f"if git -C {quoted} status --short >/dev/null; then "
        "printf 'git_status=readable\\n'; "
        "else git_status=$?; "
        "printf 'git_status=unreadable return_code=%s\\n' \"$git_status\" >&2; "
        "exit \"$git_status\"; fi; "
        "else printf 'git_status=not_present\\n'; fi"
    )


class CODESKILLHarborEnvironmentProbe(CODESKILLHarborOpenClaw):
    """Exercise install, Git, and Python visibility without invoking a solver."""

    async def install(self, environment: Any) -> None:
        """Use the production adapter install path before deterministic checks."""
        await super().install(environment)

    @staticmethod
    def _result(value: Any) -> dict[str, Any]:
        return {
            "return_code": getattr(value, "return_code", None),
            "stdout": str(getattr(value, "stdout", "")),
            "stderr": str(getattr(value, "stderr", "")),
        }

    @staticmethod
    def _critical_failures(*groups: tuple[str, dict[str, Any]]) -> list[str]:
        failures: list[str] = []
        for group_name, results in groups:
            for check_name, result in results.items():
                if not isinstance(result, dict) or result.get("return_code") != 0:
                    failures.append(f"{group_name}.{check_name}")
        return failures

    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        del instruction, context
        agent_results: dict[str, Any] = {}
        with environment.with_default_user(self.AGENT_USER):
            agent_env = self._agent_environment()
            agent_results["identity"] = self._result(
                await self.exec_as_agent(
                    environment,
                    command=(
                        "id; printf 'HOME=%s\\nPATH=%s\\n' \"$HOME\" \"$PATH\"; "
                        "stat -c 'workspace_owner=%u:%g workspace_mode=%a' /app; "
                        "if command -v python3 >/dev/null 2>&1; then command -v python3; python3 --version; "
                        "else printf 'python3=not_present\\n'; fi; "
                        "if command -v git >/dev/null 2>&1; then git --version; fi"
                    ),
                    env=agent_env,
                )
            )
            agent_results["system_install"] = self._result(
                await self.exec_as_agent(
                    environment,
                    command=(
                        "printf '#!/bin/sh\\nprintf codeskill-environment-probe\\n' "
                        "> /usr/local/bin/codeskill-environment-probe && "
                        "chmod 755 /usr/local/bin/codeskill-environment-probe && "
                        "/usr/local/bin/codeskill-environment-probe"
                    ),
                    env=agent_env,
                )
            )
            lifecycle_script = (
                "import { writeLifecycleRecord } from "
                + json.dumps(f"{self._codeskill_plugin_path}/lifecycle-record.js")
                + "; const record = writeLifecycleRecord("
                + json.dumps(
                    {
                        "permitDirectory": self._codeskill_permit_directory,
                        "trialId": self._codeskill_trial_id,
                        "sessionId": self._codeskill_session_id,
                    },
                    separators=(",", ":"),
                )
                + ","
                + json.dumps(self._codeskill_session_id)
                + ",'codeskill_normal_call_boundary','normal-call'); "
                + "console.log(JSON.stringify(record));"
            )
            agent_results["lifecycle_record"] = self._result(
                await self.exec_as_agent(
                    environment,
                    command=(
                        f"if [ -d {shlex.quote(self._codeskill_permit_directory)} ]; then "
                        'export NVM_DIR="${NVM_DIR:-$HOME/.nvm}" && . "$NVM_DIR/nvm.sh" && '
                        "nvm use 22 >/dev/null && "
                        f"node --input-type=module --eval {shlex.quote(lifecycle_script)}; "
                        "else printf 'lifecycle_record_probe=not_applicable\\n'; fi"
                    ),
                    env=agent_env,
                )
            )
            package_source = (
                "from pathlib import Path; import site; "
                "root=Path(site.getusersitepackages()); root.mkdir(parents=True, exist_ok=True); "
                "(root/'codeskill_environment_probe_package.py').write_text("
                "'VALUE=\\\"agent-visible\\\"\\n', encoding='utf-8'); print(root)"
            )
            agent_results["python_user_package"] = self._result(
                await self.exec_as_agent(
                    environment,
                    command=(
                        "if command -v python3 >/dev/null 2>&1; then "
                        f"{_python_command(package_source)}; "
                        "else printf 'package_probe=not_applicable\\n'; fi"
                    ),
                    env=agent_env,
                )
            )
            agent_results["git_repository"] = self._result(
                await self.exec_as_agent(
                    environment,
                    command=_git_repository_probe_command(),
                    env=agent_env,
                )
            )

        verifier_results: dict[str, Any] = {}
        verifier_results["identity"] = self._result(
            await self.exec_as_root(
                environment,
                command="id; printf 'HOME=%s\\n' \"$HOME\"; stat -c 'workspace_owner=%u:%g workspace_mode=%a' /app",
            )
        )
        verifier_results["solver_artifact"] = self._result(
            await self.exec_as_root(
                environment,
                command="test -x /usr/local/bin/codeskill-environment-probe && /usr/local/bin/codeskill-environment-probe",
            )
        )
        verifier_results["python_user_package"] = self._result(
            await self.exec_as_root(
                environment,
                command=(
                    "if command -v python3 >/dev/null 2>&1; then "
                    + _python_command(
                        "import codeskill_environment_probe_package as package; "
                        "assert package.VALUE == 'agent-visible'; print(package.__file__)"
                    )
                    + "; else printf 'package_probe=not_applicable\\n'; fi"
                ),
            )
        )
        verifier_results["git_repository"] = self._result(
            await self.exec_as_root(
                environment,
                command=_git_repository_probe_command(),
            )
        )
        failures = self._critical_failures(
            ("agent", agent_results),
            ("verifier", verifier_results),
        )
        output = Path(self.logs_dir) / "environment-contract.json"
        output.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "codeskill_harbor_environment_contract_probe",
                    "model_calls": 0,
                    "adapter_execution_user": self.AGENT_USER,
                    "adapter_environment": self._agent_environment(),
                    "agent": agent_results,
                    "verifier": verifier_results,
                    "acceptance": {
                        "status": "passed" if not failures else "failed",
                        "critical_failures": failures,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if failures:
            raise RuntimeError(
                "environment contract probe failed: " + ", ".join(failures)
            )
