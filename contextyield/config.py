"""Operator-owned, versioned route registry. Callers cannot supply upstream URLs."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .core import Constraints, Target, choose


@dataclass(frozen=True)
class Route:
    target: Target
    key_env: str | None
    auth_type: str
    meter: str | None
    exclusive_device: bool
    timeout_s: float


@dataclass(frozen=True)
class Settings:
    mode: str
    database_url: str
    api_keys: dict[str, str]
    routes: dict[str, Route]
    registry_digest: str
    hash_key: str


def _validate_route(raw: dict[str, Any], mode: str) -> Route:
    if raw.get("kind") not in {"inference", "embedding", "tool"}:
        raise ValueError(f"unsupported route kind for {raw.get('id')}")
    url = str(raw["url"])
    parsed = urlparse(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise ValueError(f"invalid registered URL for {raw.get('id')}")
    if mode == "production" and parsed.scheme != "https":
        raise ValueError(f"production route {raw.get('id')} must use HTTPS")
    if raw["kind"] == "tool" and not raw.get("read_only", False):
        raise ValueError(f"tool route {raw.get('id')} must attest read_only=true")
    auth_type = raw.get("auth_type", "none")
    if auth_type not in {"none", "bearer", "basic"}:
        raise ValueError(f"unsupported auth_type for {raw.get('id')}")
    if auth_type != "none" and not raw.get("key_env"):
        raise ValueError(f"missing key_env for {raw.get('id')}")
    secret = os.getenv(str(raw["key_env"])) if auth_type != "none" else None
    if auth_type != "none" and not secret:
        raise ValueError(f"missing configured upstream secret for {raw.get('id')}")
    if auth_type == "basic" and secret and ":" not in secret:
        raise ValueError(f"basic secret for {raw.get('id')} must be username:password")
    if mode == "production" and raw.get("evidence") in {"synthetic", "synthetic_demo"}:
        raise ValueError("synthetic energy profiles are forbidden in production")
    if mode == "production" and float(raw.get("quality", 0)) > 0 and not raw.get("quality_eval_id"):
        raise ValueError("production quality claims need quality_eval_id")
    timeout_s = float(raw.get("timeout_s", 10))
    if not 0 < timeout_s <= 120:
        raise ValueError("route timeout_s must be within (0, 120]")
    meter = raw.get("meter")
    if meter and not str(meter).startswith("nvml:"):
        raise ValueError("only nvml:<device-index> meter is supported")
    if meter and not raw.get("meter_backend_co_located", False):
        raise ValueError("meter requires meter_backend_co_located=true attestation")
    if raw.get("exclusive_device") and not meter:
        raise ValueError("exclusive_device requires meter")
    expiry = raw.get("profile_expires_at")
    if isinstance(expiry, str):
        expiry = datetime.fromisoformat(expiry)
    if mode == "production" and raw.get("evidence") in {"profiled", "benchmark_profile"}:
        if not expiry or not raw.get("profile_id") or not raw.get("quality_eval_id"):
            raise ValueError("production profiled routes need expiry, profile_id, quality_eval_id")
        fingerprint = os.getenv("CY_HARDWARE_FINGERPRINT")
        if not fingerprint or raw.get("profile_hardware") != fingerprint:
            raise ValueError("production profiled route hardware fingerprint mismatch")
    target = Target(
        id=str(raw["id"]),
        kind=str(raw["kind"]),
        capability=str(raw["capability"]),
        url=url,
        version=str(raw["version"]),
        region=str(raw["region"]),
        quality=float(raw["quality"]),
        p95_ms=int(raw["p95_ms"]),
        energy_low_j=float(raw["energy_low_j"]) if raw.get("energy_low_j") is not None else None,
        energy_high_j=float(raw["energy_high_j"]) if raw.get("energy_high_j") is not None else None,
        evidence=str(raw["evidence"]),
        profile_id=raw.get("profile_id"),
        profile_expires_at=expiry,
        priority=int(raw.get("priority", 100)),
        enabled=bool(raw.get("enabled", True)),
    )
    return Route(target, raw.get("key_env"), auth_type, meter,
                 bool(raw.get("exclusive_device", False)), timeout_s)


def load_settings() -> Settings:
    mode = os.getenv("CY_MODE", "demo")
    if mode not in {"demo", "production"}:
        raise ValueError("CY_MODE must be demo or production")
    registry_path = Path(os.getenv("CY_REGISTRY", "config/routes.demo.json"))
    raw_bytes = registry_path.read_bytes()
    document = json.loads(raw_bytes)
    if document.get("schema_version") != 1:
        raise ValueError("unsupported registry schema_version")
    routes = [_validate_route(item, mode) for item in document["routes"]]
    route_map = {route.target.id: route for route in routes}
    if len(route_map) != len(routes) or not routes:
        raise ValueError("registry routes must be nonempty and have unique IDs")
    choose((route.target for route in routes), "inference", "__validation__", Constraints())
    keys = json.loads(os.getenv("CY_API_KEYS_JSON", '{"demo":"dev-local-key"}'))
    if not isinstance(keys, dict) or not keys or any(not k or not v for k, v in keys.items()):
        raise ValueError("CY_API_KEYS_JSON must map tenant IDs to nonempty keys")
    if len(set(keys.values())) != len(keys):
        raise ValueError("API keys must be unique across tenants")
    if mode == "production" and ("dev-local-key" in keys.values() or not os.getenv("CY_API_KEYS_JSON")):
        raise ValueError("production needs non-demo API keys")
    hash_key = os.getenv("CY_HASH_KEY", "demo-only-hash-key")
    if mode == "production" and (hash_key == "demo-only-hash-key" or len(hash_key) < 32):
        raise ValueError("production needs a stable CY_HASH_KEY of at least 32 characters")
    database_url = os.getenv("CY_DATABASE_URL", "postgresql://contextyield:contextyield@localhost:5432/contextyield")
    return Settings(mode, database_url, keys, route_map, hashlib.sha256(raw_bytes).hexdigest(), hash_key)
