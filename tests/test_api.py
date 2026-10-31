"""HTTP contract and fault-path checks without a running database or paid backend."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import StringIO

import httpx
from fastapi.testclient import TestClient

from contextyield.api import LOG, create_app
from contextyield.config import Route, Settings
from contextyield.core import Target
from contextyield.store import InsertResult


class MemoryStore:
    def __init__(self, fail_insert: bool = False) -> None:
        self.rows: dict[str, dict] = {}
        self.keys: dict[tuple[str, str], str] = {}
        self.task_events: dict[tuple[str, str, str], dict] = {}
        self.fail_insert = fail_insert

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def ready(self) -> bool:
        return True

    def insert_decision(self, tenant, idempotency_key, request_hash, decision):
        if self.fail_insert:
            raise RuntimeError("database unavailable")
        key = (tenant, idempotency_key)
        if idempotency_key is not None and key in self.keys:
            old = self.rows[self.keys[key]]
            return InsertResult(old["id"], False, old["request_hash"] == request_hash)
        receipt_id = str(uuid.uuid4())
        self.rows[receipt_id] = {"id": receipt_id, "tenant": tenant, "request_hash": request_hash,
                                 "decision": decision, "status": "pending", "outcome": None,
                                 "attempts": []}
        if idempotency_key is not None:
            self.keys[key] = receipt_id
        return InsertResult(receipt_id, True, True)

    def finish(self, receipt_id, status, outcome, attempt=None):
        row = self.rows[receipt_id]
        assert row["status"] == "pending"
        row["status"] = status
        row["outcome"] = outcome
        if attempt:
            row["attempts"].append(attempt)

    def get(self, tenant, receipt_id):
        row = self.rows.get(receipt_id)
        return row if row and row["tenant"] == tenant else None

    def insert_task_event(self, tenant, event):
        key = (tenant, event["task_id"], event["event_id"])
        if key in self.task_events:
            if self.task_events[key] != event:
                raise ValueError("event_id_reused_with_different_content")
            return False
        self.task_events[key] = event
        return True

    def get_task_events(self, tenant, task_id):
        return [event for (owner, task, _), event in self.task_events.items()
                if owner == tenant and task == task_id]


def make_settings() -> Settings:
    efficient = Target("efficient", "inference", "short_answer", "http://backend/efficient",
                       "efficient-v1", "local", 0.82, 900, 2.0, 5.0,
                       "synthetic_demo", "fixture-1", None, 20, True)
    robust = Target("robust", "inference", "short_answer", "http://backend/robust",
                    "robust-v1", "local", 0.96, 600, 8.0, 15.0,
                    "synthetic_demo", "fixture-1", None, 10, True)
    return Settings("demo", "unused", {"a": "key-a", "b": "key-b"},
                    {target.id: Route(target, None, "none", None, False, 2)
                     for target in (efficient, robust)}, "registry-digest", "test-hash-key")


BODY = {"kind": "inference", "capability": "short_answer",
        "messages": [{"role": "user", "content": "PRIVATE_PROMPT_abc"}],
        "constraints": {"min_quality": 0.8, "evidence_requirement": "allow_synthetic_demo"}}
AUTH = {"Authorization": "Bearer key-a"}
CHAT_OK = {"choices": [{"message": {"content": "ok"}}],
           "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}}


def test_receipt_redaction_idempotency_and_tenant_scope():
    calls = []
    log_buffer = StringIO()
    log_handler = logging.StreamHandler(log_buffer)
    LOG.addHandler(log_handler)

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json=CHAT_OK)

    store = MemoryStore()
    client = httpx.Client(transport=httpx.MockTransport(upstream))
    try:
        with TestClient(create_app(make_settings(), store, client)) as app:
            assert app.post("/v1/execute", json=BODY).status_code == 401
            first = app.post("/v1/execute", headers={**AUTH, "Idempotency-Key": "same"}, json=BODY)
            assert first.status_code == 200
            data = first.json()
            assert data["target_id"] == "efficient"
            assert data["forecast"]["evidence"] == "synthetic_demo"
            assert data["observation"]["basis"] == "unavailable"
            assert data["token_usage"] == {"basis": "provider_reported", "prompt_tokens": 4,
                                            "completion_tokens": 2, "total_tokens": 6}
            receipt = app.get(f"/v1/receipts/{data['receipt_id']}", headers=AUTH)
            assert receipt.status_code == 200
            assert receipt.json()["outcome"]["token_usage"] == data["token_usage"]
            assert "PRIVATE_PROMPT_abc" not in json.dumps(receipt.json())
            assert app.get(f"/v1/receipts/{data['receipt_id']}",
                           headers={"Authorization": "Bearer key-b"}).status_code == 404
            duplicate = app.post("/v1/execute", headers={**AUTH, "Idempotency-Key": "same"}, json=BODY)
            assert duplicate.status_code == 409
            assert len(calls) == 1
            changed = {**BODY, "messages": [{"role": "user", "content": "different"}]}
            assert app.post("/v1/execute", headers={**AUTH, "Idempotency-Key": "same"},
                            json=changed).status_code == 422
            assert len(calls) == 1
        assert "PRIVATE_PROMPT_abc" not in log_buffer.getvalue()
    finally:
        LOG.removeHandler(log_handler)


def test_no_route_and_ledger_fail_closed():
    calls = []
    client = httpx.Client(transport=httpx.MockTransport(lambda req: calls.append(req) or
                                                        httpx.Response(200, json={})))
    with TestClient(create_app(make_settings(), MemoryStore(), client)) as app:
        impossible = {**BODY, "constraints": {"min_quality": 0.99,
                                               "evidence_requirement": "allow_synthetic_demo"}}
        result = app.post("/v1/execute", headers=AUTH, json=impossible)
        assert result.status_code == 422
        assert result.json()["error"] == "no_eligible_route"
    with TestClient(create_app(make_settings(), MemoryStore(fail_insert=True), client)) as app:
        assert app.post("/v1/execute", headers=AUTH, json=BODY).status_code == 503
    assert calls == []


def test_circuit_opens_after_three_failures_and_next_request_uses_other_route():
    def upstream(request):
        if request.url.path == "/efficient":
            return httpx.Response(503, json={"detail": "down"})
        return httpx.Response(200, json=CHAT_OK)

    client = httpx.Client(transport=httpx.MockTransport(upstream))
    with TestClient(create_app(make_settings(), MemoryStore(), client)) as app:
        for _ in range(3):
            response = app.post("/v1/execute", headers=AUTH, json=BODY)
            assert response.status_code == 502
        fourth = app.post("/v1/execute", headers=AUTH, json=BODY)
        assert fourth.status_code == 200
        assert fourth.json()["target_id"] == "robust"


def test_oversized_backend_response_is_rejected():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x" * 1_048_577)))
    with TestClient(create_app(make_settings(), MemoryStore(), client)) as app:
        result = app.post("/v1/execute", headers=AUTH, json=BODY)
        assert result.status_code == 502
        assert result.json()["error"] == "upstream_response_too_large"


def test_backend_redirect_is_not_followed():
    calls = []

    def upstream(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://unregistered.example/secret"})

    client = httpx.Client(transport=httpx.MockTransport(upstream))
    with TestClient(create_app(make_settings(), MemoryStore(), client)) as app:
        result = app.post("/v1/execute", headers=AUTH, json=BODY)
        assert result.status_code == 502
        assert result.json()["error"] == "upstream_http_error"
    assert calls == ["http://backend/efficient"]


def test_expired_profile_is_returned_as_unknown_not_as_joules():
    settings = make_settings()
    route = settings.routes["efficient"]
    expired = replace(route.target, evidence="profiled",
                      profile_expires_at=datetime.now(UTC) - timedelta(days=1))
    settings.routes["efficient"] = replace(route, target=expired)
    settings.routes.pop("robust")
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=CHAT_OK)))
    with TestClient(create_app(settings, MemoryStore(), client)) as app:
        body = {**BODY, "constraints": {"evidence_requirement": "allow_unknown"}}
        response = app.post("/v1/execute", headers=AUTH, json=body)
        assert response.status_code == 200
        forecast = response.json()["forecast"]
        assert forecast["evidence"] == "unknown"
        assert forecast["declared_evidence"] == "profiled"
        assert forecast["energy_high_j"] is None


def test_embedding_contract_and_request_validation():
    settings = make_settings()
    settings.routes["embedding"] = Route(
        Target("embedding", "embedding", "retrieval.embed", "http://backend/embeddings",
               "embed-v1", "local", 0.9, 100, None, None, "unknown"),
        None, "none", None, False, 2)
    calls = []

    def upstream(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"embedding": [0.5, 1.0]}]})

    with TestClient(create_app(settings, MemoryStore(),
                               httpx.Client(transport=httpx.MockTransport(upstream)))) as app:
        assert app.get("/v1/capabilities").status_code == 401
        capabilities = app.get("/v1/capabilities", headers=AUTH)
        assert capabilities.json()["capabilities"] == [
            {"kind": "embedding", "capability": "retrieval.embed"},
            {"kind": "inference", "capability": "short_answer"}]
        body = {"kind": "embedding", "capability": "retrieval.embed", "text": "heat pump"}
        response = app.post("/v1/execute", headers=AUTH, json=body)
        assert response.status_code == 200
        assert response.json()["output"] == {"data": [{"embedding": [0.5, 1.0]}]}
        assert calls == [{"model": "embed-v1", "input": "heat pump"}]
        assert app.post("/v1/execute", headers=AUTH,
                        json={**body, "messages": [{"role": "user", "content": "x"}]}).status_code == 422


def test_http_200_with_invalid_model_shape_is_a_failed_attempt():
    store = MemoryStore()
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"error": "provider rejected model"})))
    with TestClient(create_app(make_settings(), store, client)) as app:
        response = app.post("/v1/execute", headers=AUTH, json=BODY)
        assert response.status_code == 502
        assert response.json()["error"] == "invalid_upstream_response"
        receipt = app.get(f"/v1/receipts/{response.json()['receipt_id']}", headers=AUTH)
        assert receipt.json()["status"] == "failed"


def test_embedding_nonfinite_vector_is_rejected():
    settings = make_settings()
    settings.routes["embedding"] = Route(
        Target("embedding", "embedding", "retrieval.embed", "http://backend/embeddings",
               "embed-v1", "local", 0.9, 100, None, None, "unknown"),
        None, "none", None, False, 2)
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": [{"embedding": [float("nan")]}]})))
    with TestClient(create_app(settings, MemoryStore(), client)) as app:
        response = app.post("/v1/execute", headers=AUTH,
                            json={"kind": "embedding", "capability": "retrieval.embed", "text": "x"})
        assert response.status_code == 502
        assert response.json()["error"] == "invalid_upstream_response"


def test_nested_nonfinite_model_json_fails_before_completed_receipt():
    store = MemoryStore()
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={**CHAT_OK,
                                                   "usage": {"total_tokens": float("nan")}})))
    with TestClient(create_app(make_settings(), store, client)) as app:
        response = app.post("/v1/execute", headers=AUTH, json=BODY)
        assert response.status_code == 502
        assert response.json()["error"] == "invalid_upstream_response"
        receipt = app.get(f"/v1/receipts/{response.json()['receipt_id']}", headers=AUTH)
        assert receipt.json()["status"] == "failed"


def test_missing_or_malformed_provider_usage_remains_unknown():
    for usage in (None, {"prompt_tokens": -1}, {"total_tokens": True}, {"total_tokens": "3"}):
        response_body = {"choices": [{"message": {"content": "ok"}}]}
        if usage is not None:
            response_body["usage"] = usage
        client = httpx.Client(transport=httpx.MockTransport(
            lambda request, response_body=response_body: httpx.Response(200, json=response_body)))
        with TestClient(create_app(make_settings(), MemoryStore(), client)) as app:
            result = app.post("/v1/execute", headers=AUTH, json=BODY)
            assert result.status_code == 200
            assert result.json()["token_usage"] == {"basis": "unavailable"}


def test_task_event_ingest_summary_and_tenant_scope():
    store = MemoryStore()
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(500)))
    event = {"task_id": "task-1", "event_id": "model-1", "kind": "model",
             "started_at": "2026-11-01T00:00:00Z", "ended_at": "2026-11-01T00:00:01Z",
             "prompt_tokens": 10, "completion_tokens": 2, "cost_usd": 0.01}
    with TestClient(create_app(make_settings(), store, client)) as app:
        assert app.post("/v1/tasks/events", json=event).status_code == 401
        created = app.post("/v1/tasks/events", headers=AUTH, json=event)
        assert created.status_code == 200 and created.json()["status"] == "created"
        duplicate = app.post("/v1/tasks/events", headers=AUTH, json=event)
        assert duplicate.status_code == 200 and duplicate.json()["status"] == "duplicate"
        changed = {**event, "prompt_tokens": 12}
        assert app.post("/v1/tasks/events", headers=AUTH, json=changed).status_code == 409
        assert app.post("/v1/tasks/events", headers=AUTH,
                        json={**event, "event_id": "bad", "prompt": "secret"}).status_code == 422
        summary = app.get("/v1/tasks/task-1/summary", headers=AUTH)
        assert summary.status_code == 200
        assert summary.json()["provider_tokens"]["prompt"] == 10
        assert app.get("/v1/tasks/task-1/summary",
                       headers={"Authorization": "Bearer key-b"}).status_code == 404


def test_action_policy_endpoint_and_progressive_tool_search():
    settings = make_settings()
    settings.routes["lookup"] = Route(
        Target("lookup", "tool", "catalog.lookup", "http://backend/lookup",
               "tool-v1", "local", 0.9, 100, None, None, "unknown"),
        None, "none", None, False, 2)
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(500)))
    with TestClient(create_app(settings, MemoryStore(), client)) as app:
        search = app.get("/v1/tools/search?q=catalog", headers=AUTH)
        assert search.status_code == 200
        assert search.json()["tools"] == [{"capability": "catalog.lookup"}]
        body = {"budget": {"min_quality": 0.9, "max_total_tokens": 20},
                "state": {"spent_tokens": 5},
                "candidates": [{"id": "small", "action": "answer", "predicted_quality": 0.95,
                                "quality_eval_id": "eval-1", "forecast_tokens": 10,
                                "forecast_cost_usd": 0.01, "forecast_ms": 100}]}
        decision = app.post("/v1/actions/choose", headers=AUTH, json=body)
        assert decision.status_code == 200 and decision.json()["selected_id"] == "small"
        assert decision.json()["execution"] == "caller_owned"
        body["state"] = {"spent_tokens": None}
        rejected = app.post("/v1/actions/choose", headers=AUTH, json=body)
        assert rejected.status_code == 200 and rejected.json()["selected_id"] is None
