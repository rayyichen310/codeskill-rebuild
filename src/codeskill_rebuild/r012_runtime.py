"""Runtime-facing composition of R012 freeze and supplied-only evolution."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Iterable

from .evolution import supplied_skills_for_evolution
from .trial_schedule import InstanceBankFreeze


class R012Runtime:
    """One lifecycle coordinator for a future isolated M3/M4 runner.

    It performs no model call itself.  The runner supplies durable proxy
    attempt records and explicit maintenance callbacks, so the same gates work
    for real OpenClaw evidence and offline integration fixtures.
    """

    def __init__(self, bank_freeze: InstanceBankFreeze) -> None:
        self.bank_freeze = bank_freeze

    def freeze_instance(self, instance_id: str) -> list[dict[str, Any]]:
        return self.bank_freeze.freeze(instance_id)

    def supplied_evolution_candidates(self, *, trial_id: str, proxy_attempt_records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return supplied_skills_for_evolution(proxy_attempt_records, trial_id=trial_id)

    def release_instance_updates(
        self,
        instance_id: str,
        *,
        ordered_trial_ids: Iterable[str],
        apply_update: Callable[[str, Any, dict[str, Any]], Any],
    ) -> list[dict[str, Any]]:
        return self.bank_freeze.release_updates(
            instance_id,
            ordered_trial_ids=ordered_trial_ids,
            apply_update=apply_update,
        )

    def finish_trial(self, trial_id: str, *, result_evidence: dict[str, Any]) -> None:
        self.bank_freeze.finish(trial_id, result_evidence=deepcopy(result_evidence))
