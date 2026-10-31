"""Hard constraints cannot be relaxed by an optional decision advisor."""

import pytest

from contextyield.action_policy import ActionCandidate, TaskBudget, TaskState, choose_action


def candidate(id: str, action="answer", **fields):
    return ActionCandidate(id, action, fields.get("quality", 0.95),
                           fields.get("eval_id", "held-out-1"), fields.get("tokens", 10),
                           fields.get("cost", 0.01), fields.get("ms", 100),
                           fields.get("energy"), fields.get("energy_evidence", "unknown"))


def test_quality_floor_then_energy_and_cost():
    budget = TaskBudget(min_quality=0.9)
    decision = choose_action(budget, TaskState(), [
        candidate("low-quality", quality=0.5, energy=1, energy_evidence="profiled"),
        candidate("cheap", cost=0.01, energy=3, energy_evidence="profiled"),
        candidate("efficient", cost=0.02, energy=2, energy_evidence="profiled"),
    ])
    assert decision.selected_id == "efficient"
    rejected = next(item for item in decision.candidates if item["id"] == "low-quality")
    assert "quality_below_floor" in rejected["reasons"]


def test_unknown_spend_cannot_pass_hard_budget_and_advisor_cannot_override():
    budget = TaskBudget(min_quality=0.9, max_total_tokens=20)
    decision = choose_action(budget, TaskState(spent_tokens=None),
                             [candidate("advisor-choice")], advisor_preference="advisor-choice")
    assert decision.selected_id is None
    assert decision.action == "human"
    assert "token_budget_unverifiable" in decision.candidates[0]["reasons"]


def test_human_fallback_can_be_disallowed():
    decision = choose_action(TaskBudget(0.8, allowed_actions=("answer", "abstain")),
                             TaskState(), [candidate("x", quality=0.2)])
    assert decision.action == "abstain"


def test_unvalidated_quality_fails_even_at_zero_floor():
    decision = choose_action(TaskBudget(0.0), TaskState(), [candidate("x", eval_id=None)])
    assert decision.selected_id is None
    assert "quality_unvalidated" in decision.candidates[0]["reasons"]


def test_finite_energy_cap_requires_measured_so_far_and_profiled_forecast():
    budget = TaskBudget(0.8, max_energy_j=10)
    decision = choose_action(budget, TaskState(spent_energy_j=None), [candidate("x", energy=2)])
    assert "energy_budget_unverifiable" in decision.candidates[0]["reasons"]
    decision = choose_action(budget, TaskState(spent_energy_j=3),
                             [candidate("x", energy=2, energy_evidence="profiled")])
    assert decision.selected_id == "x"


def test_invalid_forecast_rejected():
    with pytest.raises(ValueError, match="candidate forecasts"):
        choose_action(TaskBudget(0.8), TaskState(), [candidate("x", tokens=-1)])
