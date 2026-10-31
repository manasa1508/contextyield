"""Framework-neutral, metadata-only task trace analysis.

The functional unit is a completed task, not a model call. Partial telemetry
remains partial; in particular, tokens and dollars never imply joules.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

StepKind = Literal[
    "model", "decision", "retrieval", "tool_discovery", "tool_execution",
    "retry", "human_escalation", "task_outcome",
]


class TaskEvent(BaseModel):
    """Portable event emitted by an agent host or an SDK adapter."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    task_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    event_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    kind: StepKind
    started_at: datetime
    ended_at: datetime
    target: str | None = Field(default=None, max_length=128,
                               pattern=r"^[A-Za-z0-9_.:-]+$")
    prompt_tokens: int | None = Field(default=None, ge=0, strict=True)
    completion_tokens: int | None = Field(default=None, ge=0, strict=True)
    cached_tokens: int | None = Field(default=None, ge=0, strict=True)
    cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    energy_j: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    energy_basis: Literal["unavailable", "caller_attested_local_device", "provider_reported"] = "unavailable"
    receipt_id: str | None = Field(default=None, max_length=128,
                                   pattern=r"^[A-Za-z0-9_.:-]+$")
    trace_id: str | None = Field(default=None, max_length=128,
                                 pattern=r"^[A-Za-z0-9_.:-]+$")
    outcome_pass: bool | None = None
    outcome_basis: Literal["unavailable", "external_eval", "human_review"] = "unavailable"
    cache_hit: bool | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> TaskEvent:
        if self.started_at.tzinfo is None or self.ended_at.tzinfo is None:
            raise ValueError("event timestamps must include a timezone")
        if self.ended_at < self.started_at:
            raise ValueError("event ended before it started")
        if (self.energy_j is None) != (self.energy_basis == "unavailable"):
            raise ValueError("energy_j and energy_basis must agree")
        if self.kind != "task_outcome" and self.outcome_pass is not None:
            raise ValueError("only task_outcome may report outcome_pass")
        if self.outcome_pass is None and self.outcome_basis != "unavailable":
            raise ValueError("outcome_basis requires outcome_pass")
        if self.outcome_pass is not None and self.outcome_basis == "unavailable":
            raise ValueError("outcome_pass requires an external outcome_basis")
        return self


def load_events(path: Path, *, max_bytes: int = 32_000_000,
                max_events: int = 100_000) -> list[TaskEvent]:
    """Load bounded JSONL without accepting prompt, output, or secret fields."""
    if path.stat().st_size > max_bytes:
        raise ValueError("trace file exceeds maximum size")
    events: list[TaskEvent] = []
    seen: set[tuple[str, str]] = set()
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            if len(line) > 65_536:
                raise ValueError(f"line {line_number}: event exceeds maximum size")
            if len(events) >= max_events:
                raise ValueError("trace exceeds maximum event count")
            try:
                event = TaskEvent.model_validate(json.loads(line))
            except (ValueError, TypeError) as exc:
                raise ValueError(f"line {line_number}: invalid task event") from exc
            key = (event.task_id, event.event_id)
            if key in seen:
                raise ValueError(f"line {line_number}: duplicate event ID within task")
            seen.add(key)
            events.append(event)
    return events


def summarize(events: list[TaskEvent]) -> dict:
    """Aggregate by task, marking partial provider telemetry explicitly."""
    grouped: dict[str, list[TaskEvent]] = defaultdict(list)
    for event in events:
        grouped[event.task_id].append(event)
    tasks: list[dict] = []
    for task_id, steps in sorted(grouped.items()):
        outcomes = [event for event in steps if event.kind == "task_outcome"]
        if len(outcomes) > 1:
            raise ValueError(f"task {task_id}: multiple task_outcome events")
        active = [event for event in steps if event.kind != "task_outcome"]
        token_events = [event for event in active if event.kind in {"model", "decision"}]
        usage_complete = all(event.prompt_tokens is not None
                             and event.completion_tokens is not None for event in token_events)
        cost_complete = all(event.cost_usd is not None for event in active)
        quality = outcomes[0] if outcomes else None
        counts = Counter(event.kind for event in active)
        tasks.append({
            "task_id": task_id,
            "steps": len(active),
            "step_counts": dict(sorted(counts.items())),
            "wall_ms": int((max(event.ended_at for event in steps)
                            - min(event.started_at for event in steps)).total_seconds() * 1000),
            "provider_tokens": (
                {"prompt": sum(event.prompt_tokens or 0 for event in token_events),
                 "completion": sum(event.completion_tokens or 0 for event in token_events),
                 "cached": (sum(event.cached_tokens or 0 for event in token_events)
                            if all(event.cached_tokens is not None for event in token_events)
                            else None)}
                if usage_complete and token_events else None),
            "token_coverage": {"reported": sum(event.prompt_tokens is not None
                                                 and event.completion_tokens is not None
                                                 for event in token_events),
                               "eligible": len(token_events)},
            "cost_usd": round(sum(event.cost_usd or 0 for event in active), 8)
            if cost_complete and active else None,
            "cost_coverage": {"reported": sum(event.cost_usd is not None for event in active),
                              "eligible": len(active)},
            "energy_by_basis_j": {
                basis: round(sum(event.energy_j or 0 for event in active
                                 if event.energy_basis == basis), 6)
                for basis in ("caller_attested_local_device", "provider_reported")
                if any(event.energy_basis == basis for event in active)
            },
            "energy_coverage": {"reported": sum(event.energy_j is not None for event in active),
                                "eligible": len(active)},
            "energy_note": "caller/provider-reported partial observations; not whole-task energy",
            "outcome_pass": quality.outcome_pass if quality else None,
            "outcome_basis": quality.outcome_basis if quality else "unavailable",
        })
    return {"schema_version": 1, "task_count": len(tasks), "event_count": len(events), "tasks": tasks,
            "limitations": ["source code alone cannot supply runtime measurements",
                            "provider tokens are not energy", "unreported metrics remain unknown"]}


def compare_cohorts(baseline: dict, candidate: dict) -> dict:
    """Paired descriptive comparison only; no causal or statistical claim."""
    left = {task["task_id"]: task for task in baseline["tasks"]}
    right = {task["task_id"]: task for task in candidate["tasks"]}
    paired = sorted(left.keys() & right.keys())
    rows: list[dict] = []
    for task_id in paired:
        before, after = left[task_id], right[task_id]
        before_tokens = before["provider_tokens"]
        after_tokens = after["provider_tokens"]
        rows.append({
            "task_id": task_id,
            "step_delta": after["steps"] - before["steps"],
            "token_delta": (after_tokens["prompt"] + after_tokens["completion"]
                            - before_tokens["prompt"] - before_tokens["completion"]
                            if before_tokens is not None and after_tokens is not None else None),
            "cost_delta_usd": (round(after["cost_usd"] - before["cost_usd"], 8)
                               if before["cost_usd"] is not None
                               and after["cost_usd"] is not None else None),
            "quality_before": before["outcome_pass"],
            "quality_after": after["outcome_pass"],
        })
    return {"schema_version": 1, "paired_tasks": len(rows), "rows": rows,
            "regressions": sum(row["quality_before"] is True
                               and row["quality_after"] is False for row in rows),
            "rebound_candidates": sum(row["step_delta"] > 0 and
                                      (row["token_delta"] is None or row["token_delta"] > 0)
                                      for row in rows),
            "claim": "descriptive paired comparison; not causal savings or total energy"}
