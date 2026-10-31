"""Offline commands do not require gateway settings or external backends."""

import json
from pathlib import Path

from contextyield.cli import main


def test_scan_and_trace_cli_are_offline(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("CY_REGISTRY", str(tmp_path / "missing.json"))
    (tmp_path / "agent.py").write_text("import openai\n")
    main(["scan-repo", "--path", str(tmp_path)])
    scan = json.loads(capsys.readouterr().out)
    assert scan["families"] == ["model_sdk"]
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({
        "task_id": "t", "event_id": "e", "kind": "model",
        "started_at": "2026-11-01T00:00:00Z", "ended_at": "2026-11-01T00:00:01Z",
        "prompt_tokens": 2, "completion_tokens": 1,
    }) + "\n")
    main(["analyze-trace", "--trace", str(trace)])
    analysis = json.loads(capsys.readouterr().out)
    assert analysis["tasks"][0]["provider_tokens"]["prompt"] == 2


def test_compare_traces_cli(tmp_path: Path, capsys):
    event = {"task_id": "t", "event_id": "m", "kind": "model",
             "started_at": "2026-11-01T00:00:00Z", "ended_at": "2026-11-01T00:00:01Z",
             "prompt_tokens": 5, "completion_tokens": 1}
    baseline = tmp_path / "before.jsonl"
    candidate = tmp_path / "after.jsonl"
    baseline.write_text(json.dumps(event) + "\n")
    candidate.write_text(json.dumps({**event, "prompt_tokens": 3}) + "\n")
    main(["compare-traces", "--baseline", str(baseline), "--candidate", str(candidate)])
    result = json.loads(capsys.readouterr().out)
    assert result["rows"][0]["token_delta"] == -2
