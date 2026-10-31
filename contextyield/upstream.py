"""Wire contracts for the supported OpenAI-compatible model endpoint types."""

from __future__ import annotations

import base64
import json
import math
import os

from .config import Route


def authorization_headers(route: Route) -> dict[str, str]:
    if route.auth_type == "none":
        return {}
    secret = os.getenv(route.key_env or "")
    if not secret:
        raise RuntimeError(f"missing secret for registered target {route.target.id}")
    if route.auth_type == "bearer":
        return {"Authorization": f"Bearer {secret}"}
    return {"Authorization": "Basic " + base64.b64encode(secret.encode()).decode()}


def chat_payload(model: str, messages: list[dict[str, str]], max_tokens: int) -> dict:
    return {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": False}


def embedding_payload(model: str, text: str) -> dict:
    return {"model": model, "input": text}


def chat_text(output: dict) -> str:
    try:
        content = output["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("chat completion lacks choices[0].message.content") from exc
    if not isinstance(content, str):
        raise TypeError("chat completion content must be text")
    return content


def validate_output(kind: str, output: dict) -> None:
    if kind == "inference":
        chat_text(output)
    elif kind == "embedding":
        try:
            data = output["data"]
            vector = data[0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("embedding response lacks data[0].embedding") from exc
        if (not isinstance(data, list) or len(data) != 1 or not isinstance(vector, list)
                or not vector or any(isinstance(item, bool) or not isinstance(item, (int, float))
                                      or not math.isfinite(item) for item in vector)):
            raise ValueError("embedding response must contain one finite numeric vector")
    elif kind != "tool":
        raise ValueError("unsupported output kind")


def parse_output(raw: bytes, kind: str) -> dict:
    output = json.loads(raw)
    if not isinstance(output, dict):
        raise TypeError("upstream response must be a JSON object")
    json.dumps(output, allow_nan=False)
    validate_output(kind, output)
    return output


def reported_usage(output: dict | None) -> dict:
    """Return only non-sensitive provider token counters, or an explicit unknown.

    Providers may omit usage or use incompatible schemas. Such values must not
    become a zero-token claim or a derived energy estimate.
    """
    raw = output.get("usage") if output is not None else None
    if not isinstance(raw, dict):
        return {"basis": "unavailable"}
    fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    counts = {key: raw[key] for key in fields
              if isinstance(raw.get(key), int) and not isinstance(raw[key], bool)
              and raw[key] >= 0}
    if not counts:
        return {"basis": "unavailable"}
    return {"basis": "provider_reported", **counts}
