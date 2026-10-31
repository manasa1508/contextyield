"""Private chat evaluation and opt-in, isolated GPU-device profiling."""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any

import httpx

from .config import Route
from .energy import DeviceEnergyProbe
from .upstream import authorization_headers, chat_payload, chat_text, parse_output


def _quality_evaluation_id(route: Route, fixture_digest: str,
                           samples: list[dict[str, Any]]) -> str:
    evidence = {"route_id": route.target.id, "model_version": route.target.version,
                "fixture_sha256": fixture_digest, "scorer": "exact_substring_casefold_v1",
                "outcomes": [{"id": str(sample["id"]), "correct": bool(sample["correct"]),
                              "error_code": sample.get("error_code")} for sample in samples]}
    return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()


def run_quality_eval(route: Route, fixture_path: Path, output_path: Path | None,
                     client: httpx.Client) -> dict[str, Any]:
    """Run a private exact-substring chat check without recording prompts or responses."""
    if route.target.kind != "inference":
        raise ValueError("quality evaluation requires an inference route")
    raw = fixture_path.read_bytes()
    if len(raw) > 2_000_000:
        raise ValueError("quality fixture exceeds 2 MB")
    fixture = json.loads(raw)
    samples = fixture.get("samples") if isinstance(fixture, dict) else None
    if (not isinstance(fixture, dict) or fixture.get("schema_version") != 1
            or not isinstance(samples, list) or not 10 <= len(samples) <= 1000):
        raise ValueError("quality fixture needs schema_version=1 and 10..1000 samples")
    ids: set[str] = set()
    for sample in samples:
        if not isinstance(sample, dict) or not isinstance(sample.get("id"), str):
            raise TypeError("each sample needs a string id")
        if not sample["id"] or sample["id"] in ids:
            raise ValueError("sample ids must be unique and nonempty")
        ids.add(sample["id"])
        messages = sample.get("messages")
        if (not isinstance(messages, list) or not 1 <= len(messages) <= 50
                or any(not isinstance(message, dict)
                       or message.get("role") not in {"system", "user", "assistant"}
                       or not isinstance(message.get("content"), str)
                       or not 1 <= len(message["content"]) <= 20000 for message in messages)):
            raise ValueError("each sample needs 1..50 valid text messages")
        expected = sample.get("expected_substring")
        if not isinstance(expected, str) or not 1 <= len(expected) <= 2000:
            raise ValueError("each sample needs a nonempty expected_substring")
        tokens = sample.get("max_tokens", 256)
        if isinstance(tokens, bool) or not isinstance(tokens, int) or not 1 <= tokens <= 4096:
            raise ValueError("sample max_tokens must be an integer within 1..4096")

    headers = authorization_headers(route)
    results = []
    for sample in samples:
        payload = chat_payload(route.target.version, sample["messages"],
                               sample.get("max_tokens", 256))
        start = time.monotonic()
        correct = False
        error_code = None
        try:
            with client.stream("POST", route.target.url, json=payload, headers=headers,
                               timeout=route.timeout_s) as response:
                response.raise_for_status()
                chunks = bytearray()
                for chunk in response.iter_bytes(chunk_size=65_536):
                    chunks.extend(chunk)
                    if len(chunks) > 1_048_576:
                        raise ValueError("quality response exceeds 1 MB")
            text = chat_text(parse_output(chunks, "inference"))
            correct = sample["expected_substring"].casefold() in text.casefold()
        except httpx.HTTPStatusError as exc:
            error_code = f"http_{exc.response.status_code}"
        except httpx.TimeoutException:
            error_code = "timeout"
        except httpx.RequestError:
            error_code = "transport_error"
        except (ValueError, TypeError, OverflowError):
            error_code = "invalid_response"
        results.append({"id": sample["id"], "correct": correct,
                        "latency_ms": round((time.monotonic() - start) * 1000, 3),
                        "error_code": error_code})

    latencies = sorted(item["latency_ms"] for item in results)
    quality = sum(item["correct"] for item in results) / len(results)
    failures = sum(item["error_code"] is not None for item in results)
    fixture_digest = hashlib.sha256(raw).hexdigest()
    summary = {"route_id": route.target.id, "model_version": route.target.version,
               "fixture_sha256": fixture_digest,
               "scorer": "exact_substring_casefold_v1", "sample_count": len(results),
               "quality": quality, "failed_samples": failures,
               "p95_latency_ms": latencies[math.ceil(0.95 * len(latencies)) - 1]}
    evaluation_id = _quality_evaluation_id(route, fixture_digest, results)
    result = {"evaluation": {**summary, "quality_eval_id": evaluation_id},
              "samples": results,
              "suggested_route_fields": ({"quality": quality, "quality_eval_id": evaluation_id,
                                          "p95_ms": math.ceil(summary["p95_latency_ms"])}
                                         if failures == 0 else None),
              "warning": "Exact-substring accuracy is a limited proxy. Validate on held-out, representative tasks before using a quality floor. No energy was measured."}
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def hardware_fingerprint(device_index: int) -> str:
    """Stable-enough deployment fingerprint; does not prove exclusive usage."""
    import pynvml  # type: ignore[import-not-found]

    pynvml.nvmlInit()
    try:
        handle = pynvml.nvmlDeviceGetHandleByIndex(device_index)
        properties = [pynvml.nvmlDeviceGetName(handle), pynvml.nvmlDeviceGetUUID(handle),
                      pynvml.nvmlSystemGetDriverVersion()]
        normalized = [item.decode() if isinstance(item, bytes) else str(item) for item in properties]
        return hashlib.sha256("|".join(normalized).encode()).hexdigest()
    finally:
        pynvml.nvmlShutdown()


def summarize(samples: list[dict[str, Any]], route: Route, fixture_digest: str,
              hardware: str) -> dict[str, Any]:
    if len(samples) < 10:
        raise ValueError("at least 10 successful isolated samples are required")
    if any(sample["observation"]["basis"] != "device_counter_window" or
           not sample["observation"]["attributable_to_request"] for sample in samples):
        raise ValueError("all samples need valid exclusive GPU device windows")
    energies = [float(sample["observation"]["joules"]) for sample in samples]
    latencies = sorted(float(sample["latency_ms"]) for sample in samples)
    if (not all(math.isfinite(value) and value > 0 for value in energies)
            or not all(math.isfinite(value) and value >= 0 for value in latencies)):
        raise ValueError("invalid meter or latency values")
    quality = mean(float(sample["correct"]) for sample in samples)
    quality_eval_id = _quality_evaluation_id(route, fixture_digest, samples)
    profile_core = {
        "route_id": route.target.id, "model_version": route.target.version,
        "hardware_fingerprint": hardware, "fixture_sha256": fixture_digest,
        "method": "nvml_total_energy_counter_gross_gpu_device_window_v1",
        "sample_count": len(samples), "benchmark_quality_exact_substring": quality,
        "quality_eval_id": quality_eval_id,
        "empirical_min_j": min(energies), "empirical_max_j": max(energies),
        "empirical_p95_latency_ms": latencies[math.ceil(0.95 * len(latencies)) - 1],
    }
    profile_id = hashlib.sha256(json.dumps(profile_core, sort_keys=True).encode()).hexdigest()
    return {
        "profile": {**profile_core, "profile_id": profile_id,
                    "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
                    "caveat": "Empirical gross GPU-device window range, not a guaranteed bound or facility energy."},
        "suggested_route_fields": {
            "quality": quality, "quality_eval_id": quality_eval_id,
            "p95_ms": math.ceil(profile_core["empirical_p95_latency_ms"]),
            "energy_low_j": min(energies), "energy_high_j": max(energies),
            "evidence": "profiled", "profile_id": profile_id,
            "profile_expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
            "profile_hardware": hardware,
        },
        "samples": [{"id": sample["id"], "latency_ms": sample["latency_ms"],
                     "gross_gpu_joules": sample["observation"]["joules"],
                     "correct": sample["correct"]} for sample in samples],
        "warning": "Operator must validate workload representativeness and hardware/load match before routing.",
    }


def run_profile(route: Route, fixture_path: Path, output_path: Path) -> dict[str, Any]:
    if route.target.kind != "inference" or not route.meter or not route.exclusive_device:
        raise ValueError("profiling requires an inference route with meter and exclusive_device=true")
    raw = fixture_path.read_bytes()
    if len(raw) > 2_000_000:
        raise ValueError("profile fixture exceeds 2 MB")
    fixture = json.loads(raw)
    if fixture.get("schema_version") != 1 or len(fixture.get("samples", [])) < 10:
        raise ValueError("fixture needs schema_version=1 and at least 10 samples")
    index = int(route.meter.split(":", 1)[1])
    hardware = hardware_fingerprint(index)
    probe = DeviceEnergyProbe(route.meter, True)
    headers = authorization_headers(route)
    samples = []
    with httpx.Client(timeout=route.timeout_s) as client:
        for sample in fixture["samples"]:
            if not isinstance(sample.get("messages"), list) or not sample.get("expected_substring"):
                raise ValueError("each sample needs messages and expected_substring")
            payload = chat_payload(route.target.version, sample["messages"],
                                   int(sample.get("max_tokens", 256)))
            start = time.monotonic()
            window = probe.start()
            if window.start_mj is None:
                raise RuntimeError(f"meter unavailable: {window.error}")
            response = client.post(route.target.url, json=payload, headers=headers)
            observation = probe.finish(window)
            response.raise_for_status()
            output_text = chat_text(parse_output(response.content, "inference"))
            samples.append({"id": str(sample["id"]),
                            "latency_ms": (time.monotonic() - start) * 1000,
                            "correct": sample["expected_substring"].casefold() in output_text.casefold(),
                            "observation": observation})
    result = summarize(samples, route, hashlib.sha256(raw).hexdigest(), hardware)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result
