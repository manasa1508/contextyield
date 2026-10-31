"""Synthetic comparisons must be deterministic and never claim physical savings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contextyield.config import load_settings
from contextyield.replay import compare, write_report

ROOT = Path(__file__).resolve().parents[1]


def test_replay_is_deterministic_and_labelled(monkeypatch, tmp_path):
    monkeypatch.chdir(ROOT)
    settings = load_settings()
    fixture = ROOT / "fixtures" / "synthetic-v1.json"
    first = compare(settings, fixture)
    second = compare(settings, fixture)
    assert first == second
    assert first["manifest"]["evidence"] == "synthetic_full_information_fixture"
    assert "No physical energy savings" in first["warning"]
    selected = {(row["task_id"], row["policy"]): row["selected_id"] for row in first["rows"]}
    assert selected[("short-easy-1", "energy")] == "mock-efficient"
    assert selected[("short-hard-1", "energy")] == "mock-robust"
    out = tmp_path / "run"
    write_report(first, out)
    before = (out / "comparison.json").read_bytes()
    write_report(second, out)
    assert (out / "comparison.json").read_bytes() == before
    assert json.loads(before)["manifest"]["fixture_sha256"]


def test_replay_rejects_invalid_synthetic_energy(monkeypatch, tmp_path):
    monkeypatch.chdir(ROOT)
    fixture = json.loads((ROOT / "fixtures" / "synthetic-v1.json").read_text())
    fixture["tasks"][0]["outcomes"]["mock-efficient"]["synthetic_energy_j"] = float("nan")
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(fixture))
    with pytest.raises(ValueError, match="bounded quality"):
        compare(load_settings(), path)
