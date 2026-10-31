"""Black-box local Compose acceptance check using Python's standard library."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid

BASE = os.getenv("CY_SMOKE_URL", "http://localhost:18080")
KEY = os.getenv("CY_SMOKE_KEY", "dev-local-key")


def request(method: str, path: str, body: dict | None = None, key: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {KEY}",
                                          **({"Idempotency-Key": key} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def main() -> None:
    status, ready = request("GET", "/health/ready")
    assert status == 200 and ready["status"] == "ready"
    secret_prompt = "SMOKE_SECRET_PROMPT_71d9"
    body = {"kind": "inference", "capability": "short_answer",
            "messages": [{"role": "user", "content": secret_prompt}],
            "constraints": {"min_quality": 0.8, "evidence_requirement": "allow_synthetic_demo"}}
    key = str(uuid.uuid4())
    status, result = request("POST", "/v1/execute", body, key)
    assert status == 200 and result["target_id"] == "mock-efficient", result
    assert result["forecast"]["evidence"] == "synthetic_demo"
    assert result["observation"]["basis"] == "unavailable"
    assert result["token_usage"] == {"basis": "provider_reported", "prompt_tokens": 1,
                                     "completion_tokens": 2, "total_tokens": 3}
    receipt_id = result["receipt_id"]
    status, receipt = request("GET", f"/v1/receipts/{receipt_id}")
    assert status == 200 and receipt["status"] == "completed"
    assert receipt["outcome"]["token_usage"] == result["token_usage"]
    assert secret_prompt not in json.dumps(receipt), "prompt leaked into receipt"
    status, repeated = request("POST", "/v1/execute", body, key)
    assert status == 409 and repeated["receipt_id"] == receipt_id
    tool = {"kind": "tool", "capability": "catalog.lookup", "input": {"sku": "A-100"},
            "constraints": {"min_quality": 0.95, "evidence_requirement": "allow_synthetic_demo"}}
    status, result = request("POST", "/v1/execute", tool)
    assert status == 200 and result["target_id"] == "catalog-origin", result
    embedding = {"kind": "embedding", "capability": "retrieval.embed", "text": "heat pump",
                 "constraints": {"min_quality": 0.8,
                                 "evidence_requirement": "allow_synthetic_demo"}}
    status, result = request("POST", "/v1/execute", embedding)
    assert status == 200 and result["target_id"] == "mock-embedding", result
    assert result["output"]["data"][0]["embedding"] == [9.0, 2.0, 1.0]
    impossible = {**body, "constraints": {"min_quality": 0.99,
                                          "evidence_requirement": "allow_synthetic_demo"}}
    status, result = request("POST", "/v1/execute", impossible)
    assert status == 422 and result["error"] == "no_eligible_route"
    status, found = request("GET", "/v1/tools/search?q=catalog")
    assert status == 200 and found["tools"] == [{"capability": "catalog.lookup"}]
    status, advice = request("POST", "/v1/actions/choose", {
        "budget": {"min_quality": 0.8, "max_total_tokens": 100},
        "state": {"spent_tokens": 10},
        "candidates": [{"id": "answer", "action": "answer", "predicted_quality": 0.9,
                        "quality_eval_id": "smoke-eval", "forecast_tokens": 5,
                        "forecast_cost_usd": 0.001, "forecast_ms": 10}],
    })
    assert status == 200 and advice["selected_id"] == "answer", advice
    task_id = f"smoke-{uuid.uuid4()}"
    event = {"schema_version": 1, "task_id": task_id, "event_id": "model-1",
             "kind": "model", "started_at": "2026-11-01T00:00:00Z",
             "ended_at": "2026-11-01T00:00:01Z", "target": "mock-efficient",
             "receipt_id": receipt_id, "prompt_tokens": 1, "completion_tokens": 2}
    status, created = request("POST", "/v1/tasks/events", event)
    assert status == 200 and created["status"] == "created", created
    status, duplicate = request("POST", "/v1/tasks/events", event)
    assert status == 200 and duplicate["status"] == "duplicate", duplicate
    status, conflict = request("POST", "/v1/tasks/events", {**event, "completion_tokens": 3})
    assert status == 409, conflict
    status, summary = request("GET", f"/v1/tasks/{task_id}/summary")
    assert status == 200 and summary["provider_tokens"]["prompt"] == 1, summary
    assert summary["provider_tokens"]["completion"] == 2, summary
    print("PASS: route gateway, receipt ledger, task event ledger, advisory policy, discovery")


if __name__ == "__main__":
    main()
