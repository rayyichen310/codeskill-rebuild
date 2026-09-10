"""Explicit context budgeting; it never silently chops a trajectory."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable


class ContextBlocked(ValueError):
    pass


def conservative_utf8_token_estimate(value: object) -> int:
    """A documented heuristic, never an upper-bound or exact-token claim."""
    encoded = str(value).encode("utf-8")
    return math.ceil(len(encoded) / 3)


@dataclass(frozen=True)
class ContextPlan:
    state: str
    input_token_estimate: int
    estimate_method: str
    budget_tokens: int
    retained_step_ids: list[str]
    omitted_step_ids: list[str]
    reason: str | None = None


def plan_context(*, rendered: str, step_ids: list[str], budget_tokens: int, estimate: Callable[[str], int] = conservative_utf8_token_estimate, exact_tokenizer_verified: bool = False) -> ContextPlan:
    input_estimate = estimate(rendered)
    if input_estimate <= budget_tokens:
        return ContextPlan(
            state="full" if exact_tokenizer_verified else "estimated_within_budget",
            input_token_estimate=input_estimate,
            estimate_method=getattr(estimate, "__name__", "custom_estimate"),
            budget_tokens=budget_tokens,
            retained_step_ids=list(step_ids),
            omitted_step_ids=[],
        )
    return ContextPlan(
        state="context_blocked",
        input_token_estimate=input_estimate,
        estimate_method=getattr(estimate, "__name__", "custom_estimate"),
        budget_tokens=budget_tokens,
        retained_step_ids=[],
        omitted_step_ids=list(step_ids),
        reason="No evidence compaction result was supplied; refusing silent truncation.",
    )


def deduplicate_exact_progress(lines: list[str]) -> tuple[list[str], list[int]]:
    """Deliberately disabled until inputs carry verified progress-event types.

    Plain transcript lines cannot distinguish repeated progress from a repeated
    command, error, or verification output. Returning the original sequence is
    safer than claiming a deduplicated trajectory.
    """
    return list(lines), []
