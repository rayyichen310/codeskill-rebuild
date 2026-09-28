"""R012's durable, bounded event-extraction schedule.

This module deliberately manages attempt state only.  It neither retries a
model call nor changes a manager budget: callers record repair/transport
retries separately so historic R009/R011 evidence remains immutable.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .pipeline import event_extraction_with_evidence_messages
from .types import canonical_instance_id, canonical_json, sha256_text


MAX_INITIAL_EVENT_EXTRACTION_ATTEMPTS = 3


class EventExtractionScheduleError(ValueError):
    pass


def _candidate_fingerprint(result: dict[str, Any]) -> str | None:
    if result.get("action") != "generate" or not isinstance(result.get("skill"), dict):
        return None
    return sha256_text(canonical_json(result["skill"]))


@dataclass
class EventExtractionSchedule:
    """One source trajectory's initial R012 Fig.7 exploration budget."""

    trace: dict[str, Any]
    source_run_references: list[dict[str, Any]] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    retry_records: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        source = self.trace.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("canonical_instance_id"), str):
            raise EventExtractionScheduleError("trace needs source.canonical_instance_id")
        self.source_instance_id = canonical_instance_id(source["canonical_instance_id"])
        if len(self.attempts) > MAX_INITIAL_EVENT_EXTRACTION_ATTEMPTS:
            raise EventExtractionScheduleError("initial event attempts exceed the R012 maximum of three")

    @classmethod
    def from_manifest(cls, trace: dict[str, Any], value: dict[str, Any]) -> "EventExtractionSchedule":
        """Restore a durable schedule without reissuing any recorded call.

        A continuation must use this instead of reconstructing schedules from
        source traces: prior candidates affect the next prompt and a rejected
        initial call still consumes its one exploration slot.
        """
        if not isinstance(value, dict) or value.get("kind") != "r012_event_extraction_schedule":
            raise EventExtractionScheduleError("invalid R012 event extraction schedule manifest")
        attempts = value.get("attempts")
        retries = value.get("retry_records")
        references = value.get("source_run_references")
        if not isinstance(attempts, list) or not isinstance(retries, list) or not isinstance(references, list):
            raise EventExtractionScheduleError("schedule manifest has invalid lists")
        restored = cls(
            trace=trace,
            source_run_references=deepcopy(references),
            attempts=deepcopy(attempts),
            retry_records=deepcopy(retries),
        )
        stated_source = value.get("source_instance_id")
        if not isinstance(stated_source, str) or canonical_instance_id(stated_source) != restored.source_instance_id:
            raise EventExtractionScheduleError("schedule manifest source differs from trace")
        ordinals = [item.get("initial_attempt_ordinal") for item in restored.attempts if isinstance(item, dict)]
        if ordinals != list(range(1, len(restored.attempts) + 1)):
            raise EventExtractionScheduleError("schedule manifest initial attempts must be contiguous")
        return restored

    @property
    def stop_reason(self) -> str | None:
        for attempt in self.attempts:
            if attempt.get("outcome") in {"skip", "duplicate"}:
                return str(attempt["outcome"])
        if len(self.attempts) >= MAX_INITIAL_EVENT_EXTRACTION_ATTEMPTS:
            return "maximum_initial_attempts_reached"
        return None

    @property
    def next_initial_attempt_ordinal(self) -> int | None:
        return None if self.stop_reason is not None else len(self.attempts) + 1

    @property
    def prior_candidate_ids(self) -> list[str]:
        return [str(attempt["candidate_id"]) for attempt in self.attempts if isinstance(attempt.get("candidate_id"), str)]

    @property
    def prior_candidate_summaries(self) -> list[dict[str, Any]]:
        """Compact content plus evidence references for attempt two/three."""
        values: list[dict[str, Any]] = []
        for attempt in self.attempts:
            if attempt.get("outcome") not in {"generated", "repaired_generated"} or not isinstance(attempt.get("candidate_id"), str):
                continue
            result = attempt.get("result")
            skill = result.get("skill") if isinstance(result, dict) else None
            evidence = result.get("evidence") if isinstance(result, dict) else None
            if not isinstance(skill, dict) or not isinstance(evidence, dict):
                raise EventExtractionScheduleError("generated event attempt lacks validated skill/evidence")
            values.append(
                {
                    "candidate_id": attempt["candidate_id"],
                    "content": {
                        "title": skill.get("title"),
                        "when_to_apply": skill.get("when_to_apply"),
                        "rules": deepcopy(skill.get("rules")),
                    },
                    "step_references": {
                        key: deepcopy(evidence.get(key, []))
                        for key in ("trigger_step_ids", "response_step_ids", "outcome_step_ids")
                    },
                    "exact_content_fingerprint": attempt.get("candidate_fingerprint"),
                }
            )
        return values

    def messages_for_next_initial_attempt(self, *, runtime_prompt: str) -> list[dict[str, str]]:
        if self.next_initial_attempt_ordinal is None:
            raise EventExtractionScheduleError(f"event extraction is stopped: {self.stop_reason}")
        return event_extraction_with_evidence_messages(
            self.trace,
            runtime_prompt=runtime_prompt,
            prior_event_ids=self.prior_candidate_ids,
            prior_event_candidates=self.prior_candidate_summaries,
        )

    def record_initial_result(
        self,
        result: dict[str, Any],
        *,
        model_call_id: str | None,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        ordinal = self.next_initial_attempt_ordinal
        if ordinal is None:
            raise EventExtractionScheduleError(f"cannot add initial result after {self.stop_reason}")
        if not isinstance(result, dict) or result.get("action") not in {"generate", "skip"}:
            raise EventExtractionScheduleError("initial event result must be a validated generate or skip object")
        fingerprint = _candidate_fingerprint(result)
        prior_fingerprints = {str(item["candidate_fingerprint"]) for item in self.attempts if item.get("candidate_fingerprint")}
        if result["action"] == "skip":
            outcome = "skip"
            candidate_id = None
        elif fingerprint in prior_fingerprints:
            outcome = "duplicate"
            candidate_id = None
        else:
            outcome = "generated"
            candidate_id = f"event:{self.source_instance_id}:initial:{ordinal}:{fingerprint[:16]}"
        record = {
            "schema_version": 1,
            "kind": "r012_initial_event_extraction_attempt",
            "source_instance_id": self.source_instance_id,
            "initial_attempt_ordinal": ordinal,
            "maximum_initial_attempts": MAX_INITIAL_EVENT_EXTRACTION_ATTEMPTS,
            # The normal R012 input is full.  D03 permits a separately
            # evidenced compacted variant after exact overflow only.
            "full_trajectory_required": evidence.get("trajectory_input_mode", "full") == "full",
            "trajectory_input_mode": evidence.get("trajectory_input_mode", "full"),
            "prior_event_candidate_ids": self.prior_candidate_ids,
            "model_call_id": model_call_id,
            "result": deepcopy(result),
            "candidate_fingerprint": fingerprint,
            "candidate_id": candidate_id,
            "outcome": outcome,
            "evidence": deepcopy(evidence),
            "source_run_references": deepcopy(self.source_run_references),
        }
        self.attempts.append(record)
        record["stop_reason_after_attempt"] = self.stop_reason
        return deepcopy(record)

    def record_initial_failure(
        self,
        *,
        model_call_id: str | None,
        error: BaseException,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Retain a failed initial call without disguising it as a skip."""
        ordinal = self.next_initial_attempt_ordinal
        if ordinal is None:
            raise EventExtractionScheduleError(f"cannot add initial failure after {self.stop_reason}")
        record = {
            "schema_version": 1,
            "kind": "r012_initial_event_extraction_attempt",
            "source_instance_id": self.source_instance_id,
            "initial_attempt_ordinal": ordinal,
            "maximum_initial_attempts": MAX_INITIAL_EVENT_EXTRACTION_ATTEMPTS,
            "full_trajectory_required": evidence.get("trajectory_input_mode", "full") == "full",
            "trajectory_input_mode": evidence.get("trajectory_input_mode", "full"),
            "prior_event_candidate_ids": self.prior_candidate_ids,
            "model_call_id": model_call_id,
            "outcome": "failure",
            "error_type": type(error).__name__,
            "error": str(error),
            "evidence": deepcopy(evidence),
            "source_run_references": deepcopy(self.source_run_references),
        }
        self.attempts.append(record)
        record["stop_reason_after_attempt"] = self.stop_reason
        return deepcopy(record)

    def record_retry(
        self,
        *,
        retry_of: dict[str, Any],
        retry_kind: str,
        model_call_id: str | None,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Record a repair/transport retry without consuming exploration slots."""
        if not isinstance(retry_kind, str) or not retry_kind:
            raise EventExtractionScheduleError("retry_kind is required")
        if retry_of.get("kind") != "r012_initial_event_extraction_attempt":
            raise EventExtractionScheduleError("retry must reference an initial R012 extraction attempt")
        record = {
            "schema_version": 1,
            "kind": "r012_event_extraction_retry",
            "source_instance_id": self.source_instance_id,
            "retry_kind": retry_kind,
            "retry_of": {
                "source_instance_id": retry_of.get("source_instance_id"),
                "initial_attempt_ordinal": retry_of.get("initial_attempt_ordinal"),
                "candidate_id": retry_of.get("candidate_id"),
            },
            "model_call_id": model_call_id,
            "evidence": deepcopy(evidence),
            "initial_attempt_count_unchanged": len(self.attempts),
        }
        self.retry_records.append(record)
        return deepcopy(record)

    def resolve_evidence_only_repair(
        self,
        *,
        retry_of: dict[str, Any],
        model_call_id: str | None,
        repaired_result: dict[str, Any],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve exactly one rejected initial output through a sidecar repair.

        The original manager call already consumed its initial exploration
        ordinal.  A successful repair may attach evidence to that same output
        without creating a fictional additional Fig.7 attempt.  The repair
        itself remains a separate retry record and must preserve the original
        skill object exactly (validated by the caller).
        """
        if retry_of.get("kind") != "r012_initial_event_extraction_attempt" or retry_of.get("outcome") != "failure":
            raise EventExtractionScheduleError("evidence-only repair requires a recorded failed initial attempt")
        try:
            index = self.attempts.index(retry_of)
        except ValueError as error:
            raise EventExtractionScheduleError("repair target is not this schedule's recorded failure") from error
        if not isinstance(repaired_result, dict) or repaired_result.get("action") not in {"generate", "skip"}:
            raise EventExtractionScheduleError("repaired result must be a validated generate or skip object")
        retry = self.record_retry(
            retry_of=retry_of,
            retry_kind="evidence_only_sidecar_repair",
            model_call_id=model_call_id,
            evidence=evidence,
        )
        attempt = self.attempts[index]
        fingerprint = _candidate_fingerprint(repaired_result)
        other_fingerprints = {
            str(item["candidate_fingerprint"])
            for position, item in enumerate(self.attempts)
            if position != index and isinstance(item, dict) and item.get("candidate_fingerprint")
        }
        if repaired_result["action"] == "skip":
            outcome = "skip"
            candidate_id = None
        elif fingerprint in other_fingerprints:
            outcome = "duplicate"
            candidate_id = None
        else:
            outcome = "repaired_generated"
            candidate_id = f"event:{self.source_instance_id}:initial:{attempt['initial_attempt_ordinal']}:{fingerprint[:16]}"
        attempt.update(
            {
                "outcome": outcome,
                "result": deepcopy(repaired_result),
                "candidate_fingerprint": fingerprint,
                "candidate_id": candidate_id,
                "repair_resolution": {
                    "kind": "r012_evidence_only_sidecar_repair_resolution",
                    "retry_model_call_id": model_call_id,
                    "retry_record": deepcopy(retry),
                    "original_model_call_id": attempt.get("model_call_id"),
                    "original_validation_error": attempt.get("error"),
                    "initial_attempt_count_unchanged": len(self.attempts),
                },
            }
        )
        attempt["stop_reason_after_attempt"] = self.stop_reason
        return deepcopy(attempt)

    def manifest(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "r012_event_extraction_schedule",
            "source_instance_id": self.source_instance_id,
            "maximum_initial_attempts": MAX_INITIAL_EVENT_EXTRACTION_ATTEMPTS,
            "attempts": deepcopy(self.attempts),
            "retry_records": deepcopy(self.retry_records),
            "stop_reason": self.stop_reason,
            "next_initial_attempt_ordinal": self.next_initial_attempt_ordinal,
            "source_run_references": deepcopy(self.source_run_references),
        }
