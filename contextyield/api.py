"""Synchronous HTTP contract over a durable, evidence-aware routing decision."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Annotated, Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .action_policy import ActionCandidate, TaskBudget, TaskState, choose_action
from .config import Route, Settings, load_settings
from .core import Constraints, choose
from .energy import DeviceEnergyProbe
from .store import ReceiptStore
from .trajectory import TaskEvent, summarize
from .upstream import (
    authorization_headers,
    chat_payload,
    embedding_payload,
    parse_output,
    reported_usage,
)

REQUESTS = Counter("contextyield_requests_total", "Requests by kind and outcome", ["kind", "outcome"])
LATENCY = Histogram("contextyield_upstream_seconds", "Upstream call duration", ["target"])
TASK_EVENTS = Counter("contextyield_task_events_total", "Task events by ingest result", ["outcome"])
LOG = logging.getLogger("contextyield")


def _log_event(event: str, **fields: object) -> None:
    """Structured metadata only: no messages, tool input/output, or credentials."""
    LOG.info(json.dumps({"event": event, **fields}, separators=(",", ":"), sort_keys=True))


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=20000)


class ConstraintBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_quality: float = Field(default=0.0, ge=0, le=1)
    max_p95_ms: int | None = Field(default=None, gt=0)
    max_energy_j: float | None = Field(default=None, gt=0)
    evidence_requirement: Literal["profiled", "allow_synthetic_demo", "allow_unknown"] = "allow_unknown"
    region: str | None = Field(default=None, max_length=80)


class ExecuteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["inference", "embedding", "tool"]
    capability: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")
    messages: list[Message] | None = Field(default=None, max_length=50)
    text: str | None = Field(default=None, min_length=1, max_length=20000)
    input: dict[str, str] | None = None
    max_tokens: int = Field(default=256, ge=1, le=4096)
    constraints: ConstraintBody = Field(default_factory=ConstraintBody)

    @model_validator(mode="after")
    def validate_payload(self) -> ExecuteBody:
        if self.kind == "inference" and (not self.messages or self.text is not None
                                          or self.input is not None):
            raise ValueError("inference requires messages and forbids text and input")
        if self.kind == "embedding" and (self.text is None or self.messages is not None
                                          or self.input is not None):
            raise ValueError("embedding requires text and forbids messages and input")
        if self.kind == "tool" and (self.input is None or self.messages is not None
                                     or self.text is not None):
            raise ValueError("tool requires input and forbids messages and text")
        if self.input is not None and (len(self.input) > 20 or
                                       any(len(k) > 80 or len(v) > 1000 for k, v in self.input.items())):
            raise ValueError("tool input too large")
        return self


class ActionBudgetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_quality: float = Field(ge=0, le=1, allow_inf_nan=False)
    max_steps: int | None = Field(default=None, ge=0)
    max_total_tokens: int | None = Field(default=None, ge=0)
    max_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_wall_ms: int | None = Field(default=None, ge=0)
    max_energy_j: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    allowed_actions: list[Literal["answer", "retrieve", "search_tool", "decision_model",
                                  "strong_model", "reuse_evidence", "human", "abstain"]] = Field(
        default_factory=lambda: ["answer", "retrieve", "search_tool", "decision_model",
                                 "strong_model", "reuse_evidence", "human", "abstain"],
        max_length=8,
    )


class ActionStateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: int = Field(default=0, ge=0)
    spent_tokens: int | None = Field(default=0, ge=0)
    spent_cost_usd: float | None = Field(default=0.0, ge=0, allow_inf_nan=False)
    elapsed_ms: int = Field(default=0, ge=0)
    spent_energy_j: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ActionCandidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    action: Literal["answer", "retrieve", "search_tool", "decision_model", "strong_model",
                    "reuse_evidence", "human", "abstain"]
    predicted_quality: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    quality_eval_id: str | None = Field(default=None, max_length=128)
    forecast_tokens: int | None = Field(default=None, ge=0)
    forecast_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    forecast_ms: int | None = Field(default=None, ge=0)
    energy_high_j: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    energy_evidence: Literal["unknown", "profiled"] = "unknown"


class ActionChooseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    budget: ActionBudgetBody
    state: ActionStateBody
    candidates: list[ActionCandidateBody] = Field(max_length=100)
    advisor_preference: str | None = Field(default=None, max_length=128)


class CircuitRegistry:
    """Per-process failure isolation; production ingress balances across replicas."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    def available(self, target_id: str) -> bool:
        with self._lock:
            return time.monotonic() >= self._open_until.get(target_id, 0)

    def record(self, target_id: str, success: bool) -> None:
        with self._lock:
            if success:
                self._failures[target_id] = 0
                self._open_until.pop(target_id, None)
            else:
                failures = self._failures.get(target_id, 0) + 1
                self._failures[target_id] = failures
                if failures >= 3:
                    self._open_until[target_id] = time.monotonic() + 30


def _tenant(authorization: str | None, settings: Settings) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Bearer API key required", headers={"WWW-Authenticate": "Bearer"})
    supplied = authorization[7:]
    for tenant, expected in settings.api_keys.items():
        if hmac.compare_digest(supplied, expected):
            return tenant
    raise HTTPException(401, "invalid API key", headers={"WWW-Authenticate": "Bearer"})


def _invoke(route: Route, body: ExecuteBody, client: httpx.Client) -> tuple[dict | None, dict]:
    probe = DeviceEnergyProbe(route.meter, route.exclusive_device)
    if body.kind == "inference":
        payload = chat_payload(route.target.version, [m.model_dump() for m in body.messages or []],
                               body.max_tokens)
    elif body.kind == "embedding":
        payload = embedding_payload(route.target.version, body.text or "")
    else:
        payload = {"input": body.input}
    start = time.monotonic()
    window = probe.start()
    http_status: int | None = None
    error_code: str | None = None
    output: dict | None = None
    try:
        headers = authorization_headers(route)
        with client.stream("POST", route.target.url, headers=headers, json=payload,
                           timeout=route.timeout_s) as response:
            http_status = response.status_code
            if not 200 <= response.status_code < 300:
                error_code = "upstream_http_error"
            else:
                chunks = bytearray()
                for chunk in response.iter_bytes(chunk_size=65_536):
                    chunks.extend(chunk)
                    if len(chunks) > 1_048_576:
                        error_code = "upstream_response_too_large"
                        break
                if error_code is None:
                    output = parse_output(chunks, body.kind)
    except httpx.TimeoutException:
        error_code = "upstream_timeout_ambiguous"
    except httpx.RequestError:
        error_code = "upstream_transport_ambiguous"
    except (ValueError, TypeError, OverflowError):
        error_code = "invalid_upstream_response"
        output = None
    except RuntimeError:
        error_code = "upstream_credentials_unavailable"
    elapsed_ms = max(0, int((time.monotonic() - start) * 1000))
    observation = probe.finish(window)
    LATENCY.labels(route.target.id).observe(elapsed_ms / 1000)
    return output, {"target_id": route.target.id, "status": "completed" if output is not None else "failed",
                    "elapsed_ms": elapsed_ms, "http_status": http_status, "error_code": error_code,
                    "observation": observation}


def create_app(settings: Settings | None = None, store: ReceiptStore | None = None,
               client: httpx.Client | None = None) -> FastAPI:
    if not LOG.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        LOG.addHandler(handler)
    LOG.propagate = False
    LOG.setLevel(logging.INFO)
    settings = settings or load_settings()
    store = store or ReceiptStore(settings.database_url)
    circuit = CircuitRegistry()
    slots = {target_id: threading.BoundedSemaphore(1 if route.meter and route.exclusive_device else 8)
             for target_id, route in settings.routes.items()}
    meter_slots = {route.meter: threading.Lock() for route in settings.routes.values()
                   if route.meter and route.exclusive_device}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.open()
        owned_client = client is None
        app.state.client = client or httpx.Client()
        app.state.store = store
        app.state.settings = settings
        try:
            yield
        finally:
            if owned_client:
                app.state.client.close()
            store.close()

    app = FastAPI(title="ContextYield", version="0.2.0", lifespan=lifespan)

    @app.middleware("http")
    async def body_limit(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH"}:
            try:
                content_length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"detail": "invalid Content-Length"}, status_code=400)
            if content_length > 65536:
                return JSONResponse({"detail": "body too large"}, status_code=413)
            if len(await request.body()) > 65536:
                return JSONResponse({"detail": "body too large"}, status_code=413)
        return await call_next(request)

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready() -> Response:
        if not store.ready():
            return JSONResponse({"status": "database_unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.get("/metrics")
    def metrics(authorization: Annotated[str | None, Header()] = None) -> Response:
        _tenant(authorization, settings)
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/v1/capabilities")
    def capabilities(authorization: Annotated[str | None, Header()] = None) -> dict:
        _tenant(authorization, settings)
        available = sorted({(route.target.kind, route.target.capability)
                            for route in settings.routes.values() if route.target.enabled})
        return {"capabilities": [{"kind": kind, "capability": capability}
                                 for kind, capability in available]}

    @app.get("/v1/tools/search")
    def search_tools(q: str, authorization: Annotated[str | None, Header()] = None,
                     limit: int = 5) -> dict:
        _tenant(authorization, settings)
        if not 1 <= len(q) <= 80 or not 1 <= limit <= 20:
            raise HTTPException(400, "q length must be 1..80 and limit 1..20")
        terms = q.lower().split()
        capabilities = sorted({route.target.capability for route in settings.routes.values()
                               if route.target.kind == "tool" and route.target.enabled})
        matches = [name for name in capabilities if all(term in name.lower() for term in terms)]
        return {"tools": [{"capability": name} for name in matches[:limit]],
                "total_matches": len(matches), "basis": "registered_capability_name"}

    @app.post("/v1/actions/choose")
    def choose_next_action(body: ActionChooseBody,
                           authorization: Annotated[str | None, Header()] = None) -> dict:
        _tenant(authorization, settings)
        raw_budget = body.budget.model_dump()
        raw_budget["allowed_actions"] = tuple(raw_budget["allowed_actions"])
        try:
            decision = choose_action(TaskBudget(**raw_budget), TaskState(**body.state.model_dump()),
                                     [ActionCandidate(**item.model_dump()) for item in body.candidates],
                                     body.advisor_preference)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {**asdict(decision), "basis": "operator_forecasts_not_observed_quality",
                "execution": "caller_owned"}

    @app.post("/v1/tasks/events")
    def ingest_task_event(event: TaskEvent,
                          authorization: Annotated[str | None, Header()] = None) -> dict:
        tenant = _tenant(authorization, settings)
        try:
            created = store.insert_task_event(tenant, event.model_dump(mode="json"))
        except ValueError as exc:
            TASK_EVENTS.labels("conflict").inc()
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            TASK_EVENTS.labels("ledger_unavailable").inc()
            raise HTTPException(503, "task ledger unavailable") from exc
        TASK_EVENTS.labels("created" if created else "duplicate").inc()
        return {"task_id": event.task_id, "event_id": event.event_id,
                "status": "created" if created else "duplicate"}

    @app.get("/v1/tasks/{task_id}/summary")
    def task_summary(task_id: str, authorization: Annotated[str | None, Header()] = None) -> dict:
        tenant = _tenant(authorization, settings)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", task_id):
            raise HTTPException(400, "invalid task_id")
        try:
            events = [TaskEvent.model_validate(item)
                      for item in store.get_task_events(tenant, task_id)]
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, "task ledger unavailable") from exc
        if not events:
            raise HTTPException(404, "task not found")
        return summarize(events)["tasks"][0]

    @app.post("/v1/execute")
    def execute(body: ExecuteBody, authorization: Annotated[str | None, Header()] = None,
                idempotency_key: Annotated[str | None, Header()] = None) -> Response:
        tenant = _tenant(authorization, settings)
        if idempotency_key is not None and not 1 <= len(idempotency_key) <= 128:
            raise HTTPException(400, "Idempotency-Key length must be 1..128")
        if settings.mode == "production" and body.constraints.evidence_requirement == "allow_synthetic_demo":
            raise HTTPException(400, "synthetic evidence is unavailable in production")
        constraints = Constraints(**body.constraints.model_dump())
        open_ids = [route.target.id for route in settings.routes.values()
                    if not circuit.available(route.target.id)]
        targets = [route.target for route in settings.routes.values()
                   if route.target.id not in open_ids]
        decision = choose(targets, body.kind, body.capability, constraints)
        selected = settings.routes.get(decision.selected_id) if decision.selected_id else None
        snapshot = {
            "kind": body.kind, "capability": body.capability, "policy": "min_upper_joules_v1",
            "registry_digest": settings.registry_digest, "selected_id": decision.selected_id,
            "reason": decision.reason, "candidates": [asdict(c) for c in decision.candidates],
            "circuit_open": open_ids,
            "constraints": body.constraints.model_dump(),
        }
        canonical = json.dumps(body.model_dump(), sort_keys=True, separators=(",", ":"))
        request_hash = hmac.new(settings.hash_key.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        try:
            inserted = store.insert_decision(tenant, idempotency_key, request_hash, snapshot)
        except Exception as exc:
            REQUESTS.labels(body.kind, "ledger_unavailable").inc()
            _log_event("ledger_unavailable", kind=body.kind)
            raise HTTPException(503, "ledger unavailable before dispatch") from exc
        receipt_id = inserted.receipt_id
        response_headers = {"X-ContextYield-Receipt-ID": receipt_id}
        if not inserted.created:
            code = 409 if inserted.same_request else 422
            reason = "duplicate_request_not_redispatched" if inserted.same_request else "idempotency_key_reused"
            return JSONResponse({"receipt_id": receipt_id, "error": reason}, status_code=code,
                                headers=response_headers)
        if selected is None:
            store.finish(receipt_id, "rejected", {"reason": "no_eligible_route"})
            REQUESTS.labels(body.kind, "rejected").inc()
            _log_event("request_rejected", receipt_id=receipt_id, kind=body.kind,
                       reason="no_eligible_route")
            return JSONResponse({"receipt_id": receipt_id, "error": "no_eligible_route",
                                 "candidates": snapshot["candidates"], "circuit_open": open_ids},
                                status_code=422, headers=response_headers)
        if not slots[selected.target.id].acquire(blocking=False):
            store.finish(receipt_id, "rejected", {"reason": "target_capacity_exhausted"})
            REQUESTS.labels(body.kind, "overloaded").inc()
            _log_event("request_rejected", receipt_id=receipt_id, kind=body.kind,
                       reason="target_capacity_exhausted")
            return JSONResponse({"receipt_id": receipt_id, "error": "target_capacity_exhausted"},
                                status_code=429, headers={**response_headers, "Retry-After": "1"})
        meter_slot = meter_slots.get(selected.meter) if selected.exclusive_device else None
        if meter_slot is not None and not meter_slot.acquire(blocking=False):
            slots[selected.target.id].release()
            store.finish(receipt_id, "rejected", {"reason": "meter_capacity_exhausted"})
            REQUESTS.labels(body.kind, "overloaded").inc()
            _log_event("request_rejected", receipt_id=receipt_id, kind=body.kind,
                       reason="meter_capacity_exhausted")
            return JSONResponse({"receipt_id": receipt_id, "error": "meter_capacity_exhausted"},
                                status_code=429, headers={**response_headers, "Retry-After": "1"})
        try:
            output, attempt = _invoke(selected, body, app.state.client)
        finally:
            if meter_slot is not None:
                meter_slot.release()
            slots[selected.target.id].release()
        success = output is not None
        circuit.record(selected.target.id, success)
        status = "completed" if success else "failed"
        outcome = {"target_id": selected.target.id, "error_code": attempt["error_code"],
                   "observation": attempt["observation"],
                   "token_usage": reported_usage(output)}
        try:
            store.finish(receipt_id, status, outcome, attempt)
        except Exception as exc:
            REQUESTS.labels(body.kind, "ledger_gap").inc()
            _log_event("ledger_gap", receipt_id=receipt_id, kind=body.kind,
                       target_id=selected.target.id)
            raise HTTPException(503, f"outcome not durably recorded; receipt_id={receipt_id}") from exc
        REQUESTS.labels(body.kind, status).inc()
        _log_event("request_finished", receipt_id=receipt_id, kind=body.kind,
                   target_id=selected.target.id, status=status,
                   elapsed_ms=attempt["elapsed_ms"], error_code=attempt["error_code"])
        if not success:
            return JSONResponse({"receipt_id": receipt_id, "error": attempt["error_code"],
                                 "http_status": attempt["http_status"]},
                                status_code=502, headers=response_headers)
        selected_candidate = next(item for item in decision.candidates if item.id == selected.target.id)
        usable_energy = selected_candidate.energy_high_j is not None
        forecast = {"energy_low_j": selected.target.energy_low_j if usable_energy else None,
                    "energy_high_j": selected_candidate.energy_high_j,
                    "evidence": selected.target.evidence if usable_energy else "unknown",
                    "declared_evidence": selected.target.evidence,
                    "profile_id": selected.target.profile_id,
                    "benchmark_quality": selected.target.quality,
                    "forecast_p95_ms": selected.target.p95_ms}
        return JSONResponse({"receipt_id": receipt_id, "target_id": selected.target.id,
                             "output": output, "forecast": forecast,
                             "observation": attempt["observation"],
                             "token_usage": outcome["token_usage"]}, headers=response_headers)

    @app.get("/v1/receipts/{receipt_id}")
    def receipt(receipt_id: str, authorization: Annotated[str | None, Header()] = None) -> dict:
        tenant = _tenant(authorization, settings)
        row = store.get(tenant, receipt_id)
        if row is None:
            raise HTTPException(404, "receipt not found")
        return row

    return app


app = create_app()
