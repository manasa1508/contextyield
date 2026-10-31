# ContextYield

ContextYield is an evidence-aware gateway for choosing among registered chat, embedding, and semantically equivalent **read-only** tool backends. It ranks candidates under hard quality, latency, region, and energy-evidence constraints; executes one route; and records a durable, tenant-scoped decision receipt. A reproducible fixture comparison makes policy behavior inspectable without claiming that synthetic joules are physical measurements.

**Status:** local reference implementation, v0.2.0. The included profiles and mock backends are synthetic demonstrations. Do not interpret their quality scores, energy values, or replay report as real-world savings. This is not yet an organization-ready production service.

The confirmed design direction is **local-first and cloud-portable**, with **local models plus OpenAI-compatible APIs** as inference sources. The policy first enforces a caller's quality floor, then minimizes the available upper-bound energy forecast among eligible routes. These are design choices, not a claim that unbenchmarked models have verified quality or energy. See the [decision record](docs/decisions.md).

## Why this exists

Energy-aware routing is already an active research area. ContextYield's focus is the *evidence contract*: an operator can see why a target was eligible, which policy/configuration made the choice, whether the forecast came from a profile or a fixture, and whether any device-window energy was actually observed. Unknown energy stays unknown. A route abstains instead of relaxing a hard constraint.

The system is intentionally one stateless HTTP service plus PostgreSQL. It uses registered destinations, not caller-supplied URLs or arbitrary tool execution. It does not host models, infer ground-truth quality for individual prompts, stream tokens, or estimate facility-wide energy/carbon.

The companion task layer examines repositories for likely AI integration points and accepts metadata-only task events from any host that can call HTTP or write JSONL. It can summarize whole-task usage, compare paired traces, suggest a next action under declared constraints, and expose a small read-only local MCP surface. A source scan cannot measure runtime tokens, quality, cost, or energy; the host must instrument those boundaries. The original route policy and energy-evidence contract are unchanged. See the [integration guide](docs/repo-and-task-integration.md).

## Quick start: Docker Compose

Requires Docker Desktop or Podman with a Compose-compatible CLI. The demo is CPU-only and needs no external API keys or GPU.

```sh
docker compose up --build -d
docker compose --profile smoke run --rm smoke
```

The router is exposed only on `127.0.0.1:18080`; the development API key is `dev-local-key`. Demo database credentials and key are intentionally insecure. Never expose this Compose file as a production deployment.

Example inference request:

```sh
curl -sS http://127.0.0.1:18080/v1/execute \
  -H 'Authorization: Bearer dev-local-key' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: example-1' \
  -d '{"kind":"inference","capability":"short_answer","messages":[{"role":"user","content":"Explain a heat pump in one sentence."}],"constraints":{"min_quality":0.8,"evidence_requirement":"allow_synthetic_demo"}}'
```

Example read-only tool request:

```sh
curl -sS http://127.0.0.1:18080/v1/execute \
  -H 'Authorization: Bearer dev-local-key' \
  -H 'Content-Type: application/json' \
  -d '{"kind":"tool","capability":"catalog.lookup","input":{"sku":"A-100"},"constraints":{"min_quality":0.95,"evidence_requirement":"allow_synthetic_demo"}}'
```

Example embedding request:

```sh
curl -sS http://127.0.0.1:18080/v1/execute \
  -H 'Authorization: Bearer dev-local-key' \
  -H 'Content-Type: application/json' \
  -d '{"kind":"embedding","capability":"retrieval.embed","text":"heat pump","constraints":{"min_quality":0.8,"evidence_requirement":"allow_synthetic_demo"}}'
```

The response includes `receipt_id`; inspect it with `GET /v1/receipts/{receipt_id}` using the same key. Authenticated `GET /v1/capabilities` lists the enabled request kinds and capabilities without exposing upstream URLs or tokens. `/docs` has the generated API schema. `GET /health/live` checks process liveness; `/health/ready` checks PostgreSQL; `/metrics` requires an API key. Repeating an `Idempotency-Key` returns `409` with the original receipt ID and **does not redispatch**. Different content with the same key returns `422`.

Successful model calls also return `token_usage`, which is recorded in the receipt outcome. Valid OpenAI-style counters are labeled `provider_reported`; absent or incompatible usage is `unavailable`, not zero. `max_tokens` is an output limit, not spent tokens. This metadata does not reveal provider energy use.

## Local developer checks

```sh
uv sync --extra dev --extra mcp
uv run pytest -q
uv run ruff check .
uv run contextyield compare --fixture fixtures/synthetic-v1.json --out artifacts/compare-v1
uv run contextyield scan-repo --path . --out -
uv run contextyield analyze-trace --trace fixtures/trajectory-demo.jsonl --out -
```

The CLI report is a deterministic synthetic full-information comparison, not a benchmark of measured energy. Its JSON manifest pins the fixture and registry hashes; rerun it to verify identical output.

You can also run a live **mock** quality evaluation without a local model:

```sh
docker compose exec -T router contextyield evaluate --target mock-efficient \
  --fixture fixtures/quality-demo.json --out - --confirm-live-requests
```

The command sends ten requests to the selected backend. The demo fixture tests an echo server, so its score is not evidence that a model is useful. For your own held-out cases, replace the fixture and target. The report stores sample IDs, correctness, failure codes, latency, model version, and a fixture hash, but not prompts or responses. Failed cases count as incorrect and block suggested route fields. `evaluate` does **not** measure energy.

Run `CY_GATEWAY_KEY=dev-local-key uv run python examples/agent_flow.py` to see a two-step application call the read-only catalog tool and then the chat route. It prints both receipt IDs and keeps workflow state on the caller side. This is a fixed integration example, not an autonomous agent. On Windows PowerShell, set `$env:CY_GATEWAY_KEY = 'dev-local-key'` before the Python command.

For an isolated NVIDIA backend, install the optional meter with `uv sync --extra nvml --extra dev`, then use `contextyield profile --target <registered-id> --fixture <private-benchmark.json> --out artifacts/profile.json`. It collects at least ten exclusive device-window observations and an exact-substring benchmark score. This requires a co-located meter and an operator-attested exclusive GPU. The emitted min/max is an empirical gross GPU-device range—not a guaranteed bound or a facility measurement. Validate on held-out runs before using it as a route profile; see the measurement guide.

## Real backends and energy evidence

For a separately running local OpenAI-compatible model server, start from `config/routes.local.example.json` and point `CY_REGISTRY` to your copy. Its two examples need a chat model at `/v1/chat/completions` and an embedding model at `/v1/embeddings`; remove the route you cannot serve. Start the gateway with `docker compose -f compose.yaml -f compose.backend-test.yaml up --build -d`. The example uses `host.docker.internal` for a model server on a Docker host; that address and the overlay's `host-gateway` alias need adjustment on some Podman setups. Local plaintext HTTP is accepted only in `CY_MODE=demo`, which uses insecure demo defaults and must remain on a trusted development machine. A production local model needs HTTPS through an internal ingress/TLS proxy. The model server is **not** bundled or started by this Compose stack, and the real backend path has not been end-to-end validated in this release.

For an externally hosted OpenAI-compatible API, copy `config/routes.production.example.json`, register your own **fixed** HTTPS endpoint, pin its model version, and set `CY_REGISTRY` to that file. Supply the upstream bearer token from the environment name in `key_env` (never in JSON or Git). For a Basic-auth *read-only tool endpoint*, `key_env` contains `username:password` and `auth_type` is `basic`. To test a remote endpoint on the loopback-only demo stack, export `CY_REGISTRY` and `CY_UPSTREAM_KEY`, then use the backend-test Compose overlay. This can incur provider charges and sends the fixture prompts to that provider. Do not use the demo key or Compose stack as a production service. For a real deployment, `CY_API_KEYS_JSON` maps tenant IDs to gateway keys; set non-demo keys and `CY_MODE=production`. Also set a stable, private `CY_HASH_KEY` (at least 32 characters) for HMAC-based idempotency hashes and `CY_DATABASE_URL` for PostgreSQL. Production mode rejects synthetic profiles and HTTP destinations.

The current model wire contract is **non-streaming OpenAI-compatible chat or single-text embeddings**. Open-weight and closed models work when their server exposes the matching endpoint. For a native-only provider, use an operator-run translation gateway and register its fixed OpenAI-compatible endpoint; verify the translation with a contract test before assigning quality scores. This is not universal support for images, audio, Responses API, tool calling, or arbitrary vendor JSON. Route changes are configuration-driven but require a gateway restart; there is no live registry administration API.

Until a model passes an evaluation, leave `quality: 0.0`; a request with a positive `min_quality` will correctly reject that route. In production, a positive quality value also requires a `quality_eval_id`. If no credible energy profile exists, use `evidence: "unknown"` and null energy bounds. The router may still route when the caller allows unknown energy, but it cannot minimize unknown joules or honor a finite energy cap for that route. An optional `nvml:<index>` meter on a **co-located** NVIDIA GPU can record gross GPU device-window joules; registration requires `meter_backend_co_located: true`. It is not total system/facility energy. Request attribution is false unless the operator explicitly attests exclusive device use; in that mode the gateway also enforces one local in-flight call per meter. See [measurement methodology](docs/measurement.md) before using physical numbers.

## Architecture and constraints

```text
client -> auth/schema -> pure route policy -> PostgreSQL decision receipt (commit)
       -> bounded upstream call + optional device counter window
       -> PostgreSQL outcome/attempt (commit) -> response
```

The pre-dispatch commit is intentional. If the ledger is unavailable, no upstream call is made. A crash or ambiguous timeout is not silently retried; `contextyield reconcile` marks old pending receipts `unknown`. The circuit breaker and per-target concurrency slots are local to each process. Run one worker per replica, with an ingress for TLS, distributed rate limiting, and balancing. Do not claim cross-replica global capacity enforcement from this version.

The receipt excludes raw prompts, tool inputs, outputs, and upstream secrets. A keyed HMAC of canonical request content is stored solely for idempotency; keep its key separate from the database. Operational logs and traces should likewise exclude content. Tool routes must be read-only and contract-equivalent; ContextYield does not execute model-emitted tool calls automatically.

Read the [testing and evaluation guide](docs/how-to-test-and-evaluate.md), [repo/task integration guide](docs/repo-and-task-integration.md), [agent-efficiency research agenda](docs/agent-efficiency-agenda.md), [decisions](docs/decisions.md), [requirements](docs/requirements.md), [architecture](docs/architecture.md), [system-design patterns](docs/patterns.md), [measurement methodology](docs/measurement.md), [threat model](docs/threat-model.md), [operations runbook](docs/operations.md), and [research notes](docs/research-notes.md) for the contract, learning path, deployment guidance, and honest evaluation limits.

## Roadmap and contribution boundary

Near-term extensions: hardware-profile import and drift checks, multiple tenant keys/rotation, OIDC at an ingress, managed PostgreSQL backup/retention, multi-replica capacity coordination, richer quality evaluations, and controlled measured-policy trials. These are **not** claimed as present. Please run the test suite and Compose smoke test before proposing changes that affect evidence labels or failure semantics.

MIT-licensed. Contributions welcome.
