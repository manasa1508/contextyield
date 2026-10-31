"""Task-level accounting and evidence honesty."""

import json
from pathlib import Path

import pytest

from contextyield.trajectory import TaskEvent, compare_cohorts, load_events, summarize


def event(event_id: str, kind: str = "model", **fields):
    return TaskEvent.model_validate({
        "task_id": "task-1", "event_id": event_id, "kind": kind,
        "started_at": "2026-11-01T00:00:00Z", "ended_at": "2026-11-01T00:00:01Z",
        **fields,
    })


def test_task_summary_counts_all_steps_but_keeps_missing_usage_unknown():
    report = summarize([
        event("m1", prompt_tokens=10, completion_tokens=3, cost_usd=0.001),
        event("d1", "decision", prompt_tokens=2, completion_tokens=0,
              cost_usd=0.0001),
        event("t1", "tool_execution"),
        event("done", "task_outcome", outcome_pass=True, outcome_basis="external_eval"),
    ])
    task = report["tasks"][0]
    assert task["steps"] == 3
    assert task["provider_tokens"] == {"prompt": 12, "completion": 3, "cached": None}
    assert task["cost_usd"] is None  # tool cost is unknown, not free
    assert task["energy_by_basis_j"] == {}
    assert task["outcome_pass"] is True


def test_trace_rejects_duplicate_and_content_fields(tmp_path: Path):
    payload = event("m1").model_dump(mode="json")
    path = tmp_path / "trace.jsonl"
    path.write_text(json.dumps(payload) + "\n" + json.dumps(payload) + "\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_events(path)
    payload["prompt"] = "secret"
    path.write_text(json.dumps(payload) + "\n")
    with pytest.raises(ValueError, match="invalid task event"):
        load_events(path)


def test_energy_basis_and_quality_evidence_are_strict():
    with pytest.raises(ValueError, match="energy_j and energy_basis"):
        event("m1", energy_j=1.0)
    with pytest.raises(ValueError, match="outcome_pass requires"):
        event("done", "task_outcome", outcome_pass=True)


def test_paired_comparison_flags_rebound_and_quality_regression():
    before = summarize([event("m1", prompt_tokens=10, completion_tokens=2),
                        event("o1", "task_outcome", outcome_pass=True,
                              outcome_basis="external_eval")])
    after = summarize([event("m1", prompt_tokens=15, completion_tokens=2),
                       event("m2", "model", prompt_tokens=5, completion_tokens=1),
                       event("o1", "task_outcome", outcome_pass=False,
                             outcome_basis="external_eval")])
    result = compare_cohorts(before, after)
    assert result["regressions"] == 1
    assert result["rebound_candidates"] == 1
    assert result["rows"][0]["token_delta"] == 11
