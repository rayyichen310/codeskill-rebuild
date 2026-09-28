"""Explicit historical continuation for an audited pre-Graph Event call."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
for entry in (ROOT, ROOT / "src"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from scripts import run_r015_c_only_harbor_driver as driver
from scripts.legacy.r015_event_extraction import _event_extraction as historical_event
from codeskill_rebuild.types import sha256_file, write_json


def _event_extraction(*, context: dict[str, Any], input_value: dict[str, Any],
                      trace: dict[str, Any], executor: Any,
                      reconciled_manager_calls: dict[str, dict[str, Any]] | None = None,
                      reconciliation_path: Path | None = None
                      ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    return historical_event(
        context=context, input_value=input_value, trace=trace, executor=executor,
        hooks={
            "_prompt_path": driver._prompt_path,
            "_manager_call": driver._manager_call,
            "_finish_manager_journal": driver._finish_manager_journal,
            "_add_provenance": driver._add_provenance,
            "_manager_derivation_binding": driver._manager_derivation_binding,
            "_historical_thinking_policy_identity": driver._historical_thinking_policy_identity,
            "_hash_json": driver._hash_json,
            "_object": driver._object,
            "_text": driver._text,
        },
        reconciled_manager_calls=reconciled_manager_calls,
        reconciliation_path=reconciliation_path,
    )


def _legacy_build_extraction(*, context: dict[str, Any], input_value: dict[str, Any],
                             trace: dict[str, Any], current_ref: dict[str, Any],
                             executor: Any, reconciliation: dict[str, Any],
                             reconciliation_path: Path
                             ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    traces = driver._pool_traces(context, trace, current_ref, input_value)
    attempts, candidates, evidence = _event_extraction(
        context=context, input_value=input_value, trace=trace, executor=executor,
        reconciled_manager_calls={"event-001": reconciliation},
        reconciliation_path=reconciliation_path,
    )
    return driver._finish_extraction(
        context=context, input_value=input_value, trace=trace,
        current_ref=current_ref, executor=executor, traces=traces,
        event_attempts=attempts, event_candidates=candidates,
        event_evidence=evidence,
    )


def _continue_from_completed_trial(input_value: dict[str, Any], input_path: Path,
                                   output_path: Path, *,
                                   reconciliation_manifest_path: Path
                                   ) -> dict[str, Any]:
    if reconciliation_manifest_path is None:
        raise driver.COnlyHarborDriverError("historical continuation requires a reconciliation manifest")
    return driver._continue_from_completed_trial(
        input_value, input_path, output_path,
        reconciliation_manifest_path=reconciliation_manifest_path,
        extraction_builder=_legacy_build_extraction,
    )


def main() -> None:
    parser = driver._parser()
    parser.description = __doc__
    args = parser.parse_args()
    if not args.continue_from_trial or args.reconciliation_manifest is None or \
            args.check_config or args.recover_from_harbor_manifest is not None:
        raise driver.COnlyHarborDriverError(
            "historical driver requires --continue-from-trial and --reconciliation-manifest"
        )
    input_path = args.input.resolve()
    output_path = args.output.resolve()
    value = driver._load_input(input_path)
    output = _continue_from_completed_trial(
        value, input_path, output_path,
        reconciliation_manifest_path=args.reconciliation_manifest,
    )
    if not output_path.is_file():
        write_json(output_path, output)
    print(json.dumps({"status": "completed", "output": str(output_path),
                      "output_sha256": sha256_file(output_path)}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (driver.COnlyHarborDriverError, OSError, ValueError, KeyError,
            json.JSONDecodeError) as error:
        raise SystemExit(f"{type(error).__name__}: {error}") from error
