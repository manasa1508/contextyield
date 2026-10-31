# ContextYield requirements and acceptance contract

This document separates what v0.2 implements from the evidence needed for a real deployment. The primary user is an engineer comparing registered model or read-only tool implementations without confusing estimated energy with measurements.

## Functional requirements

| ID | Requirement | v0.2 acceptance evidence |
| --- | --- | --- |
| FR-01 | Accept one authenticated inference request with text messages and a named capability; call one registered OpenAI-compatible non-streaming backend. | Compose smoke and `tests/test_api.py`. |
| FR-02 | Accept one explicitly invoked, registered read-only tool capability with a bounded string map; never execute model-emitted tool text. | Compose smoke and registry validation. |
| FR-03 | Apply hard quality, p95 forecast, region, energy-cap, and evidence-class constraints before deterministic ranking. Do not relax constraints to find a route. | `tests/test_core.py`; impossible request returns 422. |
| FR-04 | Store the decision before dispatch and the attempt/outcome afterward, scoped to a tenant, without raw prompt/input/output or secret in the receipt. | `tests/test_api.py` and Compose smoke. |
| FR-05 | Reusing a tenant-scoped idempotency key must not dispatch twice; a changed body under the same key is rejected. | Unit and Compose smoke. No cached-response/exactly-once claim. |
| FR-06 | Explicitly label forecast evidence and observation scope; missing meter values are null, not zero. | `tests/test_energy.py` and Compose smoke. |
| FR-07 | Produce a deterministic full-information synthetic policy comparison with fixture/config hashes and an explicit no-physical-savings warning. | `tests/test_replay.py` and `contextyield compare`. |
| FR-08 | Permit opt-in local NVIDIA GPU device-window profiling on an operator-attested exclusive device, with an empirical profile artifact that contains no prompts. | `contextyield profile` and `tests/test_calibration.py`; real hardware validation remains an operator gate. |
| FR-09 | Accept a single-text embedding request through a registered OpenAI-compatible `/v1/embeddings` route, and fail a successful HTTP response that lacks a finite numeric vector. | Compose smoke and `tests/test_api.py`. |
| FR-10 | Run an opt-in, live exact-substring chat evaluation on a private fixture without storing prompts or responses in the report. | `contextyield evaluate --confirm-live-requests` and `tests/test_calibration.py`. This score is a limited proxy, not a certified quality floor. |
| FR-11 | Inventory likely AI integration points without executing repository code or claiming runtime measurements. | `tests/test_repo_scan.py` and `contextyield scan-repo`. |
| FR-12 | Ingest tenant-scoped metadata-only task events, reject conflicting event IDs, and summarize provider usage with explicit coverage. | `tests/test_trajectory.py`, `tests/test_api.py`, and Compose smoke against PostgreSQL. |
| FR-13 | Suggest a next action using hard-eligible operator forecasts, without claiming to execute or reserve task budget. | `tests/test_action_policy.py`, `tests/test_api.py`, and Compose smoke. |
| FR-14 | Expose bounded registered-tool discovery and an optional read-only local MCP facade. | `tests/test_api.py`, `tests/test_mcp_adapter.py`, and Compose smoke for search. |

## Non-functional requirements and limits

| ID | Requirement | v0.2 mechanism and limit |
| --- | --- | --- |
| NFR-01 | Bounded calls and responses | Requests over 64 KiB are rejected after Starlette buffers the body; this is not an ingress memory bound. Field limits, a 1 MiB upstream response cap, route timeout ≤120 s, and eight concurrent calls per ordinary target per process apply. An exclusive metered route has one slot plus a process-local meter lock. These are not global tenant quotas or global meter leases. |
| NFR-02 | Fail closed before dispatch | PostgreSQL decision write must succeed; database outage returns 503. Pool wait is bounded to roughly three seconds. A completed upstream call with a failed final ledger write remains ambiguous. |
| NFR-03 | Fault isolation | Three failures open an in-process target circuit for 30 s. No automatic retry after ambiguous errors. Circuit state is not shared across replicas. |
| NFR-04 | Operational visibility | Liveness/readiness, authenticated Prometheus request and latency metrics, metadata-only JSON event logs, receipt IDs and typed errors. No built-in tracing, alerting, or dashboards. |
| NFR-05 | Reproducible build/test | Universal `uv.lock`, frozen CI and image install, unit tests, linter, and CPU-only Compose smoke. Pin base/action image digests for a hardened release. |
| NFR-06 | Privacy/security | No raw request or output in receipts; fixed operator destinations; production mode requires HTTPS route URLs and non-demo keys. Ingress TLS, egress policy, key rotation, DB TLS, and retention still belong to deployment. |
| NFR-07 | Scalability | Stateless API can have multiple replicas sharing PostgreSQL, and idempotency is database-scoped. Local semaphores/circuits mean aggregate capacity and fairness are not coordinated; size upstream capacity or add shared leases before scale-out. |

## Launch gates before publishing production claims

1. Choose a real workload and quality evaluator, and demonstrate a quality floor on held-out examples.
2. Validate physical meter availability, exclusivity, idle/background contribution, and repeatability on the actual hardware and model version. Separate aggregate GPU-device joules from whole-system energy.
3. Run load, process-kill, database-outage, upstream-failure, and multi-replica tests at intended scale; define SLOs and alert thresholds.
4. Review tenant access, route/tool permissions, privacy retention, secret rotation, ingress/egress policy, backups, and restore drills.
5. Publish only evidence-class-qualified comparisons; synthetic replay is a functionality test, not an energy-saving result.

These gates are intentionally not described as passing merely because the CPU-only demo passes.
