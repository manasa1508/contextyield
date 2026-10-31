"""Calibration summaries preserve physical measurement scope and sample limits."""

from __future__ import annotations

import json

import httpx
import pytest

from contextyield.calibration import run_quality_eval, summarize
from contextyield.config import Route
from contextyield.core import Target


def route() -> Route:
    target = Target("local", "inference", "short_answer", "http://localhost:8000/v1/chat/completions",
                    "model-v1", "local", 0, 1000, None, None, "unknown")
    return Route(target, None, "none", "nvml:0", True, 10)


def samples(count=10):
    return [{"id": str(i), "latency_ms": 100 + i, "correct": i % 2 == 0,
             "observation": {"basis": "device_counter_window", "joules": 2 + i / 10,
                             "attributable_to_request": True}}
            for i in range(count)]


def test_profile_summarizes_only_valid_isolated_samples():
    result = summarize(samples(), route(), "fixture-hash", "hardware-hash")
    assert result["profile"]["sample_count"] == 10
    assert result["suggested_route_fields"]["quality"] == 0.5
    assert result["suggested_route_fields"]["energy_low_j"] == 2.0
    assert result["suggested_route_fields"]["energy_high_j"] == 2.9
    assert result["suggested_route_fields"]["profile_hardware"] == "hardware-hash"
    changed = samples()
    changed[0]["correct"] = False
    different = summarize(changed, route(), "fixture-hash", "hardware-hash")
    assert different["suggested_route_fields"]["quality_eval_id"] != result["suggested_route_fields"]["quality_eval_id"]
    assert "gross GPU-device" in result["profile"]["caveat"]


def test_profile_rejects_too_few_or_nonexclusive_samples():
    with pytest.raises(ValueError, match="at least 10"):
        summarize(samples(9), route(), "fixture", "hardware")
    invalid = samples()
    invalid[0]["observation"]["attributable_to_request"] = False
    with pytest.raises(ValueError, match="exclusive"):
        summarize(invalid, route(), "fixture", "hardware")
    invalid = samples()
    invalid[0]["observation"]["joules"] = 0
    with pytest.raises(ValueError, match="invalid meter"):
        summarize(invalid, route(), "fixture", "hardware")


def test_quality_eval_uses_live_responses_but_does_not_store_prompts(tmp_path):
    fixture = {"schema_version": 1, "samples": [
        {"id": f"case-{index}", "messages": [{"role": "user", "content": f"PRIVATE_{index}"}],
         "expected_substring": "PASS"} for index in range(10)]}
    fixture_path = tmp_path / "private.json"
    fixture_path.write_text(json.dumps(fixture))
    output_path = tmp_path / "result.json"

    def upstream(request):
        content = json.loads(request.content)["messages"][0]["content"]
        answer = "PASS" if content != "PRIVATE_9" else "FAIL"
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = run_quality_eval(route(), fixture_path, output_path, client)
    assert result["evaluation"]["quality"] == 0.9
    assert result["evaluation"]["sample_count"] == 10
    assert result["suggested_route_fields"]["quality_eval_id"]
    assert "PRIVATE_" not in output_path.read_text()
    assert "energy" in result["warning"]


def test_quality_eval_rejects_short_fixture_before_upstream_call(tmp_path):
    fixture_path = tmp_path / "short.json"
    fixture_path.write_text(json.dumps({"schema_version": 1, "samples": []}))
    with httpx.Client(transport=httpx.MockTransport(
            lambda request: pytest.fail("upstream should not be called"))) as client:
        with pytest.raises(ValueError, match="10..1000"):
            run_quality_eval(route(), fixture_path, tmp_path / "result.json", client)


def test_quality_eval_records_failed_case_and_continues(tmp_path):
    fixture_path = tmp_path / "private.json"
    fixture_path.write_text(json.dumps({"schema_version": 1, "samples": [
        {"id": f"case-{index}", "messages": [{"role": "user", "content": str(index)}],
         "expected_substring": "PASS"} for index in range(10)]}))
    calls = []

    def upstream(request):
        index = json.loads(request.content)["messages"][0]["content"]
        calls.append(index)
        if index == "4":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "PASS"}}]})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = run_quality_eval(route(), fixture_path, tmp_path / "result.json", client)
    assert len(calls) == 10
    assert result["evaluation"]["quality"] == 0.9
    assert result["evaluation"]["failed_samples"] == 1
    assert result["samples"][4]["error_code"] == "http_503"
    assert result["suggested_route_fields"] is None
