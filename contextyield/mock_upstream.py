"""Deterministic demo upstreams. No response is a quality or energy measurement."""

from __future__ import annotations

import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="ContextYield mock upstream", docs_url=None, redoc_url=None)

CATALOG = {
    "A-100": {"sku": "A-100", "name": "Heat pump", "stock": 8},
    "B-200": {"sku": "B-200", "name": "Solar panel", "stock": 3},
}


class MockToolInput(BaseModel):
    input: dict[str, str]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _model_response(body: dict, route: str) -> dict:
    if not body.get("messages"):
        raise HTTPException(422, "messages required")
    if any("__FAIL_503__" in str(m.get("content", "")) for m in body["messages"]):
        raise HTTPException(503, "synthetic upstream failure")
    text = str(body["messages"][-1].get("content", ""))
    return {
        "id": f"mock-{route}", "object": "chat.completion", "model": route,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": f"[{route}] {text[:120]}"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": len(text.split()), "completion_tokens": min(len(text.split()) + 1, 30),
                  "total_tokens": min(len(text.split()) * 2 + 1, 60)},
    }


@app.post("/model/efficient")
def efficient(body: dict) -> dict:
    time.sleep(0.04)
    return _model_response(body, "mock-efficient-v1")


@app.post("/model/robust")
def robust(body: dict) -> dict:
    time.sleep(0.02)
    return _model_response(body, "mock-robust-v1")


@app.post("/embedding/local")
def embedding(body: dict) -> dict:
    if not isinstance(body.get("input"), str) or not body["input"]:
        raise HTTPException(422, "input text required")
    text = body["input"]
    return {"object": "list", "model": "mock-embedding-v1",
            "data": [{"object": "embedding", "index": 0,
                      "embedding": [float(len(text)), float(len(text.split())), 1.0]}]}


def _lookup(body: MockToolInput, source: str) -> dict:
    sku = body.input.get("sku")
    if not sku:
        raise HTTPException(422, "sku required")
    if sku == "__FAIL_503__":
        raise HTTPException(503, "synthetic upstream failure")
    return {"result": CATALOG.get(sku), "source": source}


@app.post("/tool/cache")
def cache(body: MockToolInput) -> dict:
    return _lookup(body, "cache")


@app.post("/tool/origin")
def origin(body: MockToolInput) -> dict:
    time.sleep(0.03)
    return _lookup(body, "origin")
