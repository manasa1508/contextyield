# ContextYield architecture

Status: **implemented v0.2 reference plus explicit future work**, not proof of production readiness. This page describes the code currently in the repository. Energy claims and fixture limits are in [measurement.md](measurement.md); security boundaries are in [threat-model.md](threat-model.md). The additive repo/task layer is detailed in [repo-and-task-integration.md](repo-and-task-integration.md).

## Caller contract and scope

The Docker Compose demo runs a FastAPI gateway, PostgreSQL, migrations, and deterministic mock chat, embedding, and tool upstreams on a CPU-only machine. A client sends a synchronous `POST /v1/execute` request of kind `inference` (messages), `embedding` (one text), or `tool` (string-valued input map) with a registered capability. The gateway chooses **one** configured target, makes **one** upstream HTTP call, and returns its output or a typed error with a receipt ID. It does not host models, stream tokens, orchestrate model-emitted tool calls, or accept a caller-supplied upstream URL. The mock route profiles and fixture joules are synthetic.

Implemented supporting surfaces: `GET /v1/receipts/{id}` (tenant-scoped), authenticated `GET /v1/capabilities`, `/health/live`, `/health/ready` (checks PostgreSQL), authenticated `/metrics` with Prometheus counters/histogram, metadata-only JSON event logs, and `contextyield compare`, `contextyield evaluate`, `contextyield profile`, and `contextyield reconcile` CLI commands. Completed model calls record available non-negative integer OpenAI-style token counters as `provider_reported` in the response and receipt outcome; missing/incompatible counters remain `unavailable`. These are not independently measured energy or billed cost. `evaluate` sends live chat requests and records a limited exact-substring quality score and latency, without energy measurement. The physical profiling command requires optional NVML, exclusive local GPU use, and at least ten benchmark samples; it emits an empirical device-window profile but does not establish statistical coverage or transferability. The public request/response schema is generated from the FastAPI models; examples are in the [README](../README.md).

| Requirement | Implemented boundary | Not yet implemented |
| --- | --- | --- |
| Route selection | Pure `choose()` filters enabled kind/capability, requested region, static quality and p95 caps, evidence requirement, profile expiry, and energy upper bound; then ranks eligible targets by known upper joules, p95, priority, ID. Unknown energy can be allowed but ranks after known energy. | Learned quality, live load-aware scoring, queue-aware optimization. |
| Execution | Operator-owned JSON route registry, single synchronous call through `httpx`; chat and embeddings use the matching OpenAI-compatible payload and response contract. Tools use a registered read-only-capability payload. A malformed successful model response fails the attempt. | Streaming, multimodal APIs, provider-native protocols, multi-step orchestration, cross-target retry/fallback after failure, live registry reload. |
| Capacity/failure isolation | Eight non-blocking semaphore slots **per ordinary target per process**; one slot and a shared local meter lock for an exclusive metered target. Three failures open a target's in-process circuit for 30 seconds. Open targets are excluded before choice. | Fair queue, global/distributed capacity or meter leases, cross-process circuit state, tenant rate limit. |
| Receipt | PostgreSQL receipt row with decision JSON committed before dispatch; terminal status/outcome update and at most one attempt row are committed together. | Append-only event stream, tamper-evident log, cached result replay. |
| Comparison | Full-information synthetic fixture compares energy, fastest, and priority choices using stored outcomes; hashes fixture and registry in a manifest. Private exact-substring chat evaluation can produce a preliminary score and p95 from live responses. | Virtual clock, arrival/queue simulation, measured energy comparison, embedding/retrieval evaluation, human or task-level grading, online A/B or counterfactual evaluation. |

The static route registry is operator-owned JSON and loaded at startup; tenant API keys come from a JSON environment setting. Keys identify tenants for receipts, but there are **no tenant-specific route allowlists**. Tool `read_only=true` is a registry attestation, not proof of backend permission. Production mode requires non-demo gateway keys and HTTPS route URLs, rejects synthetic evidence labels, and requires evaluation metadata for positive quality claims and hardware/expiry metadata for profiled energy. The loader rejects URL userinfo in every mode. These are declaration checks, not independent provenance verification or a complete security posture. External model backends can be registered without changing router code if they expose supported OpenAI-compatible chat or embedding endpoints. Native-only APIs need a separately operated translation gateway; the router does not dynamically synthesize vendor adapters.

## C4-like views of the implemented system

### Context and containers

```text
client ── Bearer key + /v1/execute ──> FastAPI gateway ── fixed URL ──> model or tool upstream
client <── output/error + receipt ID ──── gateway
                                         │
                                         └── committed receipt/attempt ──> PostgreSQL

operator ── static route JSON + environment keys/secrets ──> gateway at startup
CLI ── full-information JSON fixture + same choose() ──> comparison.json / report.md
optional co-located NVML counter ── gross device-window observation ──> attempt
```

The gateway is one deployable service. Compose also contains migration, mock, and smoke services. The serving engine and read-only tool implementation remain external. The optional NVML probe is local to the gateway host/container and is unavailable without supported NVIDIA hardware and access; it is not part of the CPU demo's physical measurement.

### Components and request flow

1. FastAPI validates request shape/size; a Bearer key maps to a tenant. A key grants that tenant access to its own receipts. It does not restrict which registry targets that tenant may request.
2. The gateway removes targets whose **local circuit** is open and calls pure `choose()`. The decision records candidate exclusion reasons, selected target, constraint snapshot, policy name, registry digest, and circuit-open IDs. Configured quality, p95, and energy bounds are inputs/forecasts, not observations from this request.
3. PostgreSQL inserts a `pending` receipt with an application-immutable decision JSON before dispatch. The request hash supports idempotency checks; raw messages, tool input, output, and upstream secret are not stored in the receipt. After dispatch the outcome may contain a small provider-reported token counter subset, never the raw upstream usage object. The database schema is one `receipts` row plus zero or one `attempts` row for this one-call path, **not** an append-only event log.
4. If a route was selected, the gateway tries its per-target semaphore **without waiting**. A full target returns 429; the gateway does not reconsider another eligible target. Otherwise it posts once to the registered URL with a route timeout, optionally samples a local NVML counter around the call, and records success/failure in the local circuit.
5. The store inserts the attempt and updates receipt status/outcome in one transaction, then the gateway responds. Rejected requests have a terminal receipt but no upstream attempt. `GET /v1/receipts/{id}` returns only the calling tenant's receipt.

```text
contextyield/api.py      FastAPI contract, auth, slots, circuit, upstream call, Prometheus
contextyield/core.py     pure target eligibility and deterministic choose()
contextyield/config.py   JSON registry, keys, route/mode validation
contextyield/upstream.py OpenAI-compatible payload and response contracts, upstream auth
contextyield/store.py    PostgreSQL insert, terminal update, lookup, reconciliation
contextyield/energy.py   optional NVML cumulative device-counter window
contextyield/calibration.py private chat evaluation and optional local GPU profiling
contextyield/replay.py   full-information fixture comparison; no scheduler
contextyield/repo_scan.py bounded, read-only source integration inventory
contextyield/trajectory.py metadata-only task events and paired summaries
contextyield/action_policy.py pure advisory next-action eligibility/ranking
contextyield/mcp_adapter.py optional read-only local stdio MCP facade
contextyield/cli.py      compare, evaluate, profile, scan, trace, and reconcile commands
migrations/001_init.sql  receipts and attempts tables
migrations/002_task_events.sql tenant-scoped metadata-only task-event table
```

## Failure and delivery semantics actually provided

| Condition | Current behavior |
| --- | --- |
| No eligible target | Decision receipt is inserted, then marked `rejected`; API returns 422 with candidate reasons. |
| Selected target's local slots or exclusive meter lock are busy | Receipt is marked `rejected`; API returns 429 with `Retry-After`. There is no application-level admission queue or alternate-route retry. |
| Three upstream failures | That process excludes the target for 30 seconds; another process would have separate state. An upstream timeout/transport error is ambiguous and no automatic retry occurs. |
| PostgreSQL insert unavailable | API returns 503 and does **not** dispatch. `/health/ready` also fails when PostgreSQL is unavailable. |
| Outcome transaction unavailable after upstream call | API returns 503 with receipt ID; a pending receipt may remain despite a completed external call. The CLI can mark sufficiently old pending receipts `unknown`; it never redispatches them. |
| Process crash between decision commit and terminal update | Pending receipt survives, but whether dispatch occurred may be unknowable. Reconciliation changes `pending` to `unknown`, not to success or failure. No exactly-once guarantee. |
| Same tenant and idempotency key repeated | Same request hash returns 409 and original receipt ID; changed request hash returns 422. Neither response redispatches or returns a cached result. Without a key, another request is a new attempt. |
| Meter missing/unsupported/reset | Observation says unavailable; it does not become zero joules. Routing still uses registry profile evidence, if any, rather than a newly measured per-request value. |

PostgreSQL's durability depends on WAL/fsync and storage configuration; a committed receipt does not prove exactly-once backend execution [PostgreSQL WAL](https://www.postgresql.org/docs/current/wal-intro.html), [non-durable settings](https://www.postgresql.org/docs/current/non-durability.html). Decision JSON is not updated by application code after insertion, but the database does not provide a cryptographic or administrator-proof audit log.

## System-design lessons and production gap

The implemented patterns are a **functional policy core**, hard-constraint filtering before ranking, deterministic tie-breaking, **pre-dispatch transactional receipt**, idempotency conflict detection, per-target bulkheads, and a local circuit breaker. They are intentionally smaller than a distributed scheduler. The configured quality/p95/profile values must be validated operationally; the gateway does not infer correctness or continuously recalibrate them.

For deployment beyond the local reference, add and verify ingress TLS/auth integration, network egress controls, distributed rate limiting and capacity coordination, secret rotation, per-tenant route authorization if needed, PostgreSQL backup/restore and retention, load/failure tests, and monitoring/alerts. The current metrics are Prometheus only; **OpenTelemetry tracing is not implemented**. A second gateway process multiplies the eight-slot limit and resets circuit state, so horizontal scaling needs explicit coordination. A fair queue or virtual-clock replay would be a future design, not a property of this version. ContextYield should continue to integrate with a serving engine rather than rebuild one; [vLLM documents an OpenAI-compatible server](https://docs.vllm.ai/en/latest/serving/online_serving/openai_compatible_server/) and [LiteLLM documents a multi-provider OpenAI-compatible gateway](https://docs.litellm.ai/docs/simple_proxy).
