"""Operator configuration must not smuggle synthetic evidence into production."""

from __future__ import annotations

import json

import pytest

from contextyield.config import load_settings


def test_production_rejects_synthetic_profile(monkeypatch, tmp_path):
    registry = {"schema_version": 1, "routes": [{
        "id": "x", "kind": "inference", "capability": "short_answer",
        "url": "https://model.example.com/v1/chat/completions", "version": "v1",
        "region": "local", "quality": 0.8, "p95_ms": 100,
        "energy_low_j": 1, "energy_high_j": 2, "evidence": "synthetic_demo",
    }]}
    path = tmp_path / "routes.json"
    path.write_text(json.dumps(registry))
    monkeypatch.setenv("CY_REGISTRY", str(path))
    monkeypatch.setenv("CY_MODE", "production")
    monkeypatch.setenv("CY_API_KEYS_JSON", '{"team":"not-a-demo-key"}')
    with pytest.raises(ValueError, match="synthetic"):
        load_settings()


def test_production_rejects_plain_http_and_demo_key(monkeypatch, tmp_path):
    registry = {"schema_version": 1, "routes": [{
        "id": "x", "kind": "inference", "capability": "short_answer",
        "url": "http://model.example.com/v1/chat/completions", "version": "v1",
        "region": "local", "quality": 0.0, "p95_ms": 100,
        "energy_low_j": None, "energy_high_j": None, "evidence": "unknown",
    }]}
    path = tmp_path / "routes.json"
    path.write_text(json.dumps(registry))
    monkeypatch.setenv("CY_REGISTRY", str(path))
    monkeypatch.setenv("CY_MODE", "production")
    monkeypatch.setenv("CY_API_KEYS_JSON", '{"team":"key"}')
    with pytest.raises(ValueError, match="HTTPS"):
        load_settings()
    registry["routes"][0]["url"] = "https://model.example.com/v1/chat/completions"
    path.write_text(json.dumps(registry))
    monkeypatch.setenv("CY_API_KEYS_JSON", '{"team":"dev-local-key"}')
    with pytest.raises(ValueError, match="non-demo"):
        load_settings()


def test_registry_rejects_password_with_empty_username(monkeypatch, tmp_path):
    registry = {"schema_version": 1, "routes": [{
        "id": "x", "kind": "inference", "capability": "short_answer",
        "url": "https://:sensitive-password@model.example/v1/chat/completions",
        "version": "v1", "region": "local", "quality": 0.0, "p95_ms": 100,
        "energy_low_j": None, "energy_high_j": None, "evidence": "unknown"}]}
    path = tmp_path / "routes.json"
    path.write_text(json.dumps(registry))
    monkeypatch.setenv("CY_REGISTRY", str(path))
    with pytest.raises(ValueError, match="invalid registered URL"):
        load_settings()
