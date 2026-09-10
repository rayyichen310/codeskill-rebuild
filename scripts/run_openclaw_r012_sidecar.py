#!/usr/bin/env python3
"""Start one isolated R012 OpenClaw sidecar from an explicit JSON config.

This command intentionally has no model, benchmark, or task defaults.  It
starts only a local OpenAI-compatible proxy, while OpenClaw keeps ownership of
the native SQLite transcript and compaction lifecycle.
"""

from __future__ import annotations

import argparse
import json
import os
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from codeskill_rebuild.openclaw_compaction import SqliteTranscriptCompactionDetector
from codeskill_rebuild.openclaw_native_summary import NativeSummaryPermitGate
from codeskill_rebuild.openclaw_overlay import DurableOverlay, EventSelectionSettings
from codeskill_rebuild.openclaw_proxy import DurableProxyService, UrllibUpstreamTransport, handler_for
from codeskill_rebuild.openclaw_sidecar_retrieval import FrozenBankSelectors, SidecarRetrievalError
from codeskill_rebuild.solver_probe import ServerPayloadTokenCounter


def _mapping(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _selection_config(config: dict[str, Any]) -> dict[str, Any]:
    selection = _mapping(config.get("selection"), field="selection")
    mode = _text(selection.get("mode"), field="selection.mode")
    if mode == "frozen-bank":
        if "taskSkill" in config or "eventSkill" in config:
            raise ValueError("legacy taskSkill/eventSkill fields are forbidden; configure selection.mode=frozen-bank")
        # This construction validates the lifecycle/profile/trial/hash contract
        # without loading the MiniLM process during --check-config.
        FrozenBankSelectors.from_config(config)
    elif mode == "fixture-test-only":
        acknowledgement = _text(selection.get("acknowledgement"), field="selection.acknowledgement")
        if acknowledgement != "not-a-retrieval-or-lifecycle-run":
            raise ValueError("fixture-test-only selection requires its exact acknowledgement")
        for key in ("taskSkill", "eventSkill"):
            value = selection.get(key)
            if value is not None and not isinstance(value, dict):
                raise ValueError(f"selection.{key} must be an object when configured")
    else:
        raise ValueError("selection.mode must be frozen-bank or fixture-test-only")
    return selection


def _validate_openclaw_binding(config: dict[str, Any]) -> None:
    binding = _mapping(config.get("openclaw"), field="openclaw")
    path = Path(_text(binding.get("configPath"), field="openclaw.configPath"))
    if not path.is_file():
        raise ValueError("openclaw.configPath does not exist or is not a file")
    provider_id = _text(binding.get("providerId"), field="openclaw.providerId")
    if provider_id != "codeskill-r012":
        raise ValueError("openclaw.providerId must be the public codeskill-r012 provider")
    model_id = _text(binding.get("modelId"), field="openclaw.modelId")
    plugin_path = Path(_text(binding.get("pluginPath"), field="openclaw.pluginPath"))
    manifest_path = plugin_path / "openclaw.plugin.json"
    if not manifest_path.is_file():
        raise ValueError("openclaw.pluginPath must contain openclaw.plugin.json")
    manifest = _mapping(json.loads(manifest_path.read_text(encoding="utf-8")), field="CODESKILL plugin manifest")
    if manifest.get("id") != "codeskill-r012-sidecar" or manifest.get("providers") != ["codeskill-r012"]:
        raise ValueError("openclaw.pluginPath does not identify the public CODESKILL codeskill-r012 provider plugin")
    openclaw = _mapping(json.loads(path.read_text(encoding="utf-8")), field="OpenClaw config")
    plugins = _mapping(openclaw.get("plugins"), field="OpenClaw config.plugins")
    allowed = plugins.get("allow")
    if not isinstance(allowed, list) or "codeskill-r012-sidecar" not in allowed:
        raise ValueError("OpenClaw plugins.allow must include codeskill-r012-sidecar")
    load = _mapping(plugins.get("load"), field="OpenClaw config.plugins.load")
    loaded_paths = load.get("paths")
    if not isinstance(loaded_paths, list) or not all(isinstance(item, str) for item in loaded_paths):
        raise ValueError("OpenClaw plugins.load.paths must be a string list")
    resolved_plugin_path = plugin_path.resolve()
    if resolved_plugin_path not in {Path(item).resolve() for item in loaded_paths}:
        raise ValueError("OpenClaw plugins.load.paths must load openclaw.pluginPath")
    entries = _mapping(plugins.get("entries"), field="OpenClaw config.plugins.entries")
    entry = _mapping(entries.get("codeskill-r012-sidecar"), field="OpenClaw CODESKILL plugin entry")
    if entry.get("enabled") is not True:
        raise ValueError("OpenClaw CODESKILL plugin entry must be enabled")
    plugin_config = _mapping(entry.get("config"), field="OpenClaw CODESKILL plugin config")
    if plugin_config.get("trialId") != config["trialId"]:
        raise ValueError("OpenClaw CODESKILL plugin trialId must equal sidecar trialId")
    if plugin_config.get("sessionId") != config["sessionId"]:
        raise ValueError("OpenClaw CODESKILL plugin sessionId must equal sidecar sessionId")
    permit_directory = Path(_text(plugin_config.get("permitDirectory"), field="OpenClaw CODESKILL plugin permitDirectory"))
    if permit_directory.resolve() != Path(_text(config.get("permitDirectory"), field="permitDirectory")).resolve():
        raise ValueError("OpenClaw CODESKILL plugin permitDirectory must equal sidecar permitDirectory")
    providers = _mapping(_mapping(openclaw.get("models"), field="OpenClaw config.models").get("providers"), field="OpenClaw config.models.providers")
    provider = _mapping(providers.get(provider_id), field=f"OpenClaw provider {provider_id}")
    listen = _mapping(config["listen"], field="listen")
    expected_base_url = f"http://{listen['host']}:{listen['port']}/v1"
    if provider.get("baseUrl") != expected_base_url:
        raise ValueError("OpenClaw provider baseUrl must bind exactly to this sidecar listener")
    models = provider.get("models")
    if not isinstance(models, list) or not any(isinstance(item, dict) and item.get("id") == model_id for item in models):
        raise ValueError("OpenClaw provider must declare openclaw.modelId")
    agents = _mapping(openclaw.get("agents"), field="OpenClaw config.agents")
    defaults = _mapping(agents.get("defaults"), field="OpenClaw config.agents.defaults")
    model = _mapping(defaults.get("model"), field="OpenClaw config.agents.defaults.model")
    if model.get("primary") != f"{provider_id}/{model_id}":
        raise ValueError("OpenClaw agents.defaults.model.primary must select this sidecar provider/model")


def load_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    config = _mapping(value, field="sidecar config")
    for field in ("trialId", "sessionId", "sessionMarker", "permitDirectory"):
        _text(config.get(field), field=field)
    overlay = _mapping(config.get("overlay"), field="overlay")
    for field in ("statePath", "evidenceDirectory"):
        _text(overlay.get(field), field=f"overlay.{field}")
    _positive_int(overlay.get("maxInputTokens"), field="overlay.maxInputTokens")
    tokenizer = _mapping(config.get("tokenizer"), field="tokenizer")
    _text(tokenizer.get("baseUrl"), field="tokenizer.baseUrl")
    _positive_int(tokenizer.get("timeoutSeconds"), field="tokenizer.timeoutSeconds")
    upstream = _mapping(config.get("upstream"), field="upstream")
    _text(upstream.get("endpoint"), field="upstream.endpoint")
    _positive_int(upstream.get("timeoutSeconds"), field="upstream.timeoutSeconds")
    listen = _mapping(config.get("listen"), field="listen")
    _text(listen.get("host"), field="listen.host")
    port = listen.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or port < 1 or port > 65535:
        raise ValueError("listen.port must be an integer from 1 through 65535")
    marker = str(config["sessionMarker"])
    if not marker.startswith("sqlite:") or f":{config['sessionId']}:" not in marker:
        raise ValueError("sessionMarker must be the exact sqlite:<agent>:<sessionId>:<store> marker for this session")
    _selection_config(config)
    _validate_openclaw_binding(config)
    return config


def _selectors(config: dict[str, Any]) -> tuple[Any, Any, EventSelectionSettings | None, bool, bool]:
    selection = _selection_config(config)
    if selection["mode"] == "fixture-test-only":
        task = selection.get("taskSkill")
        event = selection.get("eventSkill")
        return (
            (lambda *_args: {"skill": task}) if task is not None else None,
            (lambda *_args: {"skill": event}) if event is not None else None,
            None,
            task is not None,
            event is not None,
        )
    try:
        selectors = FrozenBankSelectors.from_config(config)
    except SidecarRetrievalError as error:
        raise ValueError(f"invalid frozen-bank sidecar selection: {error}") from error
    selectors.load_encoder()
    settings = EventSelectionSettings(
        max_matching_skills=selectors.rules.event_max_matching_skills,
        profile_ref=selectors.rules.event_profile_ref,
        skill_token_budget=selectors.rules.event_skill_token_budget,
        skill_token_budget_scope=selectors.rules.event_skill_token_budget_scope,
    )
    return (
        selectors.select_task if selectors.rules.enable_task else None,
        selectors.select_event if selectors.rules.enable_event else None,
        settings,
        selectors.rules.enable_task,
        selectors.rules.enable_event,
    )


def build_service(config: dict[str, Any]) -> DurableProxyService:
    overlay_config = _mapping(config["overlay"], field="overlay")
    upstream_config = _mapping(config["upstream"], field="upstream")
    tokenizer_config = _mapping(config["tokenizer"], field="tokenizer")
    authorization: str | None = None
    authorization_env = upstream_config.get("authorizationEnv")
    if authorization_env is not None:
        name = _text(authorization_env, field="upstream.authorizationEnv")
        token = os.environ.get(name)
        if not token:
            raise ValueError(f"upstream authorization environment variable {name} is unset")
        authorization = token
    counter = ServerPayloadTokenCounter(
        _text(tokenizer_config["baseUrl"], field="tokenizer.baseUrl"),
        timeout_seconds=_positive_int(tokenizer_config["timeoutSeconds"], field="tokenizer.timeoutSeconds"),
    )
    task_selector, event_selector, event_settings, enable_task, enable_event = _selectors(config)
    overlay = DurableOverlay(
        trial_id=_text(config["trialId"], field="trialId"),
        state_path=Path(_text(overlay_config["statePath"], field="overlay.statePath")),
        evidence_dir=Path(_text(overlay_config["evidenceDirectory"], field="overlay.evidenceDirectory")),
        token_counter=counter,
        max_input_tokens=_positive_int(overlay_config["maxInputTokens"], field="overlay.maxInputTokens"),
        task_selector=task_selector,
        event_selector=event_selector,
        event_selection_settings=event_settings,
        enable_task=enable_task,
        enable_event=enable_event,
    )
    marker = _text(config["sessionMarker"], field="sessionMarker")
    detector = SqliteTranscriptCompactionDetector(
        location_provider=lambda: {"session_file": marker},
        evidence_dir=Path(_text(overlay_config["evidenceDirectory"], field="overlay.evidenceDirectory")),
    )
    gate = NativeSummaryPermitGate(
        permit_dir=Path(_text(config["permitDirectory"], field="permitDirectory")),
        expected_session_id=_text(config["sessionId"], field="sessionId"),
        trial_id=_text(config["trialId"], field="trialId"),
    )
    max_forwarded_requests = config.get("maxForwardedRequests")
    if max_forwarded_requests is not None:
        max_forwarded_requests = _positive_int(max_forwarded_requests, field="maxForwardedRequests")
    max_output_tokens = config.get("maxOutputTokens")
    if max_output_tokens is not None:
        max_output_tokens = _positive_int(max_output_tokens, field="maxOutputTokens")
    return DurableProxyService(
        overlay=overlay,
        transport=UrllibUpstreamTransport(
            _text(upstream_config["endpoint"], field="upstream.endpoint"),
            timeout_seconds=_positive_int(upstream_config["timeoutSeconds"], field="upstream.timeoutSeconds"),
            authorization=authorization,
        ),
        compaction_evidence_provider=detector,
        native_summary_permit_gate=gate,
        max_forwarded_requests=max_forwarded_requests,
        max_output_tokens=max_output_tokens,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check-config", action="store_true", help="validate configuration without binding a listener")
    args = parser.parse_args()
    config = load_config(args.config)
    listen = _mapping(config["listen"], field="listen")
    if args.check_config:
        print(json.dumps({"status": "valid", "trial_id": config["trialId"], "session_id": config["sessionId"], "selection_mode": config["selection"]["mode"]}))
        return
    service = build_service(config)
    server = ThreadingHTTPServer((str(listen["host"]), int(listen["port"])), handler_for(service))
    print(json.dumps({"status": "listening", "host": listen["host"], "port": listen["port"], "trial_id": config["trialId"], "session_id": config["sessionId"]}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
