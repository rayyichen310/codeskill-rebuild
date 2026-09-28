"""Harbor adapter for an isolated, unmodified official OpenClaw release.

The adapter is intentionally small.  Harbor remains responsible for creating
the task container, launching the official solver and running its official
verifier.  This class only renders the public OpenClaw configuration needed to
route that solver through one CODESKILL sidecar and plugin instance.  It never
mounts, patches, forks, or substitutes an OpenClaw source tree.

The real Harbor package is an optional runtime dependency of this repository.
Keeping the import guarded lets the offline contract tests inspect the
configuration invariants without installing Harbor or Docker locally.
"""

from __future__ import annotations

import json
import shlex
import sqlite3
from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any


try:  # pragma: no cover - exercised on a Harbor host, not the local suite.
    from harbor.agents.installed.openclaw import OpenClaw as _HarborOpenClaw
    from harbor.agents.installed.base import (
        CliFlag as _HarborCliFlag,
        with_prompt_template as _with_prompt_template,
    )
except ModuleNotFoundError as error:  # pragma: no cover - local import guard.
    _HARBOR_IMPORT_ERROR: ModuleNotFoundError | None = error
    _HarborCliFlag = None

    class _HarborOpenClaw:  # type: ignore[no-redef]
        """Import-time placeholder; production construction fails clearly."""

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError(
                "CODESKILLHarborOpenClaw requires Harbor's installed OpenClaw adapter"
            ) from _HARBOR_IMPORT_ERROR

    def _with_prompt_template(function: Any) -> Any:
        return function

else:
    _HARBOR_IMPORT_ERROR = None


class HarborAdapterConfigurationError(ValueError):
    """The caller omitted an isolation-critical public configuration field."""


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HarborAdapterConfigurationError(f"{field} must be a nonempty string")
    return value.strip()


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HarborAdapterConfigurationError(f"{field} must be a positive integer")
    return value


def _inside_container_path(value: str, *, field: str) -> str:
    path = PurePosixPath(_text(value, field=field))
    if not path.is_absolute() or ".." in path.parts:
        raise HarborAdapterConfigurationError(f"{field} must be an absolute normalized container path")
    return str(path)


class CODESKILLHarborOpenClaw(_HarborOpenClaw):
    """Official Harbor OpenClaw with one explicitly bound CODESKILL sidecar.

    ``plugin_path`` and all evidence/state paths are *container* paths.  The
    invoking Harbor configuration mounts the public CODESKILL plugin at
    :attr:`PLUGIN_SOURCE_PATH`; ``install()`` copies that read-only source to
    the root-owned runtime path supplied as ``plugin_path``.  The adapter has
    no ``local_openclaw_root`` or OpenClaw source-mount option: OpenClaw is
    installed and launched by Harbor's own official adapter.
    """

    SIDECAR_PROVIDER_ID = "codeskill-r012"
    PLUGIN_ID = "codeskill-r012-sidecar"
    # Keep the official Harbor log contract explicit for offline imports too;
    # Harbor's guarded test placeholder does not define these inherited names.
    _UPLOAD_CONFIG_FILENAME = "openclaw.upload.json"
    _CONTAINER_LOGS_AGENT = "/logs/agent"
    # Docker presents bind-mounted source files with the task image user's
    # ownership.  OpenClaw 2026.9.3 rejects a plugin whose path is not owned by
    # root, so the launcher mounts this source read-only and install() copies
    # it into the root-owned runtime path before setup loads the plugin.
    PLUGIN_SOURCE_PATH = "/opt/codeskill/openclaw-sidecar-src"
    # openclaw@2026.9.3 declares Node >=24.16.0 while the audited Harbor
    # adapters (0.16.1 and 0.17.1) still ask nvm for the Node 22 major.  The
    # public subclass installs the exact supported Node 24 runtime and aliases
    # it as ``22`` so the inherited official setup/run commands remain
    # unchanged.
    # This leaves Harbor and the OpenClaw package untouched and makes the
    # compatibility workaround explicit in the launch evidence.
    NODE_COMPAT_VERSION = "24.16.0"
    _SUPPORTED_PROVIDERS = frozenset({*getattr(_HarborOpenClaw, "_SUPPORTED_PROVIDERS", frozenset()), SIDECAR_PROVIDER_ID})
    # Harbor 0.16.1's official adapter does not expose OpenClaw's public
    # ``--session-id`` flag even though the 2026.9.3 CLI supports it.  The
    # sidecar/plugin contract is per session, so leaving the CLI on its
    # generated session would make the configured permit identity unverifiable.
    # Add one descriptor through Harbor's normal public CLI-flag mechanism;
    # this does not patch or replace the official OpenClaw adapter.
    if _HarborCliFlag is not None:
        CLI_FLAGS = [
            *getattr(_HarborOpenClaw, "CLI_FLAGS", []),
            _HarborCliFlag("codeskill_session_id", cli="--session-id", type="str"),
        ]
    else:  # pragma: no cover - import-time placeholder for offline tests.
        CLI_FLAGS = list(getattr(_HarborOpenClaw, "CLI_FLAGS", []))

    # Preserve the official task image's default execution identity.  The
    # audited coding images default to root, as did the historical baseline.
    # Overriding that identity to UID 1000 made system installation paths
    # unwritable, changed Git checkout ownership before verification, and
    # redirected Python user installs into a home the root verifier could not
    # see.  ``None`` tells Harbor to use the container default without adding
    # a blanket Git safe.directory exception or rewriting the task workspace.
    AGENT_USER: str | int | None = None
    AGENT_HOME = "/root"
    AGENT_NVM_DIR = f"{AGENT_HOME}/.nvm"

    def __init__(
        self,
        *args: Any,
        sidecar_base_url: str,
        sidecar_model_id: str,
        plugin_path: str,
        permit_directory: str,
        plugin_audit_path: str,
        trial_id: str,
        session_id: str,
        context_tokens: int,
        max_output_tokens: int,
        codeskill_thinking: str = "off",
        codeskill_reasoning_effort: str | None = None,
        codeskill_temperature: float | None = None,
        codeskill_top_p: float | None = None,
        codeskill_provider_timeout_seconds: int | None = None,
        **kwargs: Any,
    ) -> None:
        if _HARBOR_IMPORT_ERROR is not None:
            raise RuntimeError(
                "CODESKILLHarborOpenClaw requires Harbor's installed OpenClaw adapter"
            ) from _HARBOR_IMPORT_ERROR
        self._codeskill_sidecar_base_url = _text(sidecar_base_url, field="sidecar_base_url").rstrip("/")
        self._codeskill_sidecar_model_id = _text(sidecar_model_id, field="sidecar_model_id")
        self._codeskill_plugin_path = _inside_container_path(plugin_path, field="plugin_path")
        if self._codeskill_plugin_path == self.PLUGIN_SOURCE_PATH:
            raise HarborAdapterConfigurationError(
                "plugin_path must be a separate runtime path from the public plugin source mount"
            )
        self._codeskill_permit_directory = _inside_container_path(permit_directory, field="permit_directory")
        self._codeskill_plugin_audit_path = _inside_container_path(plugin_audit_path, field="plugin_audit_path")
        self._codeskill_trial_id = _text(trial_id, field="trial_id")
        self._codeskill_session_id = _text(session_id, field="session_id")
        self._codeskill_context_tokens = _positive_int(context_tokens, field="context_tokens")
        self._codeskill_max_output_tokens = _positive_int(max_output_tokens, field="max_output_tokens")
        if codeskill_thinking not in {"off", "low", "medium", "high"}:
            raise HarborAdapterConfigurationError("codeskill_thinking must be off, low, medium, or high")
        if codeskill_reasoning_effort is not None and (not isinstance(codeskill_reasoning_effort, str) or not codeskill_reasoning_effort.strip()):
            raise HarborAdapterConfigurationError("codeskill_reasoning_effort must be a nonempty string when configured")
        for value, field in ((codeskill_temperature, "codeskill_temperature"), (codeskill_top_p, "codeskill_top_p")):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
                raise HarborAdapterConfigurationError(f"{field} must be a nonnegative number when configured")
        if codeskill_top_p is not None and codeskill_top_p > 1:
            raise HarborAdapterConfigurationError("codeskill_top_p must be at most 1 when configured")
        if codeskill_provider_timeout_seconds is not None:
            codeskill_provider_timeout_seconds = _positive_int(
                codeskill_provider_timeout_seconds,
                field="codeskill_provider_timeout_seconds",
            )
        self._codeskill_thinking = codeskill_thinking
        self._codeskill_reasoning_effort = codeskill_reasoning_effort.strip() if isinstance(codeskill_reasoning_effort, str) else None
        self._codeskill_temperature = codeskill_temperature
        self._codeskill_top_p = codeskill_top_p
        self._codeskill_provider_timeout_seconds = codeskill_provider_timeout_seconds
        if _HarborCliFlag is not None:
            # BaseInstalledAgent consumes descriptor kwargs before the
            # official OpenClaw __init__ sees them.  Keep session_id as a
            # CODESKILL binding field while exposing the CLI spelling only to
            # Harbor's public flag resolver.
            kwargs["codeskill_session_id"] = self._codeskill_session_id
        super().__init__(*args, **kwargs)

    @property
    def codeskill_binding(self) -> dict[str, Any]:
        """A secret-free copy suitable for the immutable Harbor launch record."""
        return {
            "provider_id": self.SIDECAR_PROVIDER_ID,
            "model_id": self._codeskill_sidecar_model_id,
            "sidecar_base_url": self._codeskill_sidecar_base_url,
            "plugin_id": self.PLUGIN_ID,
            "plugin_path": self._codeskill_plugin_path,
            "plugin_source_path": self.PLUGIN_SOURCE_PATH,
            "plugin_runtime_copy": "root-owned copy from read-only public-plugin mount",
            "permit_directory": self._codeskill_permit_directory,
            "plugin_audit_path": self._codeskill_plugin_audit_path,
            "trial_id": self._codeskill_trial_id,
            "session_id": self._codeskill_session_id,
            "context_tokens": self._codeskill_context_tokens,
            "max_output_tokens": self._codeskill_max_output_tokens,
            "thinking": getattr(self, "_codeskill_thinking", "off"),
            "reasoning_effort": getattr(self, "_codeskill_reasoning_effort", None),
            "temperature": getattr(self, "_codeskill_temperature", None),
            "top_p": getattr(self, "_codeskill_top_p", None),
            "provider_timeout_seconds": getattr(self, "_codeskill_provider_timeout_seconds", None),
            "openclaw_source": "Harbor-installed official package",
            "source_mount": "forbidden",
            "node_runtime": self.NODE_COMPAT_VERSION,
            "node_runtime_alias": "22 (for inherited Harbor adapter commands)",
        }

    async def install(self, environment: Any) -> None:
        """Install the official package with its declared Node runtime.

        The audited Harbor OpenClaw adapter is intentionally left unmodified,
        but its hard-coded ``nvm install 22`` conflicts with the exact
        OpenClaw package engine.  Installing Node 24.16.0 and exposing it
        through the inherited adapter's ``nvm use 22`` command keeps the
        official package, task image and verifier intact.
        """
        if _HarborCliFlag is None:  # pragma: no cover - guarded import path.
            raise RuntimeError("Harbor's installed OpenClaw adapter is unavailable")
        root_pkgs = "curl ca-certificates"
        await self.exec_as_root(
            environment,
            command=(
                f"apt-get update && apt-get install -y --no-install-recommends {root_pkgs}"
            ),
            env={"DEBIAN_FRONTEND": "noninteractive"},
        )
        timeout = self._install_exec_timeout_sec
        with environment.with_default_user(self.AGENT_USER):
            await self.exec_as_agent(
                environment,
                command=(
                    "set -o pipefail; "
                    "retry_all=$(curl --help all 2>/dev/null | grep -q -- '--retry-all-errors' && echo '--retry-all-errors'); "
                    "curl -fsSL --retry 5 --retry-delay 2 $retry_all "
                    "https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.2/install.sh "
                    "| bash"
                ),
                env=self._agent_environment(),
                timeout_sec=timeout,
            )
            runtime = shlex.quote(self.NODE_COMPAT_VERSION)
            await self.exec_as_agent(
                environment,
                command=(
                    'export NVM_DIR="${NVM_DIR:-$HOME/.nvm}" && . "$NVM_DIR/nvm.sh" && '
                    f"nvm install {runtime} && nvm alias 22 {runtime} && nvm use 22 && node -v && npm -v"
                ),
                env=self._agent_environment(),
                timeout_sec=timeout,
            )
            version_spec = f"@{self._version}" if self._version else "@latest"
            package = shlex.quote(f"openclaw{version_spec}")
            await self.exec_as_agent(
                environment,
                command=(
                    'export NVM_DIR="${NVM_DIR:-$HOME/.nvm}" && . "$NVM_DIR/nvm.sh" && '
                    f"nvm use 22 && npm install -g {package} "
                    "--fetch-retries=5 --fetch-retry-mintimeout=20000 "
                    "--fetch-retry-maxtimeout=120000"
                ),
                env=self._agent_environment(),
                timeout_sec=timeout,
            )
            await self.exec_as_agent(
                environment,
                command='export NVM_DIR="${NVM_DIR:-$HOME/.nvm}" && . "$NVM_DIR/nvm.sh" && nvm use 22 && openclaw --version',
                env=self._agent_environment(),
                timeout_sec=timeout,
            )
        await self.exec_as_root(
            environment,
            command=self._install_public_plugin_command(),
        )

    def get_version_command(self) -> str:
        """Let Harbor's root-side best-effort probe see the agent's install."""
        return (
            f"export HOME={shlex.quote(self.AGENT_HOME)} "
            f"NVM_DIR={shlex.quote(self.AGENT_NVM_DIR)} && "
            '. "$NVM_DIR/nvm.sh" && nvm use 22 && openclaw --version'
        )

    @classmethod
    def _agent_environment(cls, env: dict[str, str] | None = None) -> dict[str, str]:
        """Return per-command environment for the task image's default user."""
        return {
            **(env or {}),
            "HOME": cls.AGENT_HOME,
            "NVM_DIR": cls.AGENT_NVM_DIR,
        }

    def _install_public_plugin_command(self) -> str:
        """Copy the mounted public plugin into a root-owned runtime path."""
        source = shlex.quote(self.PLUGIN_SOURCE_PATH)
        target = shlex.quote(self._codeskill_plugin_path)
        return (
            "set -eu; "
            f"test -d {source}; "
            f"mkdir -p {target}; "
            f"cp -a {source}/. {target}/; "
            f"chown -R 0:0 {target}; "
            # Docker bind mounts may preserve a world-writable source mode.
            # OpenClaw refuses to load a plugin from such a path even after it
            # has been copied and made root-owned, so normalize the complete
            # public runtime copy before setup discovers it.
            f"chmod -R u=rwX,go=rX {target}"
        )

    @staticmethod
    def _noninteractive_setup_command() -> str:
        """Use OpenClaw's public headless setup route in Harbor's no-TTY task shell."""
        return (
            ". ~/.nvm/nvm.sh && nvm use 22 && "
            "openclaw setup --workspace . --non-interactive --accept-risk "
            "--skip-daemon --skip-channels --skip-skills --skip-search "
            "--skip-health --skip-ui --skip-hooks"
        )

    @staticmethod
    def _copy_upload_config_command() -> str:
        """Install Harbor's rendered config at OpenClaw's selected state path."""
        return (
            'state_dir="${OPENCLAW_STATE_DIR:-$HOME/.openclaw}" && '
            'mkdir -p "$state_dir" && cp '
            f"{shlex.quote(f'{CODESKILLHarborOpenClaw._CONTAINER_LOGS_AGENT}/{CODESKILLHarborOpenClaw._UPLOAD_CONFIG_FILENAME}')} "
            '"$state_dir/openclaw.json"'
        )

    @classmethod
    def _provider_env_keys(cls, provider: str) -> tuple[str, ...]:
        """Do not forward an upstream credential into the task container.

        The only endpoint OpenClaw sees is its one local sidecar.  Upstream
        authorization, if any, remains in the separately started sidecar
        process and is never placed in a Harbor agent environment or artifact.
        """
        if provider == cls.SIDECAR_PROVIDER_ID:
            return ()
        return super()._provider_env_keys(provider)

    def _bind_public_plugin(self, base_config: dict[str, Any]) -> dict[str, Any]:
        """Return a pure bound config, independently testable without Harbor."""
        config = deepcopy(base_config)
        models = config.setdefault("models", {})
        if not isinstance(models, dict):
            raise HarborAdapterConfigurationError("Harbor OpenClaw config.models must be an object")
        providers = models.setdefault("providers", {})
        if not isinstance(providers, dict):
            raise HarborAdapterConfigurationError("Harbor OpenClaw config.models.providers must be an object")
        provider_config: dict[str, Any] = {
            "baseUrl": self._codeskill_sidecar_base_url,
            "apiKey": "${CODESKILL_SIDECAR_KEY}",
            "api": "openai-completions",
            "models": [
                {
                    "id": self._codeskill_sidecar_model_id,
                    "name": "CODESKILL sidecar upstream model",
                    "reasoning": bool(getattr(self, "_codeskill_reasoning_effort", None)) or getattr(self, "_codeskill_thinking", "off") != "off",
                    "input": ["text", "image"],
                    "contextWindow": self._codeskill_context_tokens,
                    "contextTokens": self._codeskill_context_tokens,
                    "maxTokens": self._codeskill_max_output_tokens,
                    "compat": {
                        "supportsUsageInStreaming": True,
                        **(
                            {
                                "thinkingFormat": "deepseek",
                                # The upstream accepts the baseline's
                                # reasoning_effort=max through extra_body;
                                # keep the public capability list honest to
                                # OpenClaw's selector rather than inventing a
                                # new CLI level.
                                "supportedReasoningEfforts": ["off", "low", "medium", "high"],
                            }
                            if bool(getattr(self, "_codeskill_reasoning_effort", None)) or getattr(self, "_codeskill_thinking", "off") != "off"
                            else {}
                        ),
                    },
                }
            ],
        }
        provider_timeout = getattr(self, "_codeskill_provider_timeout_seconds", None)
        if provider_timeout is not None:
            provider_config["timeoutSeconds"] = provider_timeout
        providers[self.SIDECAR_PROVIDER_ID] = provider_config
        agents = config.setdefault("agents", {})
        if not isinstance(agents, dict):
            raise HarborAdapterConfigurationError("Harbor OpenClaw config.agents must be an object")
        defaults = agents.setdefault("defaults", {})
        if not isinstance(defaults, dict):
            raise HarborAdapterConfigurationError("Harbor OpenClaw config.agents.defaults must be an object")
        defaults["model"] = {
            "primary": f"{self.SIDECAR_PROVIDER_ID}/{self._codeskill_sidecar_model_id}"
        }
        defaults["thinkingDefault"] = getattr(self, "_codeskill_thinking", "off")
        default_models = defaults.setdefault("models", {})
        if not isinstance(default_models, dict):
            raise HarborAdapterConfigurationError("OpenClaw agents.defaults.models must be an object")
        params: dict[str, Any] = {}
        temperature = getattr(self, "_codeskill_temperature", None)
        top_p = getattr(self, "_codeskill_top_p", None)
        reasoning_effort = getattr(self, "_codeskill_reasoning_effort", None)
        if temperature is not None:
            params["temperature"] = temperature
        if top_p is not None:
            # OpenClaw's public extra-params bridge names this runtime field
            # ``topP`` and maps it to the provider's wire spelling.  Supplying
            # ``top_p`` here is silently ignored by the official adapter.
            params["topP"] = top_p
        if reasoning_effort is not None:
            params["extra_body"] = {"reasoning_effort": reasoning_effort}
        if params:
            default_models[f"{self.SIDECAR_PROVIDER_ID}/{self._codeskill_sidecar_model_id}"] = {"params": params}
        # OpenClaw 2026.9.3 stores this limit on each provider model.  Leaving
        # the removed agents.defaults.contextTokens key in the upload causes a
        # migration warning and does not configure the selected model.
        defaults.pop("contextTokens", None)
        defaults["maxConcurrent"] = 1
        plugins = config.setdefault("plugins", {})
        if not isinstance(plugins, dict):
            raise HarborAdapterConfigurationError("Harbor OpenClaw config.plugins must be an object")
        allowed = plugins.get("allow", [])
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            raise HarborAdapterConfigurationError("Harbor OpenClaw config.plugins.allow must be a string list")
        plugins["allow"] = [*dict.fromkeys([*allowed, self.PLUGIN_ID])]
        load = plugins.get("load", {})
        if not isinstance(load, dict):
            raise HarborAdapterConfigurationError("Harbor OpenClaw config.plugins.load must be an object")
        paths = load.get("paths", [])
        if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
            raise HarborAdapterConfigurationError("Harbor OpenClaw config.plugins.load.paths must be a string list")
        load["paths"] = [*dict.fromkeys([*paths, self._codeskill_plugin_path])]
        plugins["load"] = load
        entries = plugins.get("entries", {})
        if not isinstance(entries, dict):
            raise HarborAdapterConfigurationError("Harbor OpenClaw config.plugins.entries must be an object")
        entries[self.PLUGIN_ID] = {
            "enabled": True,
            "config": {
                "permitDirectory": self._codeskill_permit_directory,
                "trialId": self._codeskill_trial_id,
                "sessionId": self._codeskill_session_id,
                "auditPath": self._codeskill_plugin_audit_path,
            },
        }
        plugins["entries"] = entries
        return config

    def _build_full_openclaw_config(self) -> dict[str, Any]:
        """Bind the public plugin/provider while preserving Harbor's config.

        The only model endpoint exposed to the solver is the local sidecar.
        The sidecar itself owns the separately configured upstream model and
        tokenizer endpoints; neither secret nor upstream URL is embedded here.
        """
        return self._bind_public_plugin(super()._build_full_openclaw_config())

    def _agent_state_export_command(self) -> str:
        """Copy the official runtime's SQLite store into Harbor's trial logs.

        This runs only after Harbor's official OpenClaw command exits.  It does
        not touch the store in place and does not install or alter OpenClaw.
        ``sqlite3`` is intentionally not required in the heterogeneous task
        image: host-side Python reconstructs the JSONL from this copied file.
        The container may copy it as root with mode 0600. Give only the owner
        of Harbor's host log directory access before the host reads it.
        """
        owner = self.logs_dir.stat()
        owner_spec = f"{owner.st_uid}:{owner.st_gid}"
        return (
            "set +e; "
            'src="${OPENCLAW_STATE_DIR:-$HOME/.openclaw}/agents/main/agent"; '
            'dst="/logs/agent/codeskill-openclaw-state"; '
            'mkdir -p "$dst" 2>/dev/null; '
            'for f in "$src"/openclaw-agent.sqlite "$src"/openclaw-agent.sqlite-wal "$src"/openclaw-agent.sqlite-shm; do '
            'if [ -f "$f" ]; then name="${f##*/}"; cp -f "$f" "$dst/$name"; '
            f'chown {owner_spec} "$dst/$name"; fi; '
            'done; '
            f'chown {owner_spec} "$dst"; exit 0'
        )

    def _rebuild_raw_session_jsonl(self) -> dict[str, Any]:
        """Write Harbor's documented JSONL shape from the copied SQLite rows.

        The result is deliberately structured instead of silently discarded.
        Harbor's solver exception remains the primary failure, while a caller
        can still record whether the best-effort raw-session export succeeded.
        """
        logs_dir = getattr(self, "logs_dir", None)
        if logs_dir is None:
            return {"status": "logs_directory_missing"}
        logs = __import__("pathlib").Path(logs_dir)
        database = logs / "codeskill-openclaw-state" / "openclaw-agent.sqlite"
        if not database.is_file():
            return {"status": "sqlite_database_missing", "database": str(database)}
        try:
            # OpenClaw appends a diagnostic line after its final JSON envelope,
            # so Harbor's suffix-only stdout parser may return ``None`` even
            # after a successful agent run.  The runner already binds the
            # exact session ID into the public adapter; use that binding when
            # selecting rows instead of inferring identity from human-facing
            # stdout.
            session_id = self._codeskill_session_id
            if not isinstance(session_id, str) or not session_id:
                return {"status": "bound_session_id_missing", "database": str(database)}
            # Harbor may still own the logs directory while this cleanup
            # callback runs. A writable SQLite connection can fail when it
            # tries to create a journal beside the copied database, even
            # though the session rows themselves are readable. The copied
            # store is evidence only; open it without write intent.
            connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
            try:
                rows = connection.execute(
                    "SELECT event_json FROM transcript_events WHERE session_id = ? ORDER BY seq", (session_id,)
                ).fetchall()
            finally:
                connection.close()
            if not rows:
                return {
                    "status": "bound_session_rows_missing",
                    "database": str(database),
                    "session_id": session_id,
                }
            output = logs / "openclaw.session.jsonl"
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("w", encoding="utf-8") as handle:
                for (raw_event,) in rows:
                    event = json.loads(raw_event)
                    if isinstance(event, dict):
                        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
                        handle.write("\n")
            return {
                "status": "written",
                "database": str(database),
                "session_id": session_id,
                "event_count": len(rows),
                "path": str(output),
            }
        except (KeyError, OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as error:
            # Harbor's own transcript copy is best-effort.  If an official
            # release changes the SQLite schema, retain Harbor's normal
            # trajectory fallback and let the development runner fail closed
            # when the required raw JSONL evidence is absent.
            return {
                "status": "rebuild_failed",
                "database": str(database),
                "error_type": type(error).__name__,
                "error": str(error),
            }

    def _write_session_export_status(self, status: dict[str, Any]) -> dict[str, Any]:
        """Persist cleanup/export evidence without masking a solver failure."""
        logs_dir = getattr(self, "logs_dir", None)
        if logs_dir is None:
            return {"status": "logs_directory_missing"}
        try:
            output = __import__("pathlib").Path(logs_dir) / "session-export.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return {"status": "written", "path": str(output)}
        except BaseException as error:
            # The primary solver exception, if any, must remain visible to
            # Harbor.  The caller records this write failure in memory as an
            # additional cleanup error when it can.
            return {"status": "failed", "error_type": type(error).__name__, "error": str(error)}

    @_with_prompt_template
    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        """Run the official OpenClaw flow with public headless setup, then preserve rows."""
        escaped_instruction = shlex.quote(instruction)
        if not self.model_name or "/" not in self.model_name:
            raise ValueError("Model name must be in the format provider/model_name")

        provider, _ = self.model_name.split("/", 1)
        self._validate_provider(provider)
        env: dict[str, str] = {}
        keys = self._provider_env_keys(provider)
        self.logger.debug(
            "OpenClaw forwarding env vars for provider %r: %s", provider, list(keys)
        )
        for key in keys:
            val = self._get_env(key)
            if val:
                env[key] = val
            else:
                self.logger.debug("Missing optional env key for OpenClaw run: %s", key)

        upload_path = self.logs_dir / self._UPLOAD_CONFIG_FILENAME
        upload_path.write_text(
            json.dumps(self._build_full_openclaw_config(), indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            (self.logs_dir / "instruction.txt").write_text(instruction, encoding="utf-8")
        except OSError:
            pass

        # OpenClaw 2026.9.3 refuses its legacy setup command without a TTY.
        # The public non-interactive setup command creates the same baseline;
        # Harbor still owns the official package, task environment, and verifier.
        with environment.with_default_user(self.AGENT_USER):
            agent_env = self._agent_environment(env)
            primary_error: BaseException | None = None
            try:
                await self.exec_as_agent(
                    environment,
                    command=self._noninteractive_setup_command(),
                    env=agent_env,
                )

                await self.exec_as_agent(
                    environment,
                    command=self._copy_upload_config_command(),
                    env=agent_env,
                )

                skills_command = self._build_register_skills_command()
                if skills_command:
                    await self.exec_as_agent(environment, command=skills_command, env=agent_env)

                cli_flags = self.build_cli_flags()
                cli_flags_arg = (cli_flags + " ") if cli_flags else ""
                command = (
                    ". ~/.nvm/nvm.sh && nvm use 22 && "
                    f"openclaw agent --local --json {cli_flags_arg}"
                    f"--model {shlex.quote(self.model_name)} "
                    f"--message {escaped_instruction} "
                    "2>&1 </dev/null | stdbuf -oL tee /logs/agent/openclaw.txt"
                )
                self.logger.debug("OpenClaw agent env keys: %s", sorted(agent_env))
                self.logger.debug("OpenClaw agent command: %s", command)
                await self.exec_as_agent(environment, command, env=agent_env)
            except BaseException as error:
                # Save the exact object so cleanup can never replace the
                # official solver error (including timeout/rate-limit types).
                primary_error = error
                raise
            finally:
                cleanup_errors: list[dict[str, Any]] = []
                try:
                    await self._copy_openclaw_session_file_to_agent_logs(environment, agent_env)
                except BaseException as error:
                    cleanup_errors.append(
                        {
                            "phase": "copy_session_file",
                            "error_type": type(error).__name__,
                            "error": str(error),
                        }
                    )
                try:
                    await self.exec_as_agent(
                        environment,
                        command=self._agent_state_export_command(),
                        env=self._agent_environment(),
                    )
                except BaseException as error:
                    cleanup_errors.append(
                        {
                            "phase": "sqlite_export",
                            "error_type": type(error).__name__,
                            "error": str(error),
                        }
                    )
                try:
                    rebuild = self._rebuild_raw_session_jsonl()
                except BaseException as error:
                    rebuild = {
                        "status": "rebuild_exception",
                        "error_type": type(error).__name__,
                        "error": str(error),
                    }
                if rebuild.get("status") != "written":
                    cleanup_errors.append(
                        {
                            "phase": "rebuild_jsonl",
                            "error_type": "HarborSessionExportError",
                            "error": rebuild.get("status", "unknown export status"),
                            "evidence": rebuild,
                        }
                    )
                status = {
                    "kind": "r015_openclaw_session_export",
                    "status": "written" if not cleanup_errors else "failed",
                    "bound_session_id": self._codeskill_session_id,
                    "primary_error": (
                        {"error_type": type(primary_error).__name__, "error": str(primary_error)}
                        if primary_error is not None
                        else None
                    ),
                    "rebuild": rebuild,
                    "cleanup_errors": cleanup_errors,
                }
                status_write = self._write_session_export_status(status)
                if status_write.get("status") != "written":
                    cleanup_errors.append(
                        {
                            "phase": "write_export_status",
                            "error_type": "HarborSessionExportStatusError",
                            "error": status_write.get("status", "unknown export status write failure"),
                            "evidence": status_write,
                        }
                    )
                # On a normal solver success, missing raw evidence is a real
                # Harbor integration failure.  If the solver already failed,
                # leave cleanup errors as attached evidence and re-raise the
                # original exception unchanged.
                if primary_error is None and cleanup_errors:
                    raise RuntimeError("OpenClaw raw session export failed: " + json.dumps(cleanup_errors, ensure_ascii=False))
