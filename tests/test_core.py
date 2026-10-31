"""Focused standard-library tests for ContextYield's routing contract."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from contextyield.core import Constraints, Target, choose

NOW = datetime(2026, 10, 9, tzinfo=UTC)


def target(id="a", **changes):
    base = Target(
        id=id,
        kind="inference",
        capability="short_answer",
        url="http://localhost/model",
        version="v1",
        region="us-east",
        quality=0.9,
        p95_ms=100,
        energy_low_j=2,
        energy_high_j=4,
        evidence="profiled",
        profile_id="profile-1",
        profile_expires_at=NOW + timedelta(days=1),
        priority=0,
        enabled=True,
    )
    return replace(base, **changes)


class ChooseTests(unittest.TestCase):
    def choose(self, targets, constraints=None, **kwargs):
        return choose(targets, "inference", "short_answer",
                      constraints or Constraints(), now=NOW, **kwargs)

    def test_ranks_by_upper_energy_then_latency_priority_and_id(self):
        options = [
            target("z", energy_high_j=5, p95_ms=1),
            target("c", energy_high_j=4, p95_ms=100, priority=1),
            target("b", energy_high_j=4, p95_ms=100, priority=0),
            target("a", energy_high_j=4, p95_ms=100, priority=0),
        ]
        decision = self.choose(options)
        self.assertEqual(decision.selected_id, "a")
        self.assertEqual([c.id for c in decision.candidates], ["a", "b", "c", "z"])
        self.assertEqual(self.choose(reversed(options)).selected_id, "a")

    def test_hard_filters_do_not_waive_constraints(self):
        options = [
            target("disabled", enabled=False),
            target("wrong-kind", kind="tool"),
            target("wrong-capability", capability="summarize"),
            target("wrong-region", region="eu"),
            target("low-quality", quality=0.5),
            target("slow", p95_ms=101),
            target("high-energy", energy_high_j=5),
        ]
        constraints = Constraints(min_quality=0.8, max_p95_ms=100,
                                  max_energy_j=4, region="us-east")
        decision = self.choose(options, constraints)
        self.assertIsNone(decision.selected_id)
        self.assertEqual(decision.reason, "no_eligible_target")
        self.assertTrue(all(not c.eligible and c.reasons for c in decision.candidates))
        self.assertIn("energy_exceeds_max", next(c for c in decision.candidates
                                                  if c.id == "high-energy").reasons)

    def test_profiled_rejects_synthetic_unknown_and_expired(self):
        options = [
            target("synthetic", evidence="synthetic_demo", energy_low_j=0.5,
                   energy_high_j=1),
            target("unknown", evidence="unknown", energy_high_j=None),
            target("expired", profile_expires_at=NOW),
            target("valid", energy_high_j=5),
        ]
        decision = self.choose(options, Constraints(evidence_requirement="profiled"))
        self.assertEqual(decision.selected_id, "valid")
        by_id = {c.id: c for c in decision.candidates}
        self.assertIn("evidence_not_profiled", by_id["synthetic"].reasons)
        self.assertIn("evidence_not_profiled", by_id["unknown"].reasons)
        self.assertIn("profile_expired", by_id["expired"].reasons)
        self.assertIsNone(by_id["expired"].energy_high_j)

    def test_demo_requirement_accepts_synthetic_but_not_unknown(self):
        options = [target("profiled", energy_high_j=4),
                   target("synthetic", evidence="synthetic", energy_high_j=2),
                   target("unknown", evidence="unavailable", energy_high_j=None)]
        decision = self.choose(options, Constraints(evidence_requirement="allow_synthetic_demo"))
        self.assertEqual(decision.selected_id, "synthetic")
        self.assertFalse(next(c for c in decision.candidates if c.id == "unknown").eligible)

    def test_allow_unknown_ranks_known_first_and_ignores_stale_bound(self):
        options = [target("missing", evidence="unknown", energy_high_j=None, p95_ms=1),
                   target("stale", energy_low_j=0.05, energy_high_j=0.1,
                          profile_expires_at=NOW - timedelta(seconds=1)),
                   target("known", energy_high_j=100, p95_ms=200)]
        decision = self.choose(options)
        self.assertEqual(decision.selected_id, "known")
        self.assertIsNone(next(c for c in decision.candidates if c.id == "stale").energy_high_j)
        unknown_only = self.choose(options[:2])
        self.assertEqual(unknown_only.selected_id, "missing")
        self.assertEqual(unknown_only.reason, "selected_with_unknown_energy")

    def test_energy_cap_rejects_unknown_and_uses_upper_bound(self):
        options = [target("unknown", evidence="unknown", energy_high_j=None),
                   target("over", energy_high_j=4.01),
                   target("boundary", energy_high_j=4)]
        decision = self.choose(options, Constraints(max_energy_j=4))
        self.assertEqual(decision.selected_id, "boundary")
        self.assertIn("energy_unknown", next(c for c in decision.candidates
                                               if c.id == "unknown").reasons)

    def test_missing_latency_cannot_pass_latency_cap(self):
        decision = self.choose([target(p95_ms=None)], Constraints(max_p95_ms=100))
        self.assertIsNone(decision.selected_id)
        self.assertIn("latency_unknown", decision.candidates[0].reasons)

    def test_tool_kind_and_capability_work_too(self):
        tool = target("tool", kind="tool", capability="catalog.lookup")
        decision = choose([tool], "tool", "catalog.lookup", Constraints(), now=NOW)
        self.assertEqual(decision.selected_id, "tool")

    def test_unix_timestamps_and_naive_utc_datetimes(self):
        expiry = (NOW + timedelta(seconds=1)).timestamp()
        option = target(profile_expires_at=expiry)
        self.assertEqual(choose([option], "inference", "short_answer",
                                Constraints(evidence_requirement="profiled"),
                                now=NOW.replace(tzinfo=None)).selected_id, "a")

    def test_invalid_numeric_ranges_and_duplicates(self):
        invalid_targets = [
            target(quality=-0.1), target(quality=1.1), target(quality=float("nan")),
            target(p95_ms=-1), target(energy_low_j=-1),
            target(energy_low_j=5, energy_high_j=4),
            target(energy_high_j=float("inf")), target(priority=True),
        ]
        for option in invalid_targets:
            with self.subTest(option=option), self.assertRaises((ValueError, TypeError)):
                self.choose([option])
        for constraints in [Constraints(min_quality=-1), Constraints(min_quality=2),
                            Constraints(max_p95_ms=-1), Constraints(max_energy_j=-1),
                            Constraints(evidence_requirement="invalid")]:
            with self.subTest(constraints=constraints), self.assertRaises(ValueError):
                self.choose([target()], constraints)
        with self.assertRaises(ValueError):
            self.choose([target(), target()])


if __name__ == "__main__":
    unittest.main()
