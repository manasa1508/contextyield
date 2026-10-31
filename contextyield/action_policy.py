"""Deterministic, fail-closed next-action policy for a caller-owned agent loop.

Forecasts are operator inputs, not measured task outcomes. An optional advisor
can break exact ties but can never bypass hard eligibility constraints.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Literal

Action = Literal[
    "answer", "retrieve", "search_tool", "decision_model", "strong_model",
    "reuse_evidence", "human", "abstain",
]


@dataclass(frozen=True)
class TaskBudget:
    min_quality: float
    max_steps: int | None = None
    max_total_tokens: int | None = None
    max_cost_usd: float | None = None
    max_wall_ms: int | None = None
    max_energy_j: float | None = None
    allowed_actions: tuple[Action, ...] = (
        "answer", "retrieve", "search_tool", "decision_model", "strong_model",
        "reuse_evidence", "human", "abstain",
    )


@dataclass(frozen=True)
class TaskState:
    steps: int = 0
    spent_tokens: int | None = 0
    spent_cost_usd: float | None = 0.0
    elapsed_ms: int = 0
    spent_energy_j: float | None = None


@dataclass(frozen=True)
class ActionCandidate:
    id: str
    action: Action
    predicted_quality: float | None
    quality_eval_id: str | None
    forecast_tokens: int | None
    forecast_cost_usd: float | None
    forecast_ms: int | None
    energy_high_j: float | None = None
    energy_evidence: Literal["unknown", "profiled"] = "unknown"


@dataclass(frozen=True)
class ActionDecision:
    selected_id: str | None
    action: Action
    reason: str
    candidates: tuple[dict, ...]


def _valid_nonnegative(value: float | None, *, integer: bool = False) -> bool:
    if value is None:
        return True
    if isinstance(value, bool) or (integer and not isinstance(value, int)):
        return False
    return isinstance(value, (int, float)) and isfinite(value) and value >= 0


def choose_action(budget: TaskBudget, state: TaskState,
                  candidates: list[ActionCandidate],
                  advisor_preference: str | None = None) -> ActionDecision:
    """Choose the least forecast energy/cost among hard-eligible actions.

    If an enforceable budget cannot be checked because usage or forecast is
    unknown, the candidate is excluded. Quality forecasts require an eval ID.
    This is a planning suggestion; the host remains responsible for execution.
    """
    if not 0 <= budget.min_quality <= 1 or not isfinite(budget.min_quality):
        raise ValueError("min_quality must be finite in [0, 1]")
    for value, integer in ((budget.max_steps, True), (budget.max_total_tokens, True),
                           (budget.max_cost_usd, False), (budget.max_wall_ms, True),
                           (budget.max_energy_j, False), (state.steps, True),
                           (state.spent_tokens, True), (state.spent_cost_usd, False),
                           (state.elapsed_ms, True), (state.spent_energy_j, False)):
        if not _valid_nonnegative(value, integer=integer):
            raise ValueError("budget and state values must be finite and non-negative")
    if len({candidate.id for candidate in candidates}) != len(candidates):
        raise ValueError("candidate IDs must be unique")
    results: list[dict] = []
    eligible: list[tuple[tuple[float, float, float, int, str], ActionCandidate]] = []
    for candidate in sorted(candidates, key=lambda item: item.id):
        if not candidate.id:
            raise ValueError("candidate ID must be nonempty")
        if candidate.predicted_quality is not None and (
            not isfinite(candidate.predicted_quality)
            or not 0 <= candidate.predicted_quality <= 1
        ):
            raise ValueError("predicted_quality must be finite in [0, 1]")
        for value, integer in ((candidate.forecast_tokens, True),
                               (candidate.forecast_cost_usd, False),
                               (candidate.forecast_ms, True),
                               (candidate.energy_high_j, False)):
            if not _valid_nonnegative(value, integer=integer):
                raise ValueError("candidate forecasts must be finite and non-negative")
        if candidate.energy_evidence == "profiled" and candidate.energy_high_j is None:
            raise ValueError("profiled energy needs an upper-bound forecast")
        reasons: list[str] = []
        if candidate.action not in budget.allowed_actions or candidate.action == "abstain":
            reasons.append("action_not_allowed_for_selection")
        if candidate.predicted_quality is None or not candidate.quality_eval_id:
            reasons.append("quality_unvalidated")
        elif candidate.predicted_quality < budget.min_quality:
            reasons.append("quality_below_floor")
        if budget.max_steps is not None and state.steps + 1 > budget.max_steps:
            reasons.append("step_budget_exceeded")
        if budget.max_total_tokens is not None:
            if state.spent_tokens is None or candidate.forecast_tokens is None:
                reasons.append("token_budget_unverifiable")
            elif state.spent_tokens + candidate.forecast_tokens > budget.max_total_tokens:
                reasons.append("token_budget_exceeded")
        if budget.max_cost_usd is not None:
            if state.spent_cost_usd is None or candidate.forecast_cost_usd is None:
                reasons.append("cost_budget_unverifiable")
            elif state.spent_cost_usd + candidate.forecast_cost_usd > budget.max_cost_usd:
                reasons.append("cost_budget_exceeded")
        if budget.max_wall_ms is not None:
            if candidate.forecast_ms is None:
                reasons.append("time_budget_unverifiable")
            elif state.elapsed_ms + candidate.forecast_ms > budget.max_wall_ms:
                reasons.append("time_budget_exceeded")
        if budget.max_energy_j is not None:
            if (state.spent_energy_j is None or candidate.energy_evidence != "profiled"
                    or candidate.energy_high_j is None):
                reasons.append("energy_budget_unverifiable")
            elif state.spent_energy_j + candidate.energy_high_j > budget.max_energy_j:
                reasons.append("energy_budget_exceeded")
        results.append({"id": candidate.id, "action": candidate.action,
                        "eligible": not reasons, "reasons": reasons})
        if not reasons:
            rank = (candidate.energy_high_j if candidate.energy_high_j is not None else float("inf"),
                    candidate.forecast_cost_usd if candidate.forecast_cost_usd is not None
                    else float("inf"),
                    candidate.forecast_tokens if candidate.forecast_tokens is not None
                    else float("inf"),
                    0 if candidate.id == advisor_preference else 1, candidate.id)
            eligible.append((rank, candidate))
    if eligible:
        chosen = min(eligible, key=lambda item: item[0])[1]
        return ActionDecision(chosen.id, chosen.action, "selected_from_validated_forecasts",
                              tuple(results))
    action: Action = "human" if "human" in budget.allowed_actions else "abstain"
    return ActionDecision(None, action, "no_hard_eligible_candidate", tuple(results))
