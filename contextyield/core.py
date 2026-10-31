"""ContextYield's dependency-free routing policy.

Energy bounds are forecasts, not guarantees or per-request measurements. A stale
profile is treated as unknown energy when the caller explicitly allows unknowns.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from numbers import Real
from time import time

_PROFILED = frozenset({"profiled", "benchmark_profile"})
_SYNTHETIC = frozenset({"synthetic", "synthetic_demo"})
_UNKNOWN = frozenset({"unknown", "unavailable"})
_REQUIREMENTS = frozenset({"profiled", "allow_synthetic_demo", "allow_unknown"})


@dataclass(frozen=True)
class Target:
    id: str
    kind: str
    capability: str
    url: str = ""
    version: str = ""
    region: str | None = None
    quality: float = 0.0
    p95_ms: float | None = None
    energy_low_j: float | None = None
    energy_high_j: float | None = None
    evidence: str = "unknown"
    profile_id: str | None = None
    profile_expires_at: datetime | float | None = None
    priority: int = 0
    enabled: bool = True


@dataclass(frozen=True)
class Constraints:
    min_quality: float = 0.0
    max_p95_ms: float | None = None
    max_energy_j: float | None = None
    evidence_requirement: str = "allow_unknown"
    region: str | None = None


@dataclass(frozen=True)
class Candidate:
    id: str
    eligible: bool
    reasons: tuple[str, ...]
    energy_high_j: float | None
    p95_ms: float | None


@dataclass(frozen=True)
class Decision:
    selected_id: str | None
    candidates: tuple[Candidate, ...]
    reason: str


def _number(value: Real | None, name: str, *, maximum: float | None = None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if value < 0 or (maximum is not None and value > maximum):
        raise ValueError(f"{name} is out of range")


def _timestamp(value: datetime | Real, name: str) -> float:
    if isinstance(value, datetime):
        # Naive datetimes are interpreted as UTC, never in the host's local zone.
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.timestamp()
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
        raise ValueError(f"{name} must be a datetime or finite Unix timestamp")
    return float(value)


def _validate(target: Target) -> None:
    if not isinstance(target.id, str) or not target.id:
        raise ValueError("target id must be a nonempty string")
    if not isinstance(target.kind, str) or not target.kind:
        raise ValueError(f"{target.id}: kind must be a nonempty string")
    if not isinstance(target.capability, str) or not target.capability:
        raise ValueError(f"{target.id}: capability must be a nonempty string")
    if target.evidence not in _PROFILED | _SYNTHETIC | _UNKNOWN:
        raise ValueError(f"{target.id}: unsupported evidence {target.evidence!r}")
    _number(target.quality, f"{target.id}.quality", maximum=1.0)
    if target.quality is None:
        raise ValueError(f"{target.id}.quality is required")
    _number(target.p95_ms, f"{target.id}.p95_ms")
    _number(target.energy_low_j, f"{target.id}.energy_low_j")
    _number(target.energy_high_j, f"{target.id}.energy_high_j")
    if (target.energy_low_j is not None and target.energy_high_j is not None
            and target.energy_low_j > target.energy_high_j):
        raise ValueError(f"{target.id}: energy_low_j exceeds energy_high_j")
    if isinstance(target.priority, bool) or not isinstance(target.priority, int):
        raise TypeError(f"{target.id}.priority must be an integer")
    if target.profile_expires_at is not None:
        _timestamp(target.profile_expires_at, f"{target.id}.profile_expires_at")


def _validate_constraints(constraints: Constraints) -> None:
    _number(constraints.min_quality, "min_quality", maximum=1.0)
    if constraints.min_quality is None:
        raise ValueError("min_quality is required")
    _number(constraints.max_p95_ms, "max_p95_ms")
    _number(constraints.max_energy_j, "max_energy_j")
    if constraints.evidence_requirement not in _REQUIREMENTS:
        raise ValueError("unsupported evidence_requirement")


def choose(
    targets: Iterable[Target],
    kind: str,
    capability: str,
    constraints: Constraints,
    now: datetime | Real | None = None,
) -> Decision:
    """Choose one eligible target; never relax a caller constraint.

    Evidence names accepted on targets: ``profiled``/``benchmark_profile``,
    ``synthetic``/``synthetic_demo``, and ``unknown``/``unavailable``. Expiry
    and ``now`` accept UTC datetimes or Unix seconds. A missing p95 cannot pass
    a latency cap; a missing or stale energy upper bound cannot pass an energy
    cap. Candidates are returned in target-ID order for reproducibility.
    """
    if not isinstance(kind, str) or not kind:
        raise ValueError("kind must be a nonempty string")
    if not isinstance(capability, str) or not capability:
        raise ValueError("capability must be a nonempty string")
    if not isinstance(constraints, Constraints):
        raise TypeError("constraints must be Constraints")
    _validate_constraints(constraints)
    instant = time() if now is None else _timestamp(now, "now")

    ordered = list(targets)
    for target in ordered:
        if not isinstance(target, Target):
            raise TypeError("targets must contain Target instances")
        _validate(target)
    ids = [target.id for target in ordered]
    if len(ids) != len(set(ids)):
        raise ValueError("target ids must be unique")

    candidates: list[Candidate] = []
    eligible: list[tuple[tuple[float, float, int, str], Target]] = []
    for target in sorted(ordered, key=lambda item: item.id):
        reasons: list[str] = []
        if not target.enabled:
            reasons.append("disabled")
        if target.kind != kind:
            reasons.append("kind_mismatch")
        if target.capability != capability:
            reasons.append("capability_mismatch")
        if constraints.region is not None and target.region != constraints.region:
            reasons.append("region_mismatch")
        if target.quality < constraints.min_quality:
            reasons.append("quality_below_min")
        if constraints.max_p95_ms is not None:
            if target.p95_ms is None:
                reasons.append("latency_unknown")
            elif target.p95_ms > constraints.max_p95_ms:
                reasons.append("latency_exceeds_max")

        expired = (target.profile_expires_at is not None and
                   _timestamp(target.profile_expires_at, "profile_expires_at") <= instant)
        evidence_known = target.evidence not in _UNKNOWN and not expired
        energy_high = target.energy_high_j if evidence_known else None
        if energy_high is None:
            evidence_known = False

        requirement = constraints.evidence_requirement
        if requirement == "profiled":
            if target.evidence not in _PROFILED:
                reasons.append("evidence_not_profiled")
            elif expired:
                reasons.append("profile_expired")
            elif not evidence_known:
                reasons.append("energy_unknown")
        elif requirement == "allow_synthetic_demo":
            if target.evidence in _UNKNOWN:
                reasons.append("evidence_unknown")
            elif expired:
                reasons.append("profile_expired")
            elif not evidence_known:
                reasons.append("energy_unknown")

        if constraints.max_energy_j is not None:
            if energy_high is None:
                if "energy_unknown" not in reasons:
                    reasons.append("energy_unknown")
            elif energy_high > constraints.max_energy_j:
                reasons.append("energy_exceeds_max")

        candidate = Candidate(target.id, not reasons, tuple(reasons), energy_high,
                              target.p95_ms)
        candidates.append(candidate)
        if candidate.eligible:
            rank = (energy_high if energy_high is not None else float("inf"),
                    target.p95_ms if target.p95_ms is not None else float("inf"),
                    target.priority, target.id)
            eligible.append((rank, target))

    if not eligible:
        return Decision(None, tuple(candidates), "no_eligible_target")
    _, selected = min(eligible, key=lambda item: item[0])
    selected_candidate = next(item for item in candidates if item.id == selected.id)
    reason = ("selected_with_unknown_energy" if selected_candidate.energy_high_j is None
              else "selected_by_energy_latency_priority_id")
    return Decision(selected.id, tuple(candidates), reason)
