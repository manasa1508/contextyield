"""Full-information synthetic comparison, not a production counterfactual estimator."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any

from .config import Settings
from .core import Constraints, choose

POLICIES = ("energy", "fastest", "priority")


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(fraction * len(ordered)) - 1]


def compare(settings: Settings, fixture_path: Path) -> dict[str, Any]:
    fixture_bytes = fixture_path.read_bytes()
    if len(fixture_bytes) > 10_000_000:
        raise ValueError("synthetic fixture exceeds 10 MB")
    fixture = json.loads(fixture_bytes)
    if (fixture.get("schema_version") != 1 or not isinstance(fixture.get("tasks"), list)
            or len(fixture["tasks"]) > 10_000):
        raise ValueError("fixture must have schema_version=1 and tasks")
    manifest = {
        "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
        "registry_sha256": settings.registry_digest,
        "policy_version": "contextyield-v1",
        "evidence": "synthetic_full_information_fixture",
    }
    rows: list[dict[str, Any]] = []
    for task in fixture["tasks"]:
        constraints = Constraints(**task.get("constraints", {}))
        decision = choose((route.target for route in settings.routes.values()),
                          task["kind"], task["capability"], constraints)
        eligible = [settings.routes[item.id].target for item in decision.candidates if item.eligible]
        by_policy = {
            "energy": decision.selected_id,
            "fastest": min(eligible, key=lambda t: (t.p95_ms or float("inf"), t.id)).id if eligible else None,
            "priority": min(eligible, key=lambda t: (t.priority, t.id)).id if eligible else None,
        }
        for policy, selected_id in by_policy.items():
            if selected_id is None:
                rows.append({"task_id": task["id"], "policy": policy, "selected_id": None,
                             "error": "no_eligible_route"})
                continue
            outcome = task["outcomes"].get(selected_id)
            if outcome is None:
                raise ValueError(f"fixture lacks full-information outcome for {task['id']}/{selected_id}")
            quality = float(outcome["quality"])
            latency_ms = float(outcome["latency_ms"])
            synthetic_j = float(outcome["synthetic_energy_j"])
            if (not all(math.isfinite(value) for value in (quality, latency_ms, synthetic_j))
                    or not 0 <= quality <= 1 or latency_ms < 0 or synthetic_j < 0):
                raise ValueError("fixture outcomes need bounded quality and nonnegative latency/energy")
            rows.append({"task_id": task["id"], "policy": policy, "selected_id": selected_id,
                         "quality": quality, "latency_ms": latency_ms,
                         "synthetic_energy_j": synthetic_j})
    summaries = {}
    for policy in POLICIES:
        subset = [row for row in rows if row["policy"] == policy and row["selected_id"]]
        summaries[policy] = {
            "completed": len(subset),
            "rejected": sum(1 for row in rows if row["policy"] == policy and not row["selected_id"]),
            "mean_fixture_quality": mean(row["quality"] for row in subset) if subset else None,
            "p95_fixture_latency_ms": _percentile([row["latency_ms"] for row in subset], 0.95),
            "total_synthetic_joules": sum(row["synthetic_energy_j"] for row in subset),
        }
    return {"manifest": manifest, "summaries": summaries, "rows": rows,
            "warning": "Synthetic full-information fixture only. No physical energy savings or real-world quality claim."}


def write_report(result: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "comparison.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    lines = ["# ContextYield synthetic comparison", "", result["warning"], "",
             f"Fixture SHA-256: `{result['manifest']['fixture_sha256']}`", "",
             "| Policy | Completed | Rejected | Mean fixture quality | p95 fixture latency (ms) | Synthetic joules |",
             "|---|---:|---:|---:|---:|---:|"]
    for policy, summary in result["summaries"].items():
        quality = summary["mean_fixture_quality"]
        latency = summary["p95_fixture_latency_ms"]
        lines.append(f"| {policy} | {summary['completed']} | {summary['rejected']} | "
                     f"{quality:.3f} | {latency:.0f} | {summary['total_synthetic_joules']:.2f} |"
                     if quality is not None and latency is not None else
                     f"| {policy} | {summary['completed']} | {summary['rejected']} | n/a | n/a | 0 |")
    (output_dir / "report.md").write_text("\n".join(lines) + "\n")
