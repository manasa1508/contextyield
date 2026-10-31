"""Two-step caller example; the agent owns the workflow, ContextYield owns each route."""

from __future__ import annotations

import json
import os
import uuid

import httpx


def main() -> None:
    base_url = os.getenv("CY_GATEWAY_URL", "http://127.0.0.1:18080")
    key = os.environ["CY_GATEWAY_KEY"]
    run_id = str(uuid.uuid4())
    receipts = []
    with httpx.Client(base_url=base_url, timeout=30) as client:
        def step(name: str, body: dict) -> dict:
            response = client.post("/v1/execute", json=body,
                                   headers={"Authorization": f"Bearer {key}",
                                            "Idempotency-Key": f"{run_id}:{name}"})
            response.raise_for_status()
            result = response.json()
            receipts.append({"step": name, "receipt_id": result["receipt_id"],
                             "target_id": result["target_id"], "forecast": result["forecast"]})
            return result

        lookup = step("lookup", {"kind": "tool", "capability": "catalog.lookup",
                                 "input": {"sku": "A-100"},
                                 "constraints": {"min_quality": 0.95,
                                                 "evidence_requirement": "allow_synthetic_demo"}})
        item = lookup["output"]["result"]
        answer = step("answer", {"kind": "inference", "capability": "short_answer",
                                 "messages": [{"role": "user", "content":
                                               f"Write one sentence about {item['name']} (stock {item['stock']})."}],
                                 "constraints": {"min_quality": 0.8,
                                                 "evidence_requirement": "allow_synthetic_demo"}})

    bounds = [entry["forecast"]["energy_high_j"] for entry in receipts]
    print(json.dumps({"run_id": run_id, "receipts": receipts,
                      "forecast_upper_j": sum(bounds) if all(v is not None for v in bounds) else None,
                      "answer": answer["output"]["choices"][0]["message"]["content"],
                      "warning": "Demo forecasts are synthetic. This is a fixed workflow, not an autonomous agent."},
                     indent=2))


if __name__ == "__main__":
    main()
