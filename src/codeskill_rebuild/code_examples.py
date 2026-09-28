"""Evidence-bound, adaptable code examples for solver-visible skills.

The manager selects a raw tool call and writes a separate reusable example.
This module never asks the manager to copy the source operation: callers pass
the exact normalized tool call/result selected from an immutable trajectory,
and the materialized record keeps the two representations distinct.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
from copy import deepcopy
from typing import Any, Callable

from .types import canonical_json, sha256_text


class CodeExampleError(ValueError):
    pass


SUPPORTED_LANGUAGES = {"python", "bash"}
ADAPTATION_KINDS = {"parameterized", "preserved_requirement", "other_rewrite"}
SOURCE_BINDING_FIELDS = ("canonical_instance_id", "action_step_id", "tool_call_id", "result_step_id")


def check_code_syntax(language: str, code: str) -> dict[str, Any]:
    """Parse supported example code without executing it."""
    if language == "python":
        try:
            ast.parse(code)
        except SyntaxError as error:
            return {
                "status": "invalid",
                "checker": "python_ast",
                "error": {
                    "message": error.msg,
                    "lineno": error.lineno,
                    "offset": error.offset,
                },
            }
        return {"status": "valid", "checker": "python_ast"}
    if language == "bash":
        executable = shutil.which("bash")
        if executable is None:
            return {
                "status": "not_checked",
                "checker": "bash_n",
                "reason": "bash executable is unavailable",
            }
        try:
            completed = subprocess.run(
                [executable, "--noprofile", "--norc", "-n"],
                input=code.encode("utf-8"),
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return {
                "status": "not_checked",
                "checker": "bash_n",
                "reason": f"bash syntax check failed to run: {type(error).__name__}: {error}",
            }
        if completed.returncode != 0:
            return {
                "status": "invalid",
                "checker": "bash_n",
                "returncode": completed.returncode,
                "stderr": completed.stderr.decode("utf-8", errors="replace"),
            }
        return {"status": "valid", "checker": "bash_n"}
    return {
        "status": "unsupported",
        "checker": None,
        "reason": f"unsupported code-example language: {language}",
    }


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CodeExampleError(f"{field} must be nonempty text")
    return value


def _text_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise CodeExampleError(f"{field} must be a list of nonempty strings")
    return list(value)


def _adaptations(value: Any, *, original_command: str, generated_code: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise CodeExampleError("code example adaptations must be a nonempty list")
    checked: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise CodeExampleError(f"code example adaptation[{index}] must be an object")
        kind = item.get("kind")
        if kind not in ADAPTATION_KINDS:
            raise CodeExampleError(f"code example adaptation[{index}] has unsupported kind {kind!r}")
        source_fragment = _text(item.get("source_fragment"), field=f"code example adaptation[{index}].source_fragment")
        generated_fragment = _text(item.get("generated_fragment"), field=f"code example adaptation[{index}].generated_fragment")
        rationale = _text(item.get("rationale"), field=f"code example adaptation[{index}].rationale")
        applicability = _text(item.get("applicability"), field=f"code example adaptation[{index}].applicability")
        if source_fragment not in original_command:
            raise CodeExampleError(f"code example adaptation[{index}] source_fragment is absent from the raw operation")
        if generated_fragment not in generated_code:
            raise CodeExampleError(f"code example adaptation[{index}] generated_fragment is absent from generated_code")
        if kind == "preserved_requirement" and source_fragment != generated_fragment:
            raise CodeExampleError("preserved_requirement must keep the exact source fragment")
        if kind in {"parameterized", "other_rewrite"} and source_fragment == generated_fragment:
            raise CodeExampleError(f"{kind} must describe a changed fragment")
        checked.append(
            {
                "kind": str(kind),
                "source_fragment": source_fragment,
                "generated_fragment": generated_fragment,
                "rationale": rationale,
                "applicability": applicability,
            }
        )
    return checked


def _local_operation_status(tool_result: dict[str, Any]) -> str:
    details = tool_result.get("details") if isinstance(tool_result.get("details"), dict) else {}
    exit_code = details.get("exitCode")
    if tool_result.get("is_error") is True:
        return "observed_tool_error"
    if isinstance(exit_code, int):
        return "observed_exit_zero" if exit_code == 0 else "observed_nonzero_exit"
    if tool_result.get("is_error") is False:
        return "observed_non_error"
    return "unknown"


def materialize_code_example(
    value: dict[str, Any],
    *,
    canonical_instance_id: str,
    action_step_id: str,
    result_step_id: str,
    tool_call: dict[str, Any],
    result_step: dict[str, Any],
    whole_task_outcome: Any,
) -> dict[str, Any]:
    """Combine manager-authored adaptation fields with exact source evidence."""
    if not isinstance(value, dict):
        raise CodeExampleError("code example must be an object")
    language = value.get("language")
    if language not in SUPPORTED_LANGUAGES:
        raise CodeExampleError(f"code example language must be one of {sorted(SUPPORTED_LANGUAGES)}")
    generated_code = _text(value.get("generated_code"), field="code example generated_code")
    if "```" in generated_code:
        raise CodeExampleError("generated_code cannot contain Markdown fence delimiters")
    if len([line for line in generated_code.splitlines() if line.strip()]) < 2:
        raise CodeExampleError("generated_code must contain at least two nonempty lines")
    arguments = tool_call.get("arguments")
    if not isinstance(arguments, dict):
        raise CodeExampleError("referenced tool call has no argument object")
    command_key = next((key for key in ("command", "cmd") if isinstance(arguments.get(key), str)), None)
    if command_key is None:
        raise CodeExampleError("referenced tool call has no string command/cmd argument")
    original_command = arguments[command_key]
    syntax = check_code_syntax(str(language), generated_code)
    if syntax["status"] != "valid":
        raise CodeExampleError(f"generated {language} example did not pass syntax validation: {syntax}")
    adaptations = _adaptations(
        value.get("adaptations"),
        original_command=original_command,
        generated_code=generated_code,
    )
    tool_result = result_step.get("tool_result")
    if not isinstance(tool_result, dict):
        raise CodeExampleError("referenced result step has no tool_result object")
    tool_call_id = tool_call.get("tool_call_id")
    source_binding = {
        "canonical_instance_id": canonical_instance_id,
        "action_step_id": action_step_id,
        "tool_call_id": tool_call_id,
        "result_step_id": result_step_id,
    }
    source_operation = {
        "origin": "programmatic_source_retrieval",
        "tool_name": tool_call.get("tool_name"),
        "arguments": deepcopy(arguments),
    }
    source_observation = {
        "origin": "programmatic_source_retrieval",
        "tool_name": tool_result.get("tool_name"),
        "is_error": tool_result.get("is_error"),
        "content": deepcopy(result_step.get("content")),
        "details": deepcopy(tool_result.get("details")),
    }
    generated_sha256 = sha256_text(generated_code)
    return {
        "schema_version": 1,
        "language": language,
        "purpose": _text(value.get("purpose"), field="code example purpose"),
        "source_binding": source_binding,
        "source_operation": {
            **source_operation,
            "sha256": sha256_text(canonical_json(source_operation)),
        },
        "source_observation": {
            **source_observation,
            "sha256": sha256_text(canonical_json(source_observation)),
        },
        "generated_example": {
            "origin": "manager_generated_adaptation",
            "execution_policy": "adapt_before_use",
            "code": generated_code,
            "adaptations": adaptations,
            "prerequisites": _text_list(value.get("prerequisites"), field="code example prerequisites"),
            "known_limitations": _text_list(value.get("known_limitations"), field="code example known_limitations"),
            "unknowns": _text_list(value.get("unknowns"), field="code example unknowns"),
            "sha256": generated_sha256,
        },
        "revision_history": [{"action": "created", "generated_sha256": generated_sha256}],
        "deterministic_checks": {
            "syntax": syntax,
            "source_fragments_present": True,
            "generated_fragments_present": True,
            "arbitrary_source_command_executed": False,
        },
        "evidence_boundary": {
            "local_operation_status": _local_operation_status(tool_result),
            "whole_task_outcome": deepcopy(whole_task_outcome),
            "local_operation_result_is_not_whole_task_outcome": True,
        },
    }


def validate_materialized_code_examples(value: Any) -> list[dict[str, Any]]:
    """Fail closed if a stored/materialized example loses its evidence identity."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise CodeExampleError("code_examples must be a list")
    checked: list[dict[str, Any]] = []
    identities: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or item.get("schema_version") not in {1, 2}:
            raise CodeExampleError(f"materialized code_examples[{index}] has an unsupported schema")
        binding = item.get("source_binding")
        operation = item.get("source_operation")
        observation = item.get("source_observation")
        generated = item.get("generated_example")
        checks = item.get("deterministic_checks")
        boundary = item.get("evidence_boundary")
        if not all(isinstance(part, dict) for part in (binding, operation, observation, generated, checks, boundary)):
            raise CodeExampleError(f"materialized code_examples[{index}] is incomplete")
        if not all(isinstance(binding.get(field), str) and binding[field] for field in SOURCE_BINDING_FIELDS):
            raise CodeExampleError(f"materialized code_examples[{index}] has invalid source binding")
        if operation.get("origin") != "programmatic_source_retrieval" or observation.get("origin") != "programmatic_source_retrieval":
            raise CodeExampleError("stored source operation/observation must remain programmatically retrieved")
        operation_payload = {key: deepcopy(value) for key, value in operation.items() if key != "sha256"}
        observation_payload = {key: deepcopy(value) for key, value in observation.items() if key != "sha256"}
        if operation.get("sha256") != sha256_text(canonical_json(operation_payload)):
            raise CodeExampleError("stored source operation hash does not match its payload")
        if observation.get("sha256") != sha256_text(canonical_json(observation_payload)):
            raise CodeExampleError("stored source observation hash does not match its payload")
        arguments = operation.get("arguments")
        if not isinstance(arguments, dict):
            raise CodeExampleError("stored source operation has no argument object")
        command_key = next((key for key in ("command", "cmd") if isinstance(arguments.get(key), str)), None)
        if command_key is None:
            raise CodeExampleError("stored source operation has no string command/cmd argument")
        original_command = arguments[command_key]
        code = generated.get("code")
        language = item.get("language")
        if language not in SUPPORTED_LANGUAGES:
            raise CodeExampleError(f"stored code example has unsupported language {language!r}")
        _text(item.get("purpose"), field="stored code example purpose")
        if generated.get("origin") != "manager_generated_adaptation" or generated.get("execution_policy") != "adapt_before_use":
            raise CodeExampleError("stored generated example has an invalid origin or execution policy")
        if not isinstance(code, str) or generated.get("sha256") != sha256_text(code):
            raise CodeExampleError("stored generated example hash does not match its code")
        if len([line for line in code.splitlines() if line.strip()]) < 2:
            raise CodeExampleError("stored generated example must contain at least two nonempty lines")
        if item["schema_version"] == 1:
            _adaptations(generated.get("adaptations"), original_command=original_command, generated_code=code)
        else:
            adaptation = item.get("maintenance_adaptation")
            if (generated.get("adaptations") != [] or not isinstance(adaptation, dict)
                    or not isinstance(adaptation.get("source_example_id"), str)
                    or not adaptation["source_example_id"]
                    or not isinstance(adaptation.get("reason"), str) or not adaptation["reason"].strip()
                    or adaptation.get("source_execution_verified") is not False):
                raise CodeExampleError("maintenance adaptation needs a source example, reason, and unverified boundary")
        _text_list(generated.get("prerequisites"), field="stored code example prerequisites")
        _text_list(generated.get("known_limitations"), field="stored code example known_limitations")
        _text_list(generated.get("unknowns"), field="stored code example unknowns")
        syntax = check_code_syntax(str(language), code)
        if syntax.get("status") != "valid" or checks.get("syntax", {}).get("status") != "valid":
            raise CodeExampleError("stored generated example no longer has valid supported syntax")
        if item["schema_version"] == 1:
            if checks.get("source_fragments_present") is not True or checks.get("generated_fragments_present") is not True:
                raise CodeExampleError("stored code example lost its fragment-presence checks")
        elif checks.get("source_fragments_present") is not False or checks.get("generated_fragments_present") is not False:
            raise CodeExampleError("maintenance adaptation cannot claim original fragment checks")
        if item["schema_version"] == 2 and boundary.get("maintenance_generated_code_executed") is not False:
            raise CodeExampleError("maintenance adaptation must mark generated code as unexecuted")
        if checks.get("arbitrary_source_command_executed") is not False:
            raise CodeExampleError("stored code example cannot claim arbitrary source execution")
        if boundary.get("local_operation_result_is_not_whole_task_outcome") is not True:
            raise CodeExampleError("stored code example must preserve the local/whole-task outcome boundary")
        history = item.get("revision_history")
        if history is not None:
            if not isinstance(history, list) or not history:
                raise CodeExampleError("stored code example revision_history must be a nonempty list")
            for revision_index, revision in enumerate(history):
                if not isinstance(revision, dict) or revision.get("action") not in {"created", "revised"}:
                    raise CodeExampleError(f"stored code example revision_history[{revision_index}] is invalid")
                if not isinstance(revision.get("generated_sha256"), str) or not revision["generated_sha256"]:
                    raise CodeExampleError(f"stored code example revision_history[{revision_index}] needs generated_sha256")
            if history[-1]["generated_sha256"] != generated["sha256"]:
                raise CodeExampleError("stored code example revision history does not end at the current generated code")
        if item["schema_version"] == 2 and (
            not isinstance(history, list) or history[-1].get("parent_example_id")
            != item["maintenance_adaptation"]["source_example_id"]
        ):
            raise CodeExampleError("maintenance adaptation lost its immediate source-example link")
        identity = _code_example_id_unchecked(item)
        if identity in identities:
            raise CodeExampleError("code_examples contains a duplicate code-example revision")
        identities.add(identity)
        checked.append(deepcopy(item))
    return checked


def _code_example_id_unchecked(item: dict[str, Any]) -> str:
    """Hash the complete generated contract and lineage, excluding raw evidence bytes."""
    generated = item["generated_example"]
    identity = {
                "source_binding": item["source_binding"],
                "language": item["language"],
                "purpose": item["purpose"],
                "generated_contract": {
                    "origin": generated["origin"],
                    "execution_policy": generated["execution_policy"],
                    "code": generated["code"],
                    "adaptations": generated["adaptations"],
                    "prerequisites": generated["prerequisites"],
                    "known_limitations": generated["known_limitations"],
                    "unknowns": generated["unknowns"],
                },
                "revision_history": item.get("revision_history", []),
            }
    if item["schema_version"] == 2:
        identity["schema_version"] = 2
        identity["maintenance_adaptation"] = item["maintenance_adaptation"]
    return sha256_text(canonical_json(identity))


def code_example_id(item: dict[str, Any]) -> str:
    """Return the stable identity of one complete generated revision."""
    checked = validate_materialized_code_examples([item])[0]
    return _code_example_id_unchecked(checked)


def materialize_code_example_from_stored_source(
    value: dict[str, Any],
    *,
    source_example: dict[str, Any],
    revision_context: dict[str, Any],
) -> dict[str, Any]:
    """Revise generated content while retaining immutable stored source bytes."""
    source = validate_materialized_code_examples([source_example])[0]
    operation = source["source_operation"]
    observation = source["source_observation"]
    binding = source["source_binding"]
    checked = materialize_code_example(
        value,
        canonical_instance_id=binding["canonical_instance_id"],
        action_step_id=binding["action_step_id"],
        result_step_id=binding["result_step_id"],
        tool_call={
            "tool_call_id": binding["tool_call_id"],
            "tool_name": operation.get("tool_name"),
            "arguments": deepcopy(operation["arguments"]),
        },
        result_step={
            "content": deepcopy(observation.get("content")),
            "tool_result": {
                "tool_call_id": binding["tool_call_id"],
                "tool_name": observation.get("tool_name"),
                "is_error": observation.get("is_error"),
                "details": deepcopy(observation.get("details")),
            },
        },
        whole_task_outcome=deepcopy(source["evidence_boundary"].get("whole_task_outcome")),
    )
    prior_history = deepcopy(source.get("revision_history"))
    if not isinstance(prior_history, list) or not prior_history:
        prior_history = [{"action": "created", "generated_sha256": source["generated_example"]["sha256"]}]
    checked["revision_history"] = [
        *prior_history,
        {
            "action": "revised",
            "generated_sha256": checked["generated_example"]["sha256"],
            "parent_example_id": code_example_id(source),
            "context": deepcopy(revision_context),
        },
    ]
    return validate_materialized_code_examples([checked])[0]


def materialize_maintenance_code_example(
    value: dict[str, Any], *, source_example: dict[str, Any],
    reason: str, revision_context: dict[str, Any],
) -> dict[str, Any]:
    """Adapt visible generated content; retain original evidence only as ancestry."""
    source = validate_materialized_code_examples([source_example])[0]
    if not isinstance(value, dict) or not isinstance(reason, str) or not reason.strip():
        raise CodeExampleError("maintenance example change needs generated fields and a reason")
    expected_fields = {"language", "purpose", "generated_code", "prerequisites",
                       "known_limitations", "unknowns"}
    if set(value) != expected_fields:
        raise CodeExampleError("maintenance replacement must contain only generated example fields")
    language = value.get("language")
    if language not in SUPPORTED_LANGUAGES:
        raise CodeExampleError("maintenance example has unsupported language")
    code = _text(value.get("generated_code"), field="maintenance generated_code")
    if "```" in code or len([line for line in code.splitlines() if line.strip()]) < 2:
        raise CodeExampleError("maintenance generated_code needs at least two nonempty lines without fences")
    syntax = check_code_syntax(language, code)
    if syntax["status"] != "valid":
        raise CodeExampleError(f"maintenance generated code did not pass syntax validation: {syntax}")
    source_id = code_example_id(source)
    result = deepcopy(source)
    result["schema_version"] = 2
    result["language"] = language
    result["purpose"] = _text(value.get("purpose"), field="maintenance purpose")
    result["generated_example"] = {
        "origin": "manager_generated_adaptation", "execution_policy": "adapt_before_use",
        "code": code, "adaptations": [],
        "prerequisites": _text_list(value.get("prerequisites"), field="maintenance prerequisites"),
        "known_limitations": _text_list(value.get("known_limitations"), field="maintenance known_limitations"),
        "unknowns": _text_list(value.get("unknowns"), field="maintenance unknowns"),
        "sha256": sha256_text(code),
    }
    result["deterministic_checks"] = {
        "syntax": syntax, "source_fragments_present": False,
        "generated_fragments_present": False, "arbitrary_source_command_executed": False,
    }
    result["maintenance_adaptation"] = {
        "source_example_id": source_id, "reason": reason,
        "source_execution_verified": False,
    }
    result["evidence_boundary"]["maintenance_generated_code_executed"] = False
    history = deepcopy(source.get("revision_history")) or [
        {"action": "created", "generated_sha256": source["generated_example"]["sha256"]}]
    result["revision_history"] = [*history, {
        "action": "revised", "generated_sha256": sha256_text(code),
        "parent_example_id": source_id, "context": deepcopy(revision_context),
    }]
    return validate_materialized_code_examples([result])[0]


def apply_code_example_changes(
    changes: Any,
    *existing_groups: Any,
    materialize_added: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    materialize_added_change: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    materialize_revised: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    revision_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Apply explicit retain/revise/remove/add choices to one skill revision."""
    existing = combine_materialized_code_examples(*existing_groups)
    if changes is None:
        if existing:
            raise CodeExampleError("code_example_changes must explicitly cover every existing example")
        return []
    if not isinstance(changes, list):
        raise CodeExampleError("code_example_changes must be a list")
    by_id = {code_example_id(item): item for item in existing}
    decided: set[str] = set()
    output: list[dict[str, Any]] = []
    for index, change in enumerate(changes):
        if not isinstance(change, dict) or change.get("action") not in {"retain", "revise", "remove", "add"}:
            raise CodeExampleError(f"code_example_changes[{index}] has an invalid action")
        action = str(change["action"])
        if action == "add":
            raw = change.get("example")
            if not isinstance(raw, dict) or (materialize_added is None and materialize_added_change is None):
                raise CodeExampleError("add needs an example and an allowed source materializer")
            output.append(materialize_added_change(change) if materialize_added_change else materialize_added(raw))
            continue
        identity = change.get("example_id")
        if not isinstance(identity, str) or identity not in by_id:
            raise CodeExampleError(f"code_example_changes[{index}] cites an unknown example_id")
        if identity in decided:
            raise CodeExampleError(f"code_example_changes[{index}] decides one existing example twice")
        decided.add(identity)
        source = by_id[identity]
        if action == "retain":
            output.append(deepcopy(source))
        elif action == "revise":
            raw = change.get("example")
            if not isinstance(raw, dict):
                raise CodeExampleError("revise needs a replacement example")
            output.append(materialize_revised(raw, source) if materialize_revised else
                          materialize_code_example_from_stored_source(
                              raw, source_example=source,
                              revision_context=revision_context or {}))
    missing = sorted(set(by_id) - decided)
    if missing:
        raise CodeExampleError(f"code_example_changes omitted existing examples: {missing}")
    return validate_materialized_code_examples(output)


def combine_materialized_code_examples(*groups: Any) -> list[dict[str, Any]]:
    """Combine exact revisions while rejecting conflicting duplicate identities."""
    combined: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for group in groups:
        for item in validate_materialized_code_examples(group):
            identity = code_example_id(item)
            prior = seen.get(identity)
            if prior is None:
                seen[identity] = item
                combined.append(item)
            elif prior != item:
                raise CodeExampleError("code-example inputs contain conflicting records for one source/generated revision")
    return validate_materialized_code_examples(combined)
