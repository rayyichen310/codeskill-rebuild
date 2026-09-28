"""Durable C-only, two-round coding protocol coordination.

This module owns the protocol boundary for the new preparation track.  It is
deliberately independent of the historical A/B/C lifecycle: each round has a
fresh :class:`SkillBank`, fresh trajectory/description pools, one C-only trial
per task, and an explicit publication boundary before the next task can be
frozen.  A baseline manifest is accepted as comparison metadata only; its
skills and traces are never loaded by this coordinator.

The class coordinates evidence and bank state.  A caller supplies the actual
Harbor/OpenClaw trial evidence and the already validated extraction/Fig.8/
Fig.9 records.  It does not invent a model decision or turn an offline fixture
into live evidence.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Iterable

from .bank import BankError, SkillBank, validate_skill_candidate
from .types import canonical_instance_id, canonical_json, read_json, sha256_file, sha256_text, utc_now, write_json


class COnlyProtocolError(RuntimeError):
    """Raised when a C-only lifecycle invariant or evidence boundary fails."""


_FORBIDDEN_KEYS = frozenset(
    {
        "baseline_skills",
        "baseline_trajectory",
        "historical_skills",
        "historical_trajectory",
        "old_bank",
        "old_skill_bank",
        "verifier_hidden_answers",
        "solver_hidden_answer",
        "hidden_answer",
        "replay_of_baseline",
    }
)


def _object(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise COnlyProtocolError(f"{field} must be an object")
    return value


def _nonempty(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise COnlyProtocolError(f"{field} must be a nonempty string")
    return value.strip()


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise COnlyProtocolError(f"{field} must be a positive integer")
    return value


def _sha256_hex(value: Any, *, field: str) -> str:
    text = _nonempty(value, field=field)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text.lower()):
        raise COnlyProtocolError(f"{field} must be a lowercase SHA-256 hex digest")
    return text.lower()


def _verified_file_ref(
    value: Any,
    *,
    field: str,
    expected_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate an immutable evidence file reference at the protocol edge.

    A path and a digest supplied by a caller are not evidence by themselves:
    the path must resolve to a regular file and the bytes on disk must still
    produce the stated digest.  Keeping this check here means a resumed
    coordinator cannot silently accept a relabelled or cross-trial trace.
    """
    ref = _object(value, field=field)
    path_value = _nonempty(ref.get("path"), field=f"{field}.path")
    path = Path(path_value)
    if not path.is_file():
        raise COnlyProtocolError(f"{field}.path does not identify an existing file: {path_value}")
    stated = _sha256_hex(ref.get("sha256"), field=f"{field}.sha256")
    actual = sha256_file(path)
    if stated != actual:
        raise COnlyProtocolError(f"{field}.sha256 does not match the evidence file: {path_value}")
    if expected_binding is not None:
        try:
            contents = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise COnlyProtocolError(f"{field}.path is not a readable JSON trajectory evidence file: {path_value}") from error
        if not isinstance(contents, dict):
            raise COnlyProtocolError(f"{field}.path does not contain a bound trajectory object: {path_value}")
        binding = contents.get("r015_binding")
        if not isinstance(binding, dict):
            raise COnlyProtocolError(f"{field}.path has no r015_binding identity record: {path_value}")
        for key, expected in expected_binding.items():
            if binding.get(key) != expected:
                raise COnlyProtocolError(
                    f"{field}.path r015_binding.{key} differs from the frozen assignment: {path_value}"
                )
    return {"path": path_value, "sha256": actual}


def _verified_hash_ref(value: Any, *, field: str) -> dict[str, Any]:
    """Validate a generic immutable JSON/evidence reference.

    Trajectory references use :func:`_verified_file_ref` with an identity
    binding.  Manager response and description references are different
    immutable JSON artifacts, so they need the same byte/hash check without
    pretending that their payload is a trajectory.
    """
    ref = _object(value, field=field)
    path_value = _nonempty(ref.get("path"), field=f"{field}.path")
    path = Path(path_value)
    if not path.is_file():
        raise COnlyProtocolError(f"{field}.path does not identify an existing file: {path_value}")
    stated = _sha256_hex(ref.get("sha256"), field=f"{field}.sha256")
    actual = sha256_file(path)
    if stated != actual:
        raise COnlyProtocolError(f"{field}.sha256 does not match the evidence file: {path_value}")
    return {"path": path_value, "sha256": actual}


def _validate_embedded_hash_refs(value: Any, *, field: str = "evidence") -> None:
    """Verify every nested immutable ``path``/``sha256`` evidence pair.

    The phase wrapper and operation journal hashes protect the JSON records,
    but they do not protect the files those records cite.  Manager journals,
    model responses, sidecar attempts, Harbor packets, and raw session copies
    can all be nested at different keys, so validate the common reference
    envelope recursively at the coordinator boundary.  ``exists: false`` is a
    deliberate absence marker used for optional artifacts and carries no
    bytes to verify.
    """
    if isinstance(value, dict):
        if "path" in value and "sha256" in value and value.get("exists") is not False:
            path_value = _nonempty(value.get("path"), field=f"{field}.path")
            path = Path(path_value)
            if not path.is_file():
                raise COnlyProtocolError(
                    f"{field}.path does not identify an immutable evidence file: {path_value}"
                )
            stated = _sha256_hex(value.get("sha256"), field=f"{field}.sha256")
            actual = sha256_file(path)
            if stated != actual:
                raise COnlyProtocolError(
                    f"{field}.sha256 does not match the evidence file: {path_value}"
                )
        for key, child in value.items():
            _validate_embedded_hash_refs(child, field=f"{field}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_embedded_hash_refs(child, field=f"{field}[{index}]")


def _validate_trajectory_ref(
    value: Any,
    *,
    field: str,
    round_id: int,
    task_id: str,
    trial_id: str,
) -> dict[str, Any]:
    ref = _object(value, field=field)
    if ref.get("round_id") != round_id or _canonical_task(ref.get("task_id"), field=f"{field}.task_id") != task_id:
        raise COnlyProtocolError(f"{field} is not bound to C-only round {round_id} task {task_id}")
    if _nonempty(ref.get("trial_id"), field=f"{field}.trial_id") != trial_id:
        raise COnlyProtocolError(f"{field}.trial_id differs from the frozen C-only trial")
    _nonempty(ref.get("session_id"), field=f"{field}.session_id")
    if ref.get("complete") is not True:
        raise COnlyProtocolError(f"{field}.complete must be true for a completed trajectory")
    file_ref = _verified_file_ref(
        ref,
        field=field,
        expected_binding={
            "round_id": round_id,
            "task_id": task_id,
            "trial_id": trial_id,
            "session_id": _nonempty(ref.get("session_id"), field=f"{field}.session_id"),
        },
    )
    normalized = deepcopy(ref)
    normalized.update(file_ref)
    return normalized


def _reject_historical_material(value: Any, *, path: str = "evidence") -> None:
    """Reject known leakage fields while allowing comparison references."""
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key.lower() in _FORBIDDEN_KEYS:
                raise COnlyProtocolError(f"{path}.{key} is forbidden in a C-only trial record")
            _reject_historical_material(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_historical_material(child, path=f"{path}[{index}]")


def _canonical_task(value: Any, *, field: str) -> str:
    raw = _nonempty(value, field=field)
    try:
        return canonical_instance_id(raw)
    except ValueError as error:
        raise COnlyProtocolError(f"{field} is not a valid task ID") from error


def _task_list(config: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = config.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise COnlyProtocolError("C-only config needs a nonempty tasks list")
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for expected_order, raw in enumerate(tasks, start=1):
        item = _object(raw, field=f"tasks[{expected_order - 1}]")
        order = item.get("order")
        if order != expected_order:
            raise COnlyProtocolError("C-only task order must be contiguous and explicit")
        task_id = _canonical_task(item.get("canonical_instance_id"), field=f"tasks[{expected_order - 1}].canonical_instance_id")
        if task_id in seen:
            raise COnlyProtocolError(f"duplicate C-only task: {task_id}")
        seen.add(task_id)
        task_name = _nonempty(item.get("task_name"), field=f"tasks[{expected_order - 1}].task_name")
        if not task_name.startswith("terminal-bench/"):
            raise COnlyProtocolError(f"tasks[{expected_order - 1}].task_name must retain the official namespace")
        if task_name.removeprefix("terminal-bench/") != task_id:
            raise COnlyProtocolError(f"task_name and canonical_instance_id disagree for {task_id}")
        digest = item.get("task_digest")
        if digest is not None:
            _nonempty(digest, field=f"tasks[{expected_order - 1}].task_digest")
        public_toml_sha = item.get("public_task_toml_sha256")
        if public_toml_sha is not None:
            _sha256_hex(public_toml_sha, field=f"tasks[{expected_order - 1}].public_task_toml_sha256")
        public_image = item.get("public_docker_image")
        if public_image is not None:
            _nonempty(public_image, field=f"tasks[{expected_order - 1}].public_docker_image")
        public_image_id = item.get("public_docker_image_id")
        if public_image_id is not None:
            _nonempty(public_image_id, field=f"tasks[{expected_order - 1}].public_docker_image_id")
        public_repo_digests = item.get("public_docker_repo_digests")
        if public_repo_digests is not None and (
            not isinstance(public_repo_digests, list)
            or not all(isinstance(value, str) and value.strip() for value in public_repo_digests)
        ):
            raise COnlyProtocolError(
                f"tasks[{expected_order - 1}].public_docker_repo_digests must be a string list"
            )
        parsed.append(
            {
                "order": order,
                "task_name": task_name,
                "canonical_instance_id": task_id,
                "task_digest": digest,
                "public_task_toml_sha256": public_toml_sha,
                "public_task_toml_size_bytes": item.get("public_task_toml_size_bytes"),
                "public_docker_image": public_image,
                "public_docker_image_id": public_image_id,
                "public_docker_repo_digests": deepcopy(public_repo_digests),
                "baseline_outcome": deepcopy(item.get("baseline_outcome")),
            }
        )
    return parsed


def validate_c_only_config(value: dict[str, Any]) -> dict[str, Any]:
    """Validate the versioned C-only config without applying defaults."""
    config = _object(value, field="config")
    if config.get("kind") != "r015_c_only_two_round_protocol":
        raise COnlyProtocolError("config kind must be r015_c_only_two_round_protocol")
    _nonempty(config.get("protocol_id"), field="protocol_id")
    protocol = _object(config.get("protocol"), field="protocol")
    if protocol.get("condition") != "C-only":
        raise COnlyProtocolError("C-only config must name condition=C-only")
    if protocol.get("round_count") != 2:
        raise COnlyProtocolError("C-only config must define exactly two sequential rounds")
    for field in ("rounds_sequential", "fresh_bank_each_round", "fresh_trajectory_pool_each_round", "fresh_description_pool_each_round", "publish_after_each_task", "baseline_reference_only", "formal_start_requires_user_confirmation"):
        if protocol.get(field) is not True:
            raise COnlyProtocolError(f"C-only config must set protocol.{field}=true")
    if protocol.get("one_trial_per_task_per_round") is not True:
        raise COnlyProtocolError("C-only config must run one trial per task per round")
    pairing = _object(protocol.get("task_skill_pairing"), field="protocol.task_skill_pairing")
    if pairing.get("min_distinct_completed_tasks") != 2 or pairing.get("max_distinct_completed_tasks") != 3:
        raise COnlyProtocolError("task skill pairing must require exactly 2 to 3 distinct current-round tasks")
    if pairing.get("early_action") != "skip":
        raise COnlyProtocolError("early task-skill pairing must skip when evidence is insufficient")
    events = _object(protocol.get("event_extraction"), field="protocol.event_extraction")
    if "max_initial_attempts" in events:
        # Frozen R015 records used the historical three-call schedule.
        if events.get("max_initial_attempts") != 3:
            raise COnlyProtocolError("event extraction must cap initial attempts at three")
        if events.get("stop_on") != ["skip", "duplicate"]:
            raise COnlyProtocolError("event extraction stop_on must be [skip, duplicate]")
    elif events.get("stop_on") != []:
        raise COnlyProtocolError("direct Event extraction must keep all segment outcomes")
    tasks = _task_list(config)
    retrieval_profile = _object(config.get("retrieval_profile"), field="retrieval_profile")
    try:
        from .r012_execution import validate_execution_profile

        validate_execution_profile(retrieval_profile)
    except Exception as error:
        if isinstance(error, COnlyProtocolError):
            raise
        raise COnlyProtocolError(f"retrieval_profile is not a valid frozen R012 profile: {error}") from error
    baseline = _object(config.get("baseline_manifest"), field="baseline_manifest")
    _nonempty(baseline.get("path"), field="baseline_manifest.path")
    _sha256_hex(baseline.get("sha256"), field="baseline_manifest.sha256")
    task_audit = config.get("task_artifact_audit")
    if task_audit is not None:
        task_audit = _object(task_audit, field="task_artifact_audit")
        _nonempty(task_audit.get("path"), field="task_artifact_audit.path")
        _sha256_hex(task_audit.get("sha256"), field="task_artifact_audit.sha256")
        if task_audit.get("hidden_material_read") is not False:
            raise COnlyProtocolError("task_artifact_audit must explicitly mark hidden_material_read=false")
    runtime = _object(config.get("runtime_alignment"), field="runtime_alignment")
    parity_gate = runtime.get("parity_gate")
    if parity_gate is not None:
        parity_gate = _object(parity_gate, field="runtime_alignment.parity_gate")
        gate_status = parity_gate.get("status")
        if gate_status not in {"aligned", "blocked_requires_user_decision", "approved_deviation"}:
            raise COnlyProtocolError(
                "runtime_alignment.parity_gate.status must be aligned, blocked_requires_user_decision, or approved_deviation"
            )
        _nonempty(parity_gate.get("evidence_path"), field="runtime_alignment.parity_gate.evidence_path")
        if gate_status == "blocked_requires_user_decision":
            options = parity_gate.get("decision_options")
            if not isinstance(options, list) or not options:
                raise COnlyProtocolError(
                    "a blocked runtime parity gate must provide decision_options"
                )
        if gate_status == "approved_deviation":
            _nonempty(
                parity_gate.get("approval_reference"),
                field="runtime_alignment.parity_gate.approval_reference",
            )
    baseline_runtime: dict[str, Any] | None = None
    prepared_runtime: dict[str, Any] | None = None
    for section_name in ("baseline_observed", "prepared_target"):
        section = _object(runtime.get(section_name), field=f"runtime_alignment.{section_name}")
        for field in ("model_id", "provider_id", "endpoint", "thinking", "reasoning_effort"):
            _nonempty(section.get(field), field=f"runtime_alignment.{section_name}.{field}")
        _positive_int(section.get("context_tokens"), field=f"runtime_alignment.{section_name}.context_tokens")
        if section_name == "baseline_observed":
            baseline_runtime = section
        else:
            prepared_runtime = section
    if baseline_runtime is not None and prepared_runtime is not None:
        for field in ("model_id", "thinking", "reasoning_effort", "temperature", "top_p", "context_tokens", "max_output_tokens"):
            if prepared_runtime.get(field) != baseline_runtime.get(field):
                raise COnlyProtocolError(f"prepared target {field} must preserve the audited baseline value")
        if (
            prepared_runtime.get("max_forwarded_requests") is not None
            or prepared_runtime.get("proxy_max_forwarded_requests") is not None
            or prepared_runtime.get("max_model_calls") is not None
            or prepared_runtime.get("max_turns") is not None
        ):
            raise COnlyProtocolError("prepared C-only target must not add a hidden model-call or turn cap")
    return {**deepcopy(config), "tasks": tasks}


def _validate_baseline_manifest(value: dict[str, Any], *, tasks: list[dict[str, Any]]) -> None:
    if value.get("kind") != "r015_legacy_coding_baseline_manifest":
        raise COnlyProtocolError("baseline manifest is not the audited legacy coding manifest")
    source = _object(value.get("source"), field="baseline.source")
    if source.get("skills_imported") is not False or source.get("trajectories_imported") is not False or source.get("solver_input_imported", False) is not False:
        raise COnlyProtocolError("baseline manifest must explicitly mark solver input, skills, and trajectories as not imported")
    baseline_tasks = value.get("tasks")
    if not isinstance(baseline_tasks, list) or len(baseline_tasks) != len(tasks):
        raise COnlyProtocolError("baseline manifest task count differs from C-only task order")
    for expected, (configured, observed) in enumerate(zip(tasks, baseline_tasks, strict=True), start=1):
        item = _object(observed, field=f"baseline.tasks[{expected - 1}]")
        if item.get("order") != expected or _canonical_task(item.get("canonical_instance_id"), field="baseline task") != configured["canonical_instance_id"]:
            raise COnlyProtocolError("C-only task order differs from the audited baseline order")
    # The coordinator deliberately does not inspect trajectories or skills in
    # the baseline object.  This check guards against a future manifest shape
    # accidentally moving them into the comparison metadata itself.
    _reject_historical_material({"baseline_manifest": {"source": source, "tasks": [{"task_name": item.get("task_name")} for item in baseline_tasks]}})


def _empty_round(round_id: int, task_order: list[str]) -> dict[str, Any]:
    bank = SkillBank.empty("terminal-bench")
    return {
        "round_id": round_id,
        "status": "active" if round_id == 1 else "not_started",
        "task_order": list(task_order),
        "next_task_index": 0,
        "bank": bank.to_dict(),
        "trajectory_pool": [],
        "description_pool": [],
        "task_candidate_pool": [],
        "assignments": {},
        "completed_tasks": {},
        "event_attempts": {},
        "publication_failures": [],
        "operations": [],
    }


def _round_digest(round_state: dict[str, Any]) -> str:
    return sha256_text(canonical_json(round_state))


def _r012_profile_sha256(profile: dict[str, Any]) -> str:
    """Hash the validated R012 profile using the shared public definition."""
    from .r012_execution import profile_sha256

    return profile_sha256(profile)


class COnlyProtocol:
    """A persisted, sequential two-round C-only lifecycle."""

    def __init__(self, *, config: dict[str, Any], baseline_manifest: dict[str, Any], state: dict[str, Any], config_path: Path, baseline_path: Path) -> None:
        self.config = validate_c_only_config(config)
        self.tasks = _task_list(self.config)
        _validate_baseline_manifest(baseline_manifest, tasks=self.tasks)
        self.baseline_manifest = deepcopy(baseline_manifest)
        self.state = deepcopy(state)
        # Schema-v1 states created before the isolated SOP pool existed are
        # losslessly upgraded with an empty pool. Existing extraction and
        # publication records remain untouched and are never replayed.
        rounds = self.state.get("rounds")
        if isinstance(rounds, dict):
            for round_state in rounds.values():
                if isinstance(round_state, dict):
                    round_state.setdefault("task_candidate_pool", [])
        self.config_path = config_path
        self.baseline_path = baseline_path
        self._validate_state()

    @classmethod
    def initialize(cls, config_path: Path, baseline_path: Path, state_path: Path | None = None) -> "COnlyProtocol":
        config = validate_c_only_config(read_json(config_path))
        configured_baseline = _object(config.get("baseline_manifest"), field="baseline_manifest")
        if _sha256_hex(configured_baseline.get("sha256"), field="baseline_manifest.sha256") != sha256_file(baseline_path):
            raise COnlyProtocolError("C-only config baseline manifest hash does not match the supplied baseline")
        baseline = read_json(baseline_path)
        tasks = _task_list(config)
        _validate_baseline_manifest(baseline, tasks=tasks)
        task_order = [item["canonical_instance_id"] for item in tasks]
        state = {
            "schema_version": 1,
            "kind": "r015_c_only_protocol_state",
            "protocol_id": config["protocol_id"],
            "created_at_utc": utc_now(),
            "updated_at_utc": utc_now(),
            "config": {"path": str(config_path), "sha256": sha256_file(config_path)},
            "baseline_manifest": {"path": str(baseline_path), "sha256": sha256_file(baseline_path), "comparison_only": True},
            "task_order": task_order,
            "round_count": 2,
            "current_round": 1,
            "formal_campaign": "not_started",
            # The public sidecar needs the frozen R012 selection rules, but it
            # must read them from this versioned C-only state rather than from
            # an A/B/C or historical lifecycle file.
            "retrieval_profile": deepcopy(config["retrieval_profile"]),
            "profile_sha256": _r012_profile_sha256(config["retrieval_profile"]),
            "global_operations": [],
            "rounds": {"1": _empty_round(1, task_order), "2": _empty_round(2, task_order)},
        }
        protocol = cls(config=config, baseline_manifest=baseline, state=state, config_path=config_path, baseline_path=baseline_path)
        if state_path is not None:
            protocol.save(state_path)
        return protocol

    @classmethod
    def load(cls, state_path: Path, config_path: Path, baseline_path: Path | None = None) -> "COnlyProtocol":
        state = read_json(state_path)
        config = read_json(config_path)
        stated_baseline = _object(state.get("baseline_manifest"), field="state.baseline_manifest")
        selected_baseline_path = baseline_path or Path(_nonempty(stated_baseline.get("path"), field="state.baseline_manifest.path"))
        baseline = read_json(selected_baseline_path)
        expected_config_hash = _sha256_hex(_object(state.get("config"), field="state.config").get("sha256"), field="state.config.sha256")
        if expected_config_hash != sha256_file(config_path):
            raise COnlyProtocolError("C-only state config hash does not match the supplied config")
        expected_baseline_hash = _sha256_hex(stated_baseline.get("sha256"), field="state.baseline_manifest.sha256")
        if expected_baseline_hash != sha256_file(selected_baseline_path):
            raise COnlyProtocolError("C-only state baseline hash does not match the supplied baseline manifest")
        configured_baseline = _object(config.get("baseline_manifest"), field="baseline_manifest")
        if _sha256_hex(configured_baseline.get("sha256"), field="baseline_manifest.sha256") != sha256_file(selected_baseline_path):
            raise COnlyProtocolError("C-only config baseline manifest hash does not match the supplied baseline")
        return cls(config=config, baseline_manifest=baseline, state=state, config_path=config_path, baseline_path=selected_baseline_path)

    def save(self, path: Path) -> None:
        self._validate_state()
        value = deepcopy(self.state)
        value["updated_at_utc"] = utc_now()
        write_json(path, value)

    @property
    def current_round_id(self) -> int:
        return int(self.state["current_round"])

    @property
    def current_task_id(self) -> str | None:
        round_state = self._round()
        index = int(round_state["next_task_index"])
        return round_state["task_order"][index] if index < len(round_state["task_order"]) else None

    def _round(self, round_id: int | None = None) -> dict[str, Any]:
        selected = self.current_round_id if round_id is None else round_id
        value = self.state.get("rounds", {}).get(str(selected))
        if not isinstance(value, dict):
            raise COnlyProtocolError(f"round {selected} has no durable state")
        return value

    def _task(self, task_id: str) -> dict[str, Any]:
        canonical = _canonical_task(task_id, field="task_id")
        for item in self.tasks:
            if item["canonical_instance_id"] == canonical:
                return item
        raise COnlyProtocolError(f"unknown configured task: {canonical}")

    def _assignment(self, task_id: str, *, round_id: int | None = None) -> dict[str, Any]:
        canonical = self._task(task_id)["canonical_instance_id"]
        value = self._round(round_id)["assignments"].get(canonical)
        if not isinstance(value, dict):
            raise COnlyProtocolError(f"{canonical}: task has not been frozen in this round")
        return value

    @staticmethod
    def _replay(operations: list[dict[str, Any]], operation_id: str, payload: Any) -> Any | None:
        digest = sha256_text(canonical_json(payload))
        for operation in operations:
            if operation.get("operation_id") != operation_id:
                continue
            if operation.get("payload_sha256") != digest:
                raise COnlyProtocolError(f"operation {operation_id} was retried with different input")
            return deepcopy(operation.get("result"))
        return None

    @staticmethod
    def _journal(operations: list[dict[str, Any]], operation_id: str, payload: Any, result: Any) -> None:
        operations.append(
            {
                "operation_id": operation_id,
                "payload_sha256": sha256_text(canonical_json(payload)),
                "created_at_utc": utc_now(),
                "result": deepcopy(result),
            }
        )

    def _replay_any(self, operation_id: str, payload: Any) -> Any | None:
        """Find an idempotent result even after the sequential cursor advanced."""
        for round_state in self.state.get("rounds", {}).values():
            if isinstance(round_state, dict):
                result = self._replay(round_state.get("operations", []), operation_id, payload)
                if result is not None:
                    return result
        return self._replay(self.state.get("global_operations", []), operation_id, payload)

    def _assert_current_task(self, task_id: str, *, round_state: dict[str, Any]) -> str:
        canonical = self._task(task_id)["canonical_instance_id"]
        if canonical != self.current_task_id:
            raise COnlyProtocolError(f"{canonical}: current sequential task is {self.current_task_id}")
        return canonical

    def freeze_task(self, task_id: str, *, operation_id: str | None = None) -> dict[str, Any]:
        round_state = self._round()
        canonical = self._task(task_id)["canonical_instance_id"]
        operation_id = operation_id or f"freeze:r{self.current_round_id}:{canonical}"
        payload = {"round_id": self.current_round_id, "task_id": canonical}
        replay = self._replay_any(operation_id, payload)
        if replay is not None:
            return replay
        canonical = self._assert_current_task(canonical, round_state=round_state)
        if canonical in round_state["assignments"]:
            raise COnlyProtocolError(f"{canonical}: task assignment exists without a replayable freeze operation")
        bank = SkillBank.from_dict(round_state["bank"])
        snapshot = bank.snapshot()
        if int(round_state["next_task_index"]) == 0 and snapshot["skills"]:
            raise COnlyProtocolError("the first C-only task must freeze an empty skill bank")
        trial_id = f"r{self.current_round_id}:C:{canonical}"
        assignment = {
            "trial_id": trial_id,
            "round_id": self.current_round_id,
            "condition": "C-only",
            "task_id": canonical,
            "task_name": self._task(canonical)["task_name"],
            "status": "pending",
            # Assignments carry the immutable public snapshot, matching the
            # R012 sidecar retrieval contract.  The round bank retains the
            # full operation journal separately; exposing it here would make
            # ``states``/``operations`` look like mutable current material to
            # a solver and would not provide the snapshot's state_sha256.
            "frozen_bank": deepcopy(snapshot),
            "frozen_bank_state_sha256": snapshot["state_sha256"],
            "frozen_skill_ids": sorted(skill.get("skill_id") for skill in snapshot["skills"] if isinstance(skill, dict) and isinstance(skill.get("skill_id"), str)),
            "historical_baseline_used": False,
        }
        round_state["assignments"][canonical] = assignment
        self._journal(round_state["operations"], operation_id, payload, assignment)
        return deepcopy(assignment)

    def frozen_bank(self, task_id: str) -> SkillBank:
        assignment = self._assignment(task_id)
        bank = SkillBank.from_dict(assignment["frozen_bank"])
        if bank.snapshot()["state_sha256"] != assignment["frozen_bank_state_sha256"]:
            raise COnlyProtocolError(f"{task_id}: frozen bank snapshot hash changed")
        return bank

    def eligible_skills(self, task_id: str, granularity: str) -> list[dict[str, Any]]:
        return self.frozen_bank(task_id).eligible(instance_id=self._task(task_id)["canonical_instance_id"], granularity=granularity)

    def record_trial(
        self,
        task_id: str,
        *,
        outcome: str,
        trajectory: dict[str, Any] | None,
        supplied_skills: Iterable[dict[str, Any]] = (),
        raw_evidence: dict[str, Any],
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        round_state = self._round()
        canonical = self._task(task_id)["canonical_instance_id"]
        operation_id = operation_id or f"trial:r{self.current_round_id}:{canonical}"
        supplied_list = [deepcopy(item) for item in supplied_skills]
        payload = {
            "round_id": self.current_round_id,
            "task_id": canonical,
            "outcome": outcome,
            "trajectory": trajectory,
            "supplied_skills": supplied_list,
            "raw_evidence": raw_evidence,
        }
        replay = self._replay_any(operation_id, payload)
        if replay is not None:
            return replay
        canonical = self._assert_current_task(canonical, round_state=round_state)
        assignment = self._assignment(canonical)
        if assignment.get("status") != "pending":
            raise COnlyProtocolError(f"{canonical}: trial is already recorded without a replayable operation")
        if outcome not in {"completed", "infra_failure"}:
            raise COnlyProtocolError("C-only trial outcome must be completed or infra_failure")
        evidence = _object(raw_evidence, field="raw_evidence")
        _reject_historical_material(evidence)
        _validate_embedded_hash_refs(evidence, field="raw_evidence")
        if evidence.get("condition", "C-only") != "C-only" or evidence.get("round_id", self.current_round_id) != self.current_round_id or evidence.get("task_id", canonical) != canonical:
            raise COnlyProtocolError("trial evidence is not bound to this C-only round/task")
        if evidence.get("historical_baseline_used") is True or evidence.get("baseline_imported") is True:
            raise COnlyProtocolError("historical baseline material cannot enter a C-only solver trial")
        if evidence.get("official_harbor_trial") is not True:
            raise COnlyProtocolError("C-only trial evidence must identify the official Harbor solver/verifier boundary")
        evidence_trial_id = _nonempty(evidence.get("trial_id"), field="raw_evidence.trial_id")
        if evidence_trial_id != assignment["trial_id"]:
            raise COnlyProtocolError("trial evidence trial_id differs from the frozen assignment")
        evidence_session_id = _nonempty(evidence.get("session_id"), field="raw_evidence.session_id")
        normalized_trajectory: dict[str, Any] | None = None
        if outcome == "completed":
            normalized_trajectory = _object(trajectory, field="trajectory")
            _reject_historical_material(normalized_trajectory, path="trajectory")
            normalized_trajectory = _validate_trajectory_ref(
                normalized_trajectory,
                field="trajectory",
                round_id=self.current_round_id,
                task_id=canonical,
                trial_id=assignment["trial_id"],
            )
            if normalized_trajectory["session_id"] != evidence_session_id:
                raise COnlyProtocolError("raw_evidence.session_id differs from trajectory.session_id")
        elif trajectory is not None:
            raise COnlyProtocolError("infrastructure failures cannot claim a complete trajectory")
        frozen = self.frozen_bank(canonical)
        frozen_skills = {(skill.get("skill_id"), skill.get("version")): skill for skill in frozen.snapshot()["skills"] if isinstance(skill, dict)}
        for index, supplied in enumerate(supplied_list):
            item = _object(supplied, field=f"supplied_skills[{index}]")
            _reject_historical_material(item, path=f"supplied_skills[{index}]")
            skill_id = _nonempty(item.get("skill_id"), field=f"supplied_skills[{index}].skill_id")
            version = item.get("version")
            if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
                raise COnlyProtocolError(f"supplied_skills[{index}].version must be a positive integer")
            if (skill_id, version) not in frozen_skills:
                raise COnlyProtocolError(f"supplied skill {skill_id}:{version} was not in the frozen C-only bank")
            if item.get("source") in {"baseline", "historical", "old_bank"} or item.get("round_id") not in {None, self.current_round_id}:
                raise COnlyProtocolError("supplied skill source is outside the current C-only round")
        normalized = {
            "condition": "C-only",
            "round_id": self.current_round_id,
            "task_id": canonical,
            "trial_id": assignment["trial_id"],
            "outcome": outcome,
            "trajectory": normalized_trajectory,
            "supplied_skills": supplied_list,
            "raw_evidence": deepcopy(evidence),
            "historical_baseline_used": False,
        }
        assignment["status"] = "finished"
        assignment["trial_evidence"] = normalized
        self._journal(round_state["operations"], operation_id, payload, normalized)
        return deepcopy(normalized)

    def _available_pairing_tasks(self, round_state: dict[str, Any], current_task: str) -> set[str]:
        available = {
            task_id
            for task_id, record in round_state["completed_tasks"].items()
            if isinstance(record, dict)
            and record.get("outcome") == "completed"
            and isinstance(record.get("trajectory"), dict)
        }
        assignment = round_state["assignments"].get(current_task)
        if isinstance(assignment, dict) and assignment.get("status") == "finished" and isinstance(assignment.get("trial_evidence"), dict) and assignment["trial_evidence"].get("outcome") == "completed":
            available.add(current_task)
        return available

    def _validate_skill_candidate(self, raw: Any, *, task_id: str, candidate_field: str) -> tuple[dict[str, Any], str]:
        candidate = validate_skill_candidate(_object(raw, field=candidate_field))
        if candidate.get("benchmark") != "terminal-bench":
            raise COnlyProtocolError(f"{candidate_field}.benchmark must be terminal-bench")
        _reject_historical_material(candidate, path=candidate_field)
        provenance = _object(candidate.get("provenance", {}), field=f"{candidate_field}.provenance")
        sources = provenance.get("source_instance_ids", [])
        if not isinstance(sources, list) or task_id not in {_canonical_task(item, field="candidate source") for item in sources}:
            raise COnlyProtocolError(f"{candidate_field} must cite the current C-only task as a source")
        raw_sources = provenance.get("source_instance_ids_raw", [])
        if raw_sources and not isinstance(raw_sources, list):
            raise COnlyProtocolError(f"{candidate_field}.provenance.source_instance_ids_raw must be a list")
        for source in raw_sources if isinstance(raw_sources, list) else []:
            if not isinstance(source, str) or source.startswith("historical:") or source.startswith("baseline:"):
                raise COnlyProtocolError(f"{candidate_field} has a historical source")
        return candidate, sha256_text(canonical_json(candidate))

    def _validate_pairing(
        self,
        pairing: Any,
        *,
        task_id: str,
        round_state: dict[str, Any],
        current_task_candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        value = _object(pairing, field="candidate.pairing")
        source_tasks = value.get("source_task_ids")
        if not isinstance(source_tasks, list) or not 2 <= len(source_tasks) <= 3:
            raise COnlyProtocolError("task skill pairing needs 2 to 3 current-round task IDs")
        normalized = [_canonical_task(item, field="pairing.source_task_ids") for item in source_tasks]
        if len(set(normalized)) != len(normalized) or task_id not in normalized:
            raise COnlyProtocolError("task skill pairing must use distinct tasks and include the current task")
        available = self._available_pairing_tasks(round_state, task_id)
        if not set(normalized) <= available:
            raise COnlyProtocolError("task skill pairing cites a task without a completed current-round trajectory")
        pooled_tasks = {item.get("task_id") for item in round_state["trajectory_pool"] if isinstance(item, dict)}
        pooled_tasks.add(task_id)
        if any(source_task not in pooled_tasks for source_task in normalized):
            raise COnlyProtocolError("task skill pairing needs the current-round trajectory pool references")
        # A task pairing is only auditable when each cited trajectory is the
        # exact file already registered by this round.  Optional refs allowed
        # callers to relabel a historical file while retaining only a task ID;
        # production C-only records therefore require the complete bound refs.
        refs = value.get("trajectory_refs")
        if not isinstance(refs, list) or len(refs) != len(normalized):
            raise COnlyProtocolError("task skill pairing requires one trajectory_ref per source task")
        pool_by_task: dict[str, dict[str, Any]] = {}
        for raw_pool in round_state["trajectory_pool"]:
            pool_item = _object(raw_pool, field="trajectory_pool item")
            pool_task = _canonical_task(pool_item.get("task_id"), field="trajectory_pool.task_id")
            if pool_task in pool_by_task:
                raise COnlyProtocolError("current-round trajectory pool has duplicate task references")
            pool_by_task[pool_task] = pool_item
        # The current task is appended to the pool only after extraction, so
        # expose its already-recorded completed trial as a temporary exact ref.
        current_assignment = self._assignment(task_id)
        current_trial = _object(current_assignment.get("trial_evidence"), field="current trial evidence")
        current_trajectory = current_trial.get("trajectory")
        if isinstance(current_trajectory, dict):
            pool_by_task.setdefault(task_id, current_trajectory)
        normalized_refs: list[dict[str, Any]] = []
        for index, (source_task, raw_ref) in enumerate(zip(normalized, refs, strict=True)):
            expected = pool_by_task.get(source_task)
            if expected is None:
                raise COnlyProtocolError(f"pairing source task {source_task} has no registered current-round trajectory")
            ref = _validate_trajectory_ref(
                raw_ref,
                field=f"pairing.trajectory_refs[{index}]",
                round_id=self.current_round_id,
                task_id=source_task,
                trial_id=_nonempty(expected.get("trial_id"), field=f"trajectory_pool[{source_task}].trial_id"),
            )
            if ref.get("path") != expected.get("path") or ref.get("sha256") != expected.get("sha256"):
                raise COnlyProtocolError("pairing trajectory reference must match the exact current-round pool path and hash")
            normalized_refs.append(ref)
        source_candidate_ids = value.get("source_candidate_ids")
        source_candidate_fingerprints = value.get("source_candidate_fingerprints")
        if not isinstance(source_candidate_ids, list) or len(source_candidate_ids) != len(normalized):
            raise COnlyProtocolError("task skill pairing requires one candidate ID per source task")
        if not isinstance(source_candidate_fingerprints, list) or len(source_candidate_fingerprints) != len(normalized):
            raise COnlyProtocolError("task skill pairing requires one candidate fingerprint per source task")
        candidate_by_task: dict[str, dict[str, Any]] = {}
        for raw_candidate in round_state.get("task_candidate_pool", []):
            candidate_record = _object(raw_candidate, field="task_candidate_pool item")
            candidate_task = _canonical_task(candidate_record.get("task_id"), field="task_candidate_pool.task_id")
            if candidate_task in candidate_by_task:
                raise COnlyProtocolError("current-round task candidate pool has duplicate tasks")
            candidate_by_task[candidate_task] = candidate_record
        if len(current_task_candidates) == 1:
            candidate_by_task[task_id] = _object(current_task_candidates[0], field="current task candidate")
        normalized_candidate_ids: list[str] = []
        normalized_candidate_fingerprints: list[str] = []
        for index, source_task in enumerate(normalized):
            expected_candidate = candidate_by_task.get(source_task)
            if expected_candidate is None:
                raise COnlyProtocolError(f"pairing source task {source_task} has no registered SOP candidate")
            candidate_id = _nonempty(source_candidate_ids[index], field=f"pairing.source_candidate_ids[{index}]")
            fingerprint = _sha256_hex(source_candidate_fingerprints[index], field=f"pairing.source_candidate_fingerprints[{index}]")
            if candidate_id != expected_candidate.get("candidate_id") or fingerprint != expected_candidate.get("candidate_fingerprint"):
                raise COnlyProtocolError("pairing must cite the exact SOP candidate for every source task")
            normalized_candidate_ids.append(candidate_id)
            normalized_candidate_fingerprints.append(fingerprint)
        return {
            "source_task_ids": normalized,
            "source_candidate_ids": normalized_candidate_ids,
            "source_candidate_fingerprints": normalized_candidate_fingerprints,
            "trajectory_refs": normalized_refs,
        }

    def record_event_attempt(
        self,
        task_id: str,
        *,
        attempt_no: int,
        outcome: str,
        candidate: dict[str, Any] | None,
        raw_response: Any,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        canonical = self._task(task_id)["canonical_instance_id"]
        operation_id = operation_id or f"event:r{self.current_round_id}:{canonical}:{attempt_no}"
        payload = {"round_id": self.current_round_id, "task_id": canonical, "attempt_no": attempt_no, "outcome": outcome, "candidate": candidate, "raw_response": raw_response}
        replay = self._replay_any(operation_id, payload)
        if replay is not None:
            return replay
        event_settings = self.config["protocol"]["event_extraction"]
        max_attempts = event_settings.get("max_initial_attempts")
        if isinstance(attempt_no, bool) or not isinstance(attempt_no, int) or attempt_no < 1:
            raise COnlyProtocolError("event extraction attempt number must be positive")
        if max_attempts is not None and attempt_no > max_attempts:
            raise COnlyProtocolError(f"event extraction allows only attempts 1 through {max_attempts}")
        round_state = self._round()
        canonical = self._assert_current_task(canonical, round_state=round_state)
        assignment = self._assignment(canonical)
        if assignment.get("status") != "finished" or not isinstance(assignment.get("trial_evidence"), dict) or assignment["trial_evidence"].get("outcome") != "completed":
            raise COnlyProtocolError("event extraction needs a completed C-only trial")
        attempts = round_state["event_attempts"].setdefault(canonical, [])
        if not isinstance(attempts, list):
            raise COnlyProtocolError("event attempt journal is malformed")
        if len(attempts) + 1 != attempt_no:
            raise COnlyProtocolError("event attempts must be recorded in order without gaps")
        if attempts and attempts[-1].get("stopped") is True:
            raise COnlyProtocolError("event extraction stopped after skip/duplicate; later attempts are forbidden")
        if outcome not in {"generated", "invalid", "skip", "duplicate"}:
            raise COnlyProtocolError("event attempt outcome must be generated, invalid, skip, or duplicate")
        normalized_candidate = None
        fingerprint = None
        if outcome == "generated":
            normalized_candidate, fingerprint = self._validate_skill_candidate(candidate, task_id=canonical, candidate_field="event_attempt.candidate")
            if normalized_candidate["granularity"] != "event":
                raise COnlyProtocolError("event extraction candidate must have granularity=event")
            if any(item.get("candidate_fingerprint") == fingerprint for item in attempts):
                raise COnlyProtocolError("a canonical duplicate must be recorded as duplicate and stop extraction")
        elif outcome == "duplicate":
            if candidate is not None:
                normalized_candidate, fingerprint = self._validate_skill_candidate(candidate, task_id=canonical, candidate_field="event_attempt.candidate")
                if normalized_candidate["granularity"] != "event":
                    raise COnlyProtocolError("duplicate event candidate must have granularity=event")
            prior_fingerprints = {item.get("candidate_fingerprint") for item in attempts if isinstance(item, dict) and item.get("candidate_fingerprint")}
            if not attempts or (fingerprint is not None and fingerprint not in prior_fingerprints) or (fingerprint is None and not prior_fingerprints):
                raise COnlyProtocolError("duplicate event outcome needs a prior candidate")
        elif outcome in {"skip", "invalid"} and candidate is not None:
            raise COnlyProtocolError(f"{outcome} event outcome cannot carry a candidate")
        _reject_historical_material(raw_response, path="event_attempt.raw_response")
        _validate_embedded_hash_refs(raw_response, field="event_attempt.raw_response")
        record = {
            "attempt_no": attempt_no,
            "outcome": outcome,
            "candidate": normalized_candidate,
            "candidate_fingerprint": fingerprint,
            "raw_response": deepcopy(raw_response),
            "stopped": outcome in event_settings["stop_on"],
        }
        attempts.append(record)
        self._journal(round_state["operations"], operation_id, payload, record)
        return deepcopy(record)

    def extract_after_task(
        self,
        task_id: str,
        *,
        candidates: Iterable[dict[str, Any]],
        trajectory_ref: dict[str, Any] | None,
        extraction_evidence: dict[str, Any],
        description_records: Iterable[dict[str, Any]] = (),
        task_candidate_records: Iterable[dict[str, Any]] = (),
        decision: str = "extract",
        reason: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        canonical = self._task(task_id)["canonical_instance_id"]
        operation_id = operation_id or f"extract:r{self.current_round_id}:{canonical}"
        candidate_list = [deepcopy(item) for item in candidates]
        descriptions = [deepcopy(item) for item in description_records]
        task_candidates = [deepcopy(item) for item in task_candidate_records]
        payload = {
            "round_id": self.current_round_id,
            "task_id": canonical,
            "candidates": candidate_list,
            "trajectory_ref": trajectory_ref,
            "extraction_evidence": extraction_evidence,
            "description_records": descriptions,
            "task_candidate_records": task_candidates,
            "decision": decision,
            "reason": reason,
        }
        replay = self._replay_any(operation_id, payload)
        if replay is not None:
            return replay
        round_state = self._round()
        canonical = self._assert_current_task(canonical, round_state=round_state)
        assignment = self._assignment(canonical)
        if assignment.get("status") != "finished":
            raise COnlyProtocolError("extraction needs finished C-only trial evidence")
        if "extraction" in assignment:
            raise COnlyProtocolError("task extraction is already durable; automatic re-extraction is forbidden")
        evidence = _object(extraction_evidence, field="extraction_evidence")
        _reject_historical_material(evidence)
        _validate_embedded_hash_refs(evidence, field="extraction_evidence")
        if decision not in {"extract", "skip"}:
            raise COnlyProtocolError("extraction decision must be extract or skip")
        if decision == "skip" and (candidate_list or reason is None or not reason.strip()):
            raise COnlyProtocolError("a skipped publication extraction needs a reason and no publishable candidates")
        trial = _object(assignment.get("trial_evidence"), field="trial_evidence")
        expected_trajectory: dict[str, Any] | None = None
        if trial.get("outcome") == "infra_failure":
            if candidate_list or trajectory_ref is not None or descriptions or task_candidates or decision != "skip":
                raise COnlyProtocolError("an infrastructure-failed task cannot publish extraction candidates")
        else:
            expected_trajectory = _object(trial.get("trajectory"), field="trial_evidence.trajectory")
            actual_trajectory = _object(trajectory_ref, field="trajectory_ref")
            if canonical_json(expected_trajectory) != canonical_json(actual_trajectory):
                raise COnlyProtocolError("extraction trajectory must be the exact completed C-only trajectory reference")
            if actual_trajectory.get("round_id") != self.current_round_id or _canonical_task(actual_trajectory.get("task_id"), field="trajectory_ref.task_id") != canonical:
                raise COnlyProtocolError("extraction trajectory is not bound to the current C-only task")
        available = self._available_pairing_tasks(round_state, canonical)
        normalized_candidates: list[dict[str, Any]] = []
        candidate_fingerprints: set[str] = set()
        candidate_ids: set[str] = set()
        for index, raw in enumerate(candidate_list):
            item = _object(raw, field=f"candidates[{index}]")
            skill, fingerprint = self._validate_skill_candidate(item.get("skill"), task_id=canonical, candidate_field=f"candidates[{index}].skill")
            candidate_id = _nonempty(item.get("candidate_id", "candidate-" + fingerprint[:16]), field=f"candidates[{index}].candidate_id")
            _reject_historical_material(item.get("raw"), path=f"candidates[{index}].raw")
            if fingerprint in candidate_fingerprints:
                raise COnlyProtocolError("duplicate extraction candidate in one task")
            if candidate_id in candidate_ids:
                raise COnlyProtocolError("extraction candidate IDs must be unique in one task")
            candidate_fingerprints.add(fingerprint)
            candidate_ids.add(candidate_id)
            if skill["granularity"] == "task":
                if len(available) < 2:
                    raise COnlyProtocolError("task skill extraction must skip until two current-round tasks are available")
                pairing = self._validate_pairing(
                    item.get("pairing"),
                    task_id=canonical,
                    round_state=round_state,
                    current_task_candidates=task_candidates,
                )
            else:
                pairing = None
            normalized_candidates.append(
                {
                    "candidate_id": candidate_id,
                    "candidate_fingerprint": fingerprint,
                    "skill": skill,
                    "pairing": pairing,
                    "source": {"round_id": self.current_round_id, "task_id": canonical, "trajectory": deepcopy(trajectory_ref)},
                    "raw": deepcopy(item.get("raw")),
                }
            )
        for index, raw in enumerate(descriptions):
            item = _object(raw, field=f"description_records[{index}]")
            if _canonical_task(item.get("task_id"), field="description_records.task_id") != canonical or item.get("round_id") != self.current_round_id:
                raise COnlyProtocolError("description pool accepts only the current C-only task")
            if expected_trajectory is None:
                raise COnlyProtocolError("description records require a completed C-only trajectory")
            # A D01 description is useful for D02 only when the value, its
            # durable manager response, and the exact source trajectory are
            # all bound together.  A caller cannot register a made-up hash
            # or relabel a response from another task/round.
            response_ref = _verified_hash_ref(item, field=f"description_records[{index}]")
            response_sha = _sha256_hex(
                item.get("response_sha256", response_ref["sha256"]),
                field=f"description_records[{index}].response_sha256",
            )
            if response_sha != response_ref["sha256"]:
                raise COnlyProtocolError("description response_sha256 differs from its durable response file")
            value = _object(item.get("value"), field=f"description_records[{index}].value")
            value_sha = _sha256_hex(item.get("value_sha256"), field=f"description_records[{index}].value_sha256")
            if value_sha != sha256_text(canonical_json(value)):
                raise COnlyProtocolError("description value_sha256 does not match the validated D01 value")
            trajectory = _validate_trajectory_ref(
                item.get("trajectory_ref"),
                field=f"description_records[{index}].trajectory_ref",
                round_id=self.current_round_id,
                task_id=canonical,
                trial_id=_nonempty(expected_trajectory.get("trial_id"), field="trial_evidence.trajectory.trial_id"),
            )
            if canonical_json(trajectory) != canonical_json(expected_trajectory):
                raise COnlyProtocolError("description trajectory_ref must be the exact completed C-only trajectory")
            _reject_historical_material(item)
        if len(task_candidates) > 1:
            raise COnlyProtocolError("one completed trace can register at most one isolated task candidate")
        normalized_task_candidates: list[dict[str, Any]] = []
        for index, raw in enumerate(task_candidates):
            item = _object(raw, field=f"task_candidate_records[{index}]")
            if item.get("status") != "validated" or item.get("source") != "current_round_c_only":
                raise COnlyProtocolError("task candidate pool accepts only validated current-round records")
            if item.get("round_id") != self.current_round_id or _canonical_task(item.get("task_id"), field="task_candidate.task_id") != canonical:
                raise COnlyProtocolError("task candidate record is outside the current round/task")
            for field in ("trial_id", "session_id"):
                if item.get(field) != expected_trajectory.get(field):
                    raise COnlyProtocolError(f"task candidate {field} differs from its completed trajectory")
            candidate_trajectory = _validate_trajectory_ref(
                item.get("trajectory_ref"),
                field=f"task_candidate_records[{index}].trajectory_ref",
                round_id=self.current_round_id,
                task_id=canonical,
                trial_id=_nonempty(expected_trajectory.get("trial_id"), field="trial_evidence.trajectory.trial_id"),
            )
            if canonical_json(candidate_trajectory) != canonical_json(expected_trajectory):
                raise COnlyProtocolError("task candidate trajectory_ref must be the exact completed C-only trajectory")
            skill, fingerprint = self._validate_skill_candidate(
                item.get("skill"),
                task_id=canonical,
                candidate_field=f"task_candidate_records[{index}].skill",
            )
            if skill.get("granularity") != "task":
                raise COnlyProtocolError("isolated task candidate must have granularity=task")
            provenance = _object(skill.get("provenance"), field="task_candidate.skill.provenance")
            sources = provenance.get("source_instance_ids")
            if not isinstance(sources, list) or {_canonical_task(source, field="task_candidate source") for source in sources} != {canonical}:
                raise COnlyProtocolError("isolated task candidate must cite exactly its own task")
            if _sha256_hex(item.get("candidate_fingerprint"), field="task_candidate.candidate_fingerprint") != fingerprint:
                raise COnlyProtocolError("task candidate fingerprint differs from its skill")
            _nonempty(item.get("candidate_id"), field="task_candidate.candidate_id")
            _object(item.get("candidate_context"), field="task_candidate.candidate_context")
            _object(item.get("evidence"), field="task_candidate.evidence")
            raw_evidence = _object(item.get("raw"), field="task_candidate.raw")
            _validate_embedded_hash_refs(raw_evidence, field="task_candidate.raw")
            _reject_historical_material(item, path="task_candidate")
            normalized_task_candidates.append(deepcopy(item))
        attempts = deepcopy(round_state["event_attempts"].get(canonical, []))
        record = {
            "round_id": self.current_round_id,
            "task_id": canonical,
            "decision": decision,
            "reason": reason,
            "trajectory_ref": deepcopy(trajectory_ref),
            "candidates": normalized_candidates,
            "event_attempts": attempts,
            "description_records": descriptions,
            "task_candidate_records": normalized_task_candidates,
            "evidence": deepcopy(evidence),
            "historical_baseline_used": False,
        }
        assignment["extraction"] = record
        if trial.get("outcome") == "completed":
            round_state["trajectory_pool"].append(
                {
                    "round_id": self.current_round_id,
                    "task_id": canonical,
                    "trial_id": trial.get("trial_id"),
                    "session_id": trajectory_ref.get("session_id"),
                    "path": trajectory_ref["path"],
                    "sha256": trajectory_ref["sha256"],
                    "source": "current_round_c_only",
                }
            )
            round_state["description_pool"].extend(
                {
                    **deepcopy(item),
                    "round_id": self.current_round_id,
                    "task_id": canonical,
                    "source": "current_round_c_only",
                }
                for item in descriptions
            )
            round_state["task_candidate_pool"].extend(deepcopy(normalized_task_candidates))
        self._journal(round_state["operations"], operation_id, payload, record)
        return deepcopy(record)

    def publish_after_task(
        self,
        task_id: str,
        *,
        operations: Iterable[dict[str, Any]],
        manager_decisions: Iterable[dict[str, Any]] = (),
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        canonical = self._task(task_id)["canonical_instance_id"]
        operation_id = operation_id or f"publish:r{self.current_round_id}:{canonical}"
        operation_list = [deepcopy(item) for item in operations]
        decision_list = [deepcopy(item) for item in manager_decisions]
        payload = {"round_id": self.current_round_id, "task_id": canonical, "operations": operation_list, "manager_decisions": decision_list}
        replay = self._replay_any(operation_id, payload)
        if replay is not None:
            return replay
        round_state = self._round()
        canonical = self._assert_current_task(canonical, round_state=round_state)
        assignment = self._assignment(canonical)
        if assignment.get("status") != "finished" or not isinstance(assignment.get("trial_evidence"), dict):
            raise COnlyProtocolError("publication needs finished C-only trial evidence")
        extraction = _object(assignment.get("extraction"), field="assignment.extraction")
        if "publication" in assignment:
            raise COnlyProtocolError("task publication is already durable; automatic republish is forbidden")
        supplied = assignment["trial_evidence"].get("supplied_skills", [])
        if not isinstance(supplied, list):
            raise COnlyProtocolError("trial supplied_skills must be a list")
        supplied_identities = {(item.get("skill_id"), item.get("version")) for item in supplied if isinstance(item, dict)}
        decisions_by_identity: dict[tuple[Any, Any], dict[str, Any]] = {}
        maintenance_operation_by_identity: dict[tuple[Any, Any], str] = {}
        extraction_candidates: dict[str, dict[str, Any]] = {}
        seen_candidates: set[str] = set()
        bank = SkillBank.from_dict(round_state["bank"])
        staged = SkillBank.from_dict(bank.to_dict())
        normalized_operations: list[dict[str, Any]] = []
        try:
            for index, decision in enumerate(decision_list):
                item = _object(decision, field=f"manager_decisions[{index}]")
                identity = (item.get("skill_id"), item.get("version"))
                if identity in decisions_by_identity:
                    raise COnlyProtocolError("manager decision identities must be unique")
                decisions_by_identity[identity] = item
                if item.get("action") not in {"evolve", "skip"}:
                    raise COnlyProtocolError("manager decisions must choose evolve or skip")
                _nonempty(item.get("reason"), field=f"manager_decisions[{index}].reason")
                decision_hash = _sha256_hex(item.get("manager_response_sha256"), field=f"manager_decisions[{index}].manager_response_sha256")
                decision_ref = _verified_hash_ref(
                    {"path": item.get("manager_response_path"), "sha256": decision_hash},
                    field=f"manager_decisions[{index}].manager_response",
                )
                if decision_ref["sha256"] != decision_hash:
                    raise COnlyProtocolError("manager decision response reference hash changed")
                if identity not in supplied_identities:
                    raise COnlyProtocolError("manager decision cites a skill that C did not actually supply")
                _reject_historical_material(item)
            if set(decisions_by_identity) != supplied_identities:
                raise COnlyProtocolError("every skill actually supplied by C needs exactly one Fig.8 decision")
            raw_extraction_candidates = extraction.get("candidates", [])
            if not isinstance(raw_extraction_candidates, list):
                raise COnlyProtocolError("assignment.extraction.candidates must be a list")
            for index, raw_candidate in enumerate(raw_extraction_candidates):
                item = _object(raw_candidate, field=f"assignment.extraction.candidates[{index}]")
                candidate_id = _nonempty(item.get("candidate_id"), field=f"assignment.extraction.candidates[{index}].candidate_id")
                if candidate_id in extraction_candidates:
                    raise COnlyProtocolError("extracted candidate IDs must be unique")
                extraction_candidates[candidate_id] = item
            for index, raw in enumerate(operation_list):
                item = _object(raw, field=f"operations[{index}]")
                op_id = _nonempty(item.get("operation_id"), field=f"operations[{index}].operation_id")
                source_kind = item.get("source_kind")
                if source_kind not in {"extraction", "maintenance"}:
                    raise COnlyProtocolError("publication source_kind must be extraction or maintenance")
                candidate, fingerprint = self._validate_skill_candidate(item.get("candidate"), task_id=canonical, candidate_field=f"operations[{index}].candidate")
                candidate_id = item.get("candidate_id")
                supplied_identity = None
                if source_kind == "extraction":
                    if candidate_id not in extraction_candidates:
                        raise COnlyProtocolError("extraction publication operation cites an unknown candidate")
                    if candidate_id in seen_candidates:
                        raise COnlyProtocolError("an extracted candidate can receive only one Fig.9 publication decision")
                    expected = extraction_candidates[candidate_id]
                    seen_candidates.add(candidate_id)
                else:
                    skill_id = _nonempty(item.get("supplied_skill_id"), field=f"operations[{index}].supplied_skill_id")
                    version = item.get("supplied_skill_version")
                    supplied_identity = (skill_id, version)
                    if supplied_identity not in supplied_identities or decisions_by_identity[supplied_identity].get("action") != "evolve":
                        raise COnlyProtocolError("maintenance operation must evolve a skill actually supplied by C")
                    if supplied_identity in maintenance_operation_by_identity:
                        raise COnlyProtocolError("each Fig.8 evolve decision must have exactly one Fig.9 maintenance operation")
                    maintenance_operation_by_identity[supplied_identity] = op_id
                source_ids = item.get("source_instance_ids", [canonical])
                if not isinstance(source_ids, list) or canonical not in {_canonical_task(value, field="operation.source_instance_ids") for value in source_ids}:
                    raise COnlyProtocolError("publication operation must preserve the current task source")
                evidence = _object(item.get("evidence"), field=f"operations[{index}].evidence")
                _reject_historical_material(evidence)
                manager_hash = _sha256_hex(
                    evidence.get("manager_response_sha256", evidence.get("fig9_response_sha256")),
                    field=f"operations[{index}].evidence.manager_response_sha256",
                )
                manager_ref = _verified_hash_ref(
                    {"path": evidence.get("manager_response_path", evidence.get("fig9_response_path")), "sha256": manager_hash},
                    field=f"operations[{index}].evidence.manager_response",
                )
                if manager_ref["sha256"] != manager_hash:
                    raise COnlyProtocolError("publication manager response reference hash changed")
                # Fig.9 is allowed to change the skill content (especially a
                # merge), but the candidate that entered Fig.9 must remain a
                # separately bound immutable value.  Comparing the final
                # candidate to the extraction output would reject legitimate
                # merges; accepting only a caller-supplied fingerprint would
                # permit relabelling.  Validate both objects and their exact
                # raw evidence at the publication edge.
                original_candidate, original_fingerprint = self._validate_skill_candidate(
                    item.get("original_candidate"),
                    task_id=canonical,
                    candidate_field=f"operations[{index}].original_candidate",
                )
                stated_original = _sha256_hex(
                    evidence.get("original_candidate_fingerprint"),
                    field=f"operations[{index}].evidence.original_candidate_fingerprint",
                )
                if stated_original != original_fingerprint:
                    raise COnlyProtocolError("publication original candidate fingerprint does not match its object")
                stated_final = _sha256_hex(
                    evidence.get("merged_candidate_fingerprint", evidence.get("candidate_fingerprint")),
                    field=f"operations[{index}].evidence.merged_candidate_fingerprint",
                )
                if stated_final != fingerprint:
                    raise COnlyProtocolError("publication merged candidate fingerprint does not match its final object")
                if source_kind == "extraction":
                    expected = extraction_candidates[candidate_id]
                    if expected.get("candidate_fingerprint") != original_fingerprint:
                        raise COnlyProtocolError("publication original candidate differs from extracted candidate")
                    original_provenance = _object(
                        original_candidate.get("provenance"),
                        field=f"operations[{index}].original_candidate.provenance",
                    )
                    original_sources = original_provenance.get("source_instance_ids")
                    if not isinstance(original_sources, list) or {
                        _canonical_task(value, field="original candidate source") for value in original_sources
                    } != {_canonical_task(value, field="operation source") for value in source_ids}:
                        raise COnlyProtocolError("extraction publication must preserve every merged task source")
                    extraction_fp = _sha256_hex(
                        evidence.get("extraction_candidate_fingerprint", original_fingerprint),
                        field=f"operations[{index}].evidence.extraction_candidate_fingerprint",
                    )
                    if extraction_fp != original_fingerprint:
                        raise COnlyProtocolError("publication extraction fingerprint differs from original candidate")
                else:
                    fig8_fp = _sha256_hex(
                        evidence.get("fig8_candidate_fingerprint"),
                        field=f"operations[{index}].evidence.fig8_candidate_fingerprint",
                    )
                    if fig8_fp != original_fingerprint:
                        raise COnlyProtocolError("Fig.9 maintenance candidate differs from the Fig.8 candidate")
                if source_kind == "maintenance":
                    # Fig.8 and Fig.9 are a one-to-one pair.  The operation
                    # must retain the exact Fig.8 response hash, the exact
                    # Fig.9 response hash, and the ancestor union that was
                    # used to construct the candidate.  A caller cannot
                    # satisfy this boundary by merely naming a supplied
                    # identity or by supplying unrelated provenance.
                    decision_record = decisions_by_identity[supplied_identity]
                    fig8_hash = _sha256_hex(
                        evidence.get("fig8_manager_response_sha256"),
                        field=f"operations[{index}].evidence.fig8_manager_response_sha256",
                    )
                    if fig8_hash != decision_record.get("manager_response_sha256"):
                        raise COnlyProtocolError("Fig.9 maintenance evidence must cite the exact Fig.8 manager response")
                    fig8_ref = _verified_hash_ref(
                        {"path": evidence.get("fig8_manager_response_path"), "sha256": fig8_hash},
                        field=f"operations[{index}].evidence.fig8_manager_response",
                    )
                    if fig8_ref["sha256"] != fig8_hash or fig8_ref["path"] != decision_record.get("manager_response_path"):
                        raise COnlyProtocolError("Fig.9 maintenance evidence response path differs from Fig.8")
                    fig9_hash = _sha256_hex(
                        evidence.get("fig9_response_sha256"),
                        field=f"operations[{index}].evidence.fig9_response_sha256",
                    )
                    if fig9_hash != evidence.get("manager_response_sha256"):
                        raise COnlyProtocolError("Fig.9 maintenance evidence has inconsistent response hashes")
                    fig9_ref = _verified_hash_ref(
                        {"path": evidence.get("fig9_response_path"), "sha256": fig9_hash},
                        field=f"operations[{index}].evidence.fig9_response",
                    )
                    if fig9_ref["sha256"] != fig9_hash or fig9_ref["path"] != manager_ref["path"]:
                        raise COnlyProtocolError("Fig.9 maintenance evidence response path differs from its manager response")
                    original_provenance = _object(
                        original_candidate.get("provenance"),
                        field=f"operations[{index}].original_candidate.provenance",
                    )
                    fig8_provenance = _object(
                        evidence.get("fig8_candidate_provenance"),
                        field=f"operations[{index}].evidence.fig8_candidate_provenance",
                    )
                    if canonical_json(fig8_provenance) != canonical_json(original_provenance):
                        raise COnlyProtocolError("Fig.9 maintenance evidence does not retain the exact Fig.8 provenance")
                    operation_candidate = _object(candidate.get("provenance"), field=f"operations[{index}].candidate.provenance")
                    parent_ids = operation_candidate.get("parent_skill_ids")
                    if not isinstance(parent_ids, list) or skill_id not in {str(value) for value in parent_ids}:
                        raise COnlyProtocolError("Fig.9 maintenance candidate must retain its Fig.8 ancestor skill ID")
                    ancestor_union = _object(evidence.get("ancestor_union"), field=f"operations[{index}].evidence.ancestor_union")
                    if canonical_json(ancestor_union) != canonical_json(operation_candidate):
                        raise COnlyProtocolError("Fig.9 maintenance ancestor_union must equal candidate provenance")
                decision = item.get("decision")
                if decision not in {"add", "merge", "drop"}:
                    raise COnlyProtocolError("publication operation decision must be add, merge, or drop")
                merge_target = item.get("merge_target_id")
                if decision == "merge":
                    merge_target = _nonempty(merge_target, field=f"operations[{index}].merge_target_id")
                    target = staged._find_active(merge_target)
                    target_evidence = _object(
                        evidence.get("merge_target_skill"),
                        field=f"operations[{index}].evidence.merge_target_skill",
                    )
                    target_fp = _sha256_hex(
                        evidence.get("merge_target_skill_fingerprint"),
                        field=f"operations[{index}].evidence.merge_target_skill_fingerprint",
                    )
                    if target_fp != sha256_text(canonical_json(target_evidence)):
                        raise COnlyProtocolError("Fig.9 merge target fingerprint does not match its evidence object")
                    if canonical_json(target_evidence) != canonical_json(target):
                        raise COnlyProtocolError("Fig.9 merge target evidence differs from the frozen staged bank")
                elif merge_target is not None:
                    raise COnlyProtocolError("add/drop publication cannot carry a merge target")
                applied = staged.apply(
                    operation_id=op_id,
                    decision=decision,
                    candidate=candidate,
                    source_instance_ids=source_ids,
                    evidence={**evidence, "c_only_round": self.current_round_id, "task_id": canonical, "source_kind": source_kind},
                    merge_target_id=merge_target,
                )
                normalized_operations.append(
                    {
                        "operation_id": op_id,
                        "source_kind": source_kind,
                        "candidate_id": candidate_id,
                        "original_candidate": original_candidate,
                        "original_candidate_fingerprint": original_fingerprint,
                        "candidate_fingerprint": fingerprint,
                        "supplied_skill_identity": list(supplied_identity) if supplied_identity else None,
                        "decision": decision,
                        "merge_target_id": merge_target,
                        "operation": applied,
                    }
                )
        except (BankError, COnlyProtocolError) as error:
            round_state["publication_failures"].append(
                {
                    "round_id": self.current_round_id,
                    "task_id": canonical,
                    "operations": deepcopy(operation_list),
                    "manager_decisions": deepcopy(decision_list),
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "created_at_utc": utc_now(),
                }
            )
            raise COnlyProtocolError(
                "C-only publication failed; staged bank was discarded and raw manager evidence was preserved: "
                + str(error)
            ) from error
        if set(extraction_candidates) != seen_candidates:
            raise COnlyProtocolError("every extracted candidate needs one explicit Fig.9 add, merge, or drop decision")
        expected_maintenance = {
            identity for identity, decision in decisions_by_identity.items() if decision.get("action") == "evolve"
        }
        if set(maintenance_operation_by_identity) != expected_maintenance:
            raise COnlyProtocolError("every Fig.8 evolve decision needs exactly one Fig.9 maintenance operation")
        before = bank.snapshot()["state_sha256"]
        after = staged.snapshot()["state_sha256"]
        round_state["bank"] = staged.to_dict()
        result = {
            "round_id": self.current_round_id,
            "task_id": canonical,
            "before_bank_state_sha256": before,
            "after_bank_state_sha256": after,
            "operations": normalized_operations,
            "manager_decisions": decision_list,
            "publication": "current_round_C_to_next_task_only",
            "historical_baseline_used": False,
        }
        assignment["publication"] = result
        self._journal(round_state["operations"], operation_id, payload, result)
        return deepcopy(result)

    def finish_task(self, task_id: str, *, operation_id: str | None = None) -> dict[str, Any]:
        canonical = self._task(task_id)["canonical_instance_id"]
        operation_id = operation_id or f"complete:r{self.current_round_id}:{canonical}"
        payload = {"round_id": self.current_round_id, "task_id": canonical}
        replay = self._replay_any(operation_id, payload)
        if replay is not None:
            return replay
        round_state = self._round()
        canonical = self._assert_current_task(canonical, round_state=round_state)
        assignment = self._assignment(canonical)
        if assignment.get("status") != "finished" or "extraction" not in assignment or "publication" not in assignment:
            raise COnlyProtocolError("a task can advance only after trial, extraction, and publication are durable")
        if canonical in round_state["completed_tasks"]:
            raise COnlyProtocolError("task completion exists without a replayable operation")
        record = {
            "round_id": self.current_round_id,
            "task_id": canonical,
            "trial_id": assignment["trial_id"],
            "outcome": assignment["trial_evidence"]["outcome"],
            "trajectory": deepcopy(assignment["trial_evidence"].get("trajectory")),
            "extraction": {
                "candidate_count": len(assignment["extraction"].get("candidates", [])),
                "task_candidate_count": len(assignment["extraction"].get("task_candidate_records", [])),
                "decision": assignment["extraction"].get("decision"),
            },
            "publication": {"after_bank_state_sha256": assignment["publication"]["after_bank_state_sha256"]},
            "completed_at_utc": utc_now(),
        }
        round_state["completed_tasks"][canonical] = record
        round_state["next_task_index"] += 1
        if round_state["next_task_index"] == len(round_state["task_order"]):
            round_state["status"] = "complete"
        self._journal(round_state["operations"], operation_id, payload, record)
        return deepcopy(record)

    def start_next_round(self, *, operation_id: str | None = None) -> dict[str, Any]:
        operation_id = operation_id or "start-round-2"
        payload = {"from_round": 1, "to_round": 2}
        replay = self._replay_any(operation_id, payload)
        if replay is not None:
            return replay
        if self.current_round_id != 1:
            raise COnlyProtocolError("the two-round protocol can advance from round 1 only once")
        round_one = self._round(1)
        if round_one.get("status") != "complete" or len(round_one.get("completed_tasks", {})) != len(self.tasks):
            raise COnlyProtocolError("round 2 cannot start before every round 1 task is completed and published")
        round_two = self._round(2)
        if round_two.get("status") != "not_started" or round_two.get("next_task_index") != 0 or round_two.get("trajectory_pool") or round_two.get("description_pool") or round_two.get("task_candidate_pool"):
            raise COnlyProtocolError("round 2 has existing material and cannot be rehydrated")
        fresh = _empty_round(2, [item["canonical_instance_id"] for item in self.tasks])
        # Round 2 is a fresh active round once the durable transition is
        # journalled.  ``not_started`` is reserved for the pre-transition
        # empty placeholder and cannot coexist with a current round of 2.
        fresh["status"] = "active"
        fresh["created_from_round1_state_sha256"] = _round_digest(round_one)
        fresh["source_material"] = "fresh-empty; no round-1 skills or trajectories imported"
        self.state["rounds"]["2"] = fresh
        self.state["current_round"] = 2
        result = {"from_round": 1, "to_round": 2, "round_1_final_state_sha256": fresh["created_from_round1_state_sha256"], "round_2_bank_state_sha256": SkillBank.from_dict(fresh["bank"]).snapshot()["state_sha256"], "round_2_pools_empty": True}
        self._journal(self.state["global_operations"], operation_id, payload, result)
        return deepcopy(result)

    def authorize_start(self) -> dict[str, Any]:
        """Open the explicit formal gate after the caller has user approval."""
        if self.state.get("formal_campaign") == "active":
            return {"status": "active", "idempotent": True}
        if self.state.get("formal_campaign") != "not_started":
            raise COnlyProtocolError("formal campaign is already complete or blocked")
        self.state["formal_campaign"] = "active"
        result = {"status": "active", "authorized_at_utc": utc_now(), "condition": "C-only", "rounds": 2}
        self.state["formal_start"] = result
        return deepcopy(result)

    def mark_complete(self) -> dict[str, Any]:
        if self.current_round_id != 2 or self._round(2).get("status") != "complete":
            raise COnlyProtocolError("formal campaign cannot complete before both C-only rounds finish")
        if self.state.get("formal_campaign") != "active":
            raise COnlyProtocolError("formal campaign was not explicitly authorized")
        self.state["formal_campaign"] = "complete"
        result = {"status": "complete", "completed_at_utc": utc_now(), "planned_trials": len(self.tasks) * 2}
        self.state["formal_completion"] = result
        return deepcopy(result)

    def task_pairing_sources(self, task_id: str) -> list[str]:
        canonical = self._task(task_id)["canonical_instance_id"]
        round_state = self._round()
        available = sorted(self._available_pairing_tasks(round_state, canonical), key=lambda item: self._task(item)["order"])
        return available[:3]

    def _validate_state(self) -> None:
        if self.state.get("kind") != "r015_c_only_protocol_state" or self.state.get("protocol_id") != self.config["protocol_id"]:
            raise COnlyProtocolError("invalid C-only protocol state identity")
        _validate_embedded_hash_refs(self.state, field="state")
        if self.state.get("round_count") != 2 or self.state.get("formal_campaign") not in {"not_started", "active", "complete"}:
            raise COnlyProtocolError("formal campaign state is invalid")
        retrieval_profile = self.state.get("retrieval_profile")
        if retrieval_profile is None:
            raise COnlyProtocolError("state.retrieval_profile is required for the public C-only sidecar")
        _object(retrieval_profile, field="state.retrieval_profile")
        expected_profile_hash = _r012_profile_sha256(retrieval_profile)
        if self.state.get("profile_sha256") != expected_profile_hash:
            raise COnlyProtocolError("state.profile_sha256 differs from the frozen retrieval profile")
        expected_order = [item["canonical_instance_id"] for item in self.tasks]
        if self.state.get("task_order") != expected_order:
            raise COnlyProtocolError("state task order differs from the versioned C-only config")
        rounds = self.state.get("rounds")
        if not isinstance(rounds, dict) or set(rounds) != {"1", "2"}:
            raise COnlyProtocolError("state must contain exactly two round records")
        current = self.state.get("current_round")
        if current not in {1, 2}:
            raise COnlyProtocolError("current_round must be 1 or 2")
        for raw_round_id, round_state in rounds.items():
            value = _object(round_state, field=f"rounds.{raw_round_id}")
            round_id = int(raw_round_id)
            if value.get("round_id") != round_id or value.get("task_order") != expected_order:
                raise COnlyProtocolError(f"round {round_id} has a mismatched task order")
            bank = SkillBank.from_dict(_object(value.get("bank"), field=f"rounds.{raw_round_id}.bank"))
            if bank.benchmark != "terminal-bench":
                raise COnlyProtocolError("C-only bank benchmark must be terminal-bench")
            if bank.skills and not bank.operations:
                raise COnlyProtocolError(f"round {round_id} contains skills without current-round publication operations")
            for operation_index, operation in enumerate(bank.operations):
                bank_operation = _object(operation, field=f"rounds.{raw_round_id}.bank.operations[{operation_index}]")
                _reject_historical_material(bank_operation, path=f"rounds.{raw_round_id}.bank.operations[{operation_index}]")
                evidence = _object(bank_operation.get("evidence"), field=f"rounds.{raw_round_id}.bank.operations[{operation_index}].evidence")
                if evidence.get("c_only_round") != round_id or _canonical_task(evidence.get("task_id"), field="bank operation evidence.task_id") not in expected_order:
                    raise COnlyProtocolError(f"round {round_id} bank operation is not bound to this C-only round")
                response_hash = evidence.get("manager_response_sha256", evidence.get("fig9_response_sha256"))
                if response_hash is not None:
                    normalized_hash = _sha256_hex(response_hash, field="bank operation evidence.manager_response_sha256")
                    response_path = evidence.get("manager_response_path", evidence.get("fig9_response_path"))
                    verified = _verified_hash_ref(
                        {"path": response_path, "sha256": normalized_hash},
                        field="bank operation evidence.manager_response",
                    )
                    if verified["sha256"] != normalized_hash:
                        raise COnlyProtocolError("bank operation manager response hash changed")
                if evidence.get("fig8_manager_response_sha256") is not None:
                    fig8_hash = _sha256_hex(
                        evidence.get("fig8_manager_response_sha256"),
                        field="bank operation evidence.fig8_manager_response_sha256",
                    )
                    fig8_ref = _verified_hash_ref(
                        {"path": evidence.get("fig8_manager_response_path"), "sha256": fig8_hash},
                        field="bank operation evidence.fig8_manager_response",
                    )
                    if fig8_ref["sha256"] != fig8_hash:
                        raise COnlyProtocolError("bank operation Fig.8 response hash changed")
                source_ids = bank_operation.get("source_instance_ids", [])
                if not isinstance(source_ids, list) or not source_ids or any(_canonical_task(item, field="bank operation source_instance_ids") not in expected_order for item in source_ids):
                    raise COnlyProtocolError(f"round {round_id} bank operation has an invalid source task")
            for skill_index, skill in enumerate(bank.skills):
                item = _object(skill, field=f"rounds.{raw_round_id}.bank.skills[{skill_index}]")
                _reject_historical_material(item, path=f"rounds.{raw_round_id}.bank.skills[{skill_index}]")
                provenance = _object(item.get("provenance"), field=f"rounds.{raw_round_id}.bank.skills[{skill_index}].provenance")
                source_ids = provenance.get("source_instance_ids", [])
                if not isinstance(source_ids, list) or not source_ids or any(_canonical_task(source, field="bank skill source_instance_ids") not in expected_order for source in source_ids):
                    raise COnlyProtocolError(f"round {round_id} bank skill has an invalid source task")
            if round_id == 2 and value.get("status") == "not_started":
                if bank.skills or bank.operations or value.get("trajectory_pool") or value.get("description_pool") or value.get("task_candidate_pool") or value.get("assignments") or value.get("completed_tasks"):
                    raise COnlyProtocolError("not-started round 2 must remain completely empty")
            assignments_value = value.get("assignments", {})
            if not isinstance(assignments_value, dict):
                raise COnlyProtocolError(f"round {round_id} assignments must be an object")
            for assignment_key, assignment in assignments_value.items():
                item = _object(assignment, field="assignment")
                if item.get("condition") != "C-only" or item.get("historical_baseline_used") is not False:
                    raise COnlyProtocolError("all assignments must be C-only and baseline-free")
                canonical_assignment = _canonical_task(assignment_key, field="assignment key")
                if canonical_assignment not in expected_order or item.get("task_id") != canonical_assignment:
                    raise COnlyProtocolError("assignment key and task_id must identify the configured task")
                expected_trial_id = f"r{round_id}:C:{canonical_assignment}"
                if item.get("round_id") != round_id or item.get("trial_id") != expected_trial_id:
                    raise COnlyProtocolError("assignment trial identity is not bound to its C-only round/task")
                frozen = SkillBank.from_dict(_object(item.get("frozen_bank"), field="assignment.frozen_bank"))
                frozen_hash = _sha256_hex(item.get("frozen_bank_state_sha256"), field="assignment.frozen_bank_state_sha256")
                if frozen.snapshot()["state_sha256"] != frozen_hash:
                    raise COnlyProtocolError("assignment frozen bank hash differs from its immutable snapshot")
                if item.get("status") not in {"pending", "finished"}:
                    raise COnlyProtocolError("assignment status must be pending or finished")
                if "trial_evidence" in item:
                    evidence = _object(item.get("trial_evidence"), field="assignment.trial_evidence")
                    trial_id = _nonempty(item.get("trial_id"), field="assignment.trial_id")
                    if evidence.get("trial_id") != trial_id or evidence.get("round_id") != round_id or evidence.get("task_id") != item.get("task_id"):
                        raise COnlyProtocolError("assignment trial evidence is not bound to its frozen assignment")
                    raw_evidence = _object(evidence.get("raw_evidence"), field="assignment.trial_evidence.raw_evidence")
                    if raw_evidence.get("official_harbor_trial") is not True or raw_evidence.get("trial_id") != trial_id:
                        raise COnlyProtocolError("assignment trial evidence must retain the official Harbor trial binding")
                    if evidence.get("outcome") == "completed":
                        trajectory = _validate_trajectory_ref(
                            evidence.get("trajectory"),
                            field="assignment.trial_evidence.trajectory",
                            round_id=round_id,
                            task_id=item["task_id"],
                            trial_id=trial_id,
                        )
                        if trajectory.get("session_id") != raw_evidence.get("session_id"):
                            raise COnlyProtocolError("assignment trial evidence session identity changed")
            for pool_name in ("trajectory_pool", "description_pool", "task_candidate_pool"):
                pool = value.get(pool_name)
                if not isinstance(pool, list):
                    raise COnlyProtocolError(f"{pool_name} must be a list")
                seen_pool_tasks: set[str] = set()
                for item in pool:
                    record = _object(item, field=pool_name)
                    if record.get("round_id") != round_id or record.get("source") != "current_round_c_only":
                        raise COnlyProtocolError(f"{pool_name} contains a cross-round or historical record")
                    task_id = _canonical_task(record.get("task_id"), field=f"{pool_name}.task_id")
                    if task_id not in expected_order:
                        raise COnlyProtocolError(f"{pool_name} contains an unknown task")
                    if task_id in seen_pool_tasks:
                        raise COnlyProtocolError(f"{pool_name} contains duplicate task material: {task_id}")
                    seen_pool_tasks.add(task_id)
                    if pool_name == "trajectory_pool":
                        _nonempty(record.get("path"), field=f"{pool_name}.path")
                        trial_id = _nonempty(record.get("trial_id"), field=f"{pool_name}.trial_id")
                        session_id = _nonempty(record.get("session_id"), field=f"{pool_name}.session_id")
                        assignment = assignments_value.get(task_id)
                        if not isinstance(assignment, dict) or assignment.get("status") != "finished":
                            raise COnlyProtocolError(
                                f"{pool_name} entry has no finished assignment for task {task_id}"
                            )
                        trial_evidence = _object(
                            assignment.get("trial_evidence"),
                            field=f"rounds.{raw_round_id}.assignments.{task_id}.trial_evidence",
                        )
                        expected_trajectory = _object(
                            trial_evidence.get("trajectory"),
                            field=f"rounds.{raw_round_id}.assignments.{task_id}.trial_evidence.trajectory",
                        )
                        # A valid r015_binding is necessary but not sufficient:
                        # the pool entry must be the exact trajectory already
                        # recorded for this assignment.  This rejects a
                        # relabelled same-task/session file introduced between
                        # phases or during resume.
                        for key in ("trial_id", "session_id", "path", "sha256"):
                            if record.get(key) != expected_trajectory.get(key):
                                raise COnlyProtocolError(
                                    f"{pool_name} entry does not match the completed assignment trajectory ({key})"
                                )
                        _verified_file_ref(
                            record,
                            field=f"{pool_name}[{task_id}]",
                            expected_binding={
                                "round_id": round_id,
                                "task_id": task_id,
                                "trial_id": trial_id,
                                "session_id": session_id,
                            },
                        )
                    elif pool_name == "description_pool":
                        # Description-pool entries are the exact D01 records
                        # produced while extracting this task.  Keep their
                        # response/value/trajectory binding intact across a
                        # save/reload so D02 cannot consume a relabelled
                        # response or a description from another round.
                        _verified_hash_ref(record, field=f"{pool_name}[{task_id}]")
                        response_sha = _sha256_hex(
                            record.get("response_sha256", record.get("sha256")),
                            field=f"{pool_name}[{task_id}].response_sha256",
                        )
                        if response_sha != record.get("sha256"):
                            raise COnlyProtocolError(
                                f"{pool_name}[{task_id}] response_sha256 differs from its response hash"
                            )
                        description = _object(record.get("value"), field=f"{pool_name}[{task_id}].value")
                        value_sha = _sha256_hex(
                            record.get("value_sha256"),
                            field=f"{pool_name}[{task_id}].value_sha256",
                        )
                        if value_sha != sha256_text(canonical_json(description)):
                            raise COnlyProtocolError(
                                f"{pool_name}[{task_id}] value_sha256 does not match its D01 value"
                            )
                        assignment = assignments_value.get(task_id)
                        if not isinstance(assignment, dict) or assignment.get("status") != "finished":
                            raise COnlyProtocolError(
                                f"{pool_name} entry has no finished assignment for task {task_id}"
                            )
                        extraction = _object(
                            assignment.get("extraction"),
                            field=f"rounds.{raw_round_id}.assignments.{task_id}.extraction",
                        )
                        records = extraction.get("description_records", [])
                        if not isinstance(records, list):
                            raise COnlyProtocolError(
                                f"assignment {task_id} description_records must be a list"
                            )
                        matches = [
                            candidate
                            for candidate in records
                            if isinstance(candidate, dict)
                            and candidate.get("path") == record.get("path")
                            and candidate.get("sha256") == record.get("sha256")
                        ]
                        # The pool adds its durable round/source envelope when
                        # the record is published.  Compare the complete D01
                        # record after reconstructing that envelope so the
                        # source binding is strict without treating pool-only
                        # metadata as a second description value.
                        expected_pool_record = deepcopy(matches[0]) if len(matches) == 1 else None
                        if expected_pool_record is not None:
                            expected_pool_record.update(
                                {
                                    "round_id": round_id,
                                    "task_id": task_id,
                                    "source": "current_round_c_only",
                                }
                            )
                        if len(matches) != 1 or canonical_json(expected_pool_record) != canonical_json(record):
                            raise COnlyProtocolError(
                                f"{pool_name}[{task_id}] is not the exact description registered by its extraction"
                            )
                        trial_evidence = _object(
                            assignment.get("trial_evidence"),
                            field=f"rounds.{raw_round_id}.assignments.{task_id}.trial_evidence",
                        )
                        expected_trajectory = _object(
                            trial_evidence.get("trajectory"),
                            field=f"rounds.{raw_round_id}.assignments.{task_id}.trial_evidence.trajectory",
                        )
                        trajectory = _validate_trajectory_ref(
                            record.get("trajectory_ref"),
                            field=f"{pool_name}[{task_id}].trajectory_ref",
                            round_id=round_id,
                            task_id=task_id,
                            trial_id=_nonempty(expected_trajectory.get("trial_id"), field="trajectory.trial_id"),
                        )
                        if canonical_json(trajectory) != canonical_json(expected_trajectory):
                            raise COnlyProtocolError(
                                f"{pool_name}[{task_id}] trajectory_ref differs from its completed trial"
                            )
                    else:
                        assignment = assignments_value.get(task_id)
                        if not isinstance(assignment, dict) or assignment.get("status") != "finished":
                            raise COnlyProtocolError(
                                f"{pool_name} entry has no finished assignment for task {task_id}"
                            )
                        extraction = _object(
                            assignment.get("extraction"),
                            field=f"rounds.{raw_round_id}.assignments.{task_id}.extraction",
                        )
                        records = extraction.get("task_candidate_records", [])
                        if not isinstance(records, list):
                            raise COnlyProtocolError(
                                f"assignment {task_id} task_candidate_records must be a list"
                            )
                        matches = [
                            candidate
                            for candidate in records
                            if isinstance(candidate, dict)
                            and candidate.get("candidate_id") == record.get("candidate_id")
                            and candidate.get("candidate_fingerprint") == record.get("candidate_fingerprint")
                        ]
                        if len(matches) != 1 or canonical_json(matches[0]) != canonical_json(record):
                            raise COnlyProtocolError(
                                f"{pool_name}[{task_id}] is not the exact SOP candidate registered by its extraction"
                            )
                        skill, fingerprint = self._validate_skill_candidate(
                            record.get("skill"),
                            task_id=task_id,
                            candidate_field=f"{pool_name}[{task_id}].skill",
                        )
                        if skill.get("granularity") != "task" or fingerprint != _sha256_hex(record.get("candidate_fingerprint"), field=f"{pool_name}.candidate_fingerprint"):
                            raise COnlyProtocolError(f"{pool_name}[{task_id}] has invalid task-candidate content")
                        provenance = _object(skill.get("provenance"), field=f"{pool_name}[{task_id}].provenance")
                        source_ids = provenance.get("source_instance_ids")
                        if not isinstance(source_ids, list) or {_canonical_task(source, field="task candidate source") for source in source_ids} != {task_id}:
                            raise COnlyProtocolError(f"{pool_name}[{task_id}] must remain single-source")
                        trial_evidence = _object(
                            assignment.get("trial_evidence"),
                            field=f"rounds.{raw_round_id}.assignments.{task_id}.trial_evidence",
                        )
                        expected_trajectory = _object(
                            trial_evidence.get("trajectory"),
                            field=f"rounds.{raw_round_id}.assignments.{task_id}.trial_evidence.trajectory",
                        )
                        if canonical_json(record.get("trajectory_ref")) != canonical_json(expected_trajectory):
                            raise COnlyProtocolError(f"{pool_name}[{task_id}] trajectory_ref differs from its completed trial")
                        _validate_embedded_hash_refs(record.get("raw"), field=f"{pool_name}[{task_id}].raw")
                    if pool_name != "task_candidate_pool":
                        _sha256_hex(record.get("sha256"), field=f"{pool_name}.sha256")
        if current == 1 and self._round(2).get("status") != "not_started":
            raise COnlyProtocolError("round 2 cannot be active before round 1 starts it")


def initialize_c_only_protocol(config_path: Path, baseline_path: Path, state_path: Path) -> COnlyProtocol:
    """Initialize and persist a clean C-only state without launching a trial."""
    return COnlyProtocol.initialize(config_path, baseline_path, state_path)
