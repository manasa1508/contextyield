# Test and evaluate ContextYield

You do **not** need to install a local model for the first test. Docker Compose starts a CPU-only gateway, PostgreSQL, and deterministic mock chat, embedding, and read-only tool backends. That test proves the API, routing rules, receipts, and failure handling. It does **not** measure model quality or physical energy.

Run the commands below from the project root. Docker Desktop, or Podman with a Compose-compatible command, is required. Replace `docker compose` with your Podman Compose command if needed. On Windows, use PowerShell for the Compose commands; use `curl.exe`, not PowerShell's `curl` alias, for the optional HTTP examples.

## Run the no-model acceptance test

1. Start the stack and check its health:

   ```sh
   docker compose up --build -d
   docker compose ps
   ```

   Expect `db`, `mock`, and `router` to be healthy. Open `http://127.0.0.1:18080/docs` for the live API schema. `http://127.0.0.1:18080/health/ready` should return `{"status":"ready"}`.

2. Run the black-box check:

   ```sh
   docker compose --profile smoke run --build --rm smoke
   ```

   Expect `PASS: chat, embedding, tool, receipt redaction, idempotency, and hard rejection`. The check sends real HTTP requests through the gateway and database. It verifies that repeating an idempotency key does not redispatch a request. If it fails, inspect `docker compose logs --no-color router mock db`.

3. Inspect a decision yourself. The example below is for macOS or Linux shells; use the API form at `/docs` on Windows if you prefer not to escape JSON in PowerShell.

   ```sh
   curl -sS -i http://127.0.0.1:18080/v1/execute \
     -H 'Authorization: Bearer dev-local-key' \
     -H 'Content-Type: application/json' \
     -d '{"kind":"inference","capability":"short_answer","messages":[{"role":"user","content":"Explain a heat pump."}],"constraints":{"min_quality":0.95,"evidence_requirement":"allow_synthetic_demo"}}'
   ```

   Expect HTTP `200`, `target_id: "mock-robust"`, a `receipt_id`, and `forecast.evidence: "synthetic_demo"`. Change `min_quality` to `0.99`; expect HTTP `422` and `error: "no_eligible_route"`. With `min_quality: 0.8`, the lower declared energy bound selects `mock-efficient`. These values are synthetic registry declarations, not observed quality or joules. Fetch a returned receipt with `GET /v1/receipts/{receipt_id}` and the same Bearer key. The receipt has the candidate reasons but not the raw prompt or output. Authenticated `GET /metrics` exposes request counters and an upstream-latency histogram.

4. Check the embedding path with the same API. Send `kind: "embedding"`, `capability: "retrieval.embed"`, and `text: "heat pump"`; do not send `messages`. The demo route returns `target_id: "mock-embedding"` and `output.data[0].embedding: [9.0, 2.0, 1.0]`. This vector is deterministic mock data, not a useful semantic embedding. A malformed chat or embedding upstream response must fail with `502` and `invalid_upstream_response`; the unit tests exercise that boundary.

5. Run code checks and the synthetic policy comparison if `uv` is installed:

   ```sh
   uv sync --extra dev
   uv run pytest -q
   uv run ruff check .
   uv run contextyield compare --fixture fixtures/synthetic-v1.json --out artifacts/compare-v1
   ```

   Read `artifacts/compare-v1/report.md` and `comparison.json`. The three policies use the same synthetic full-information fixture. The manifest hashes the fixture and route registry. Do not report the synthetic-joule difference as measured savings or a production counterfactual.

6. Exercise the live quality-evaluation command against the echo mock:

   ```sh
   docker compose exec -T router contextyield evaluate --target mock-efficient \
     --fixture fixtures/quality-demo.json --out - --confirm-live-requests
   ```

   Expect a JSON report with `sample_count: 10`, `quality: 1.0`, and `scorer: "exact_substring_casefold_v1"`. The ten cases merely ask an echo server to repeat their text. A passing score does not validate an AI model. To save the report on your machine, redirect stdout to a private file; keep fixture identifiers free of personal data. The explicit flag prevents accidental paid requests when you later target a hosted API.

7. Run the fixed two-step caller in `examples/agent_flow.py`. On macOS or Linux, run `CY_GATEWAY_KEY=dev-local-key uv run python examples/agent_flow.py`. On Windows PowerShell, set `$env:CY_GATEWAY_KEY = 'dev-local-key'` and then run `uv run python examples/agent_flow.py`. Expect a tool receipt, a chat receipt, and an answer from the echo mock. The script shows where an agent framework would call the gateway and keep its own workflow state; it is not an autonomous agent.

## Connect a real model only when you need one

The gateway is **not** a model server. A local model is optional; a hosted API is also valid. The first-release wire contracts are non-streaming OpenAI-compatible `/v1/chat/completions` for `kind: "inference"` and `/v1/embeddings` for `kind: "embedding"`. The response must contain chat `choices[0].message.content` as text or one nonempty finite numeric vector at embedding `data[0].embedding`. A successful HTTP status alone is insufficient. Streaming, images, audio, model-emitted tool execution, and arbitrary native provider request formats are not supported.

For a local server, copy `config/routes.local.example.json` within `config/` and set each route's fixed `url` and `version` to the server's endpoint and model ID. Remove the embedding route if your server only hosts chat, or the chat route if it only hosts embeddings. Confirm that the provider's model ID really identifies the version you evaluated; a movable alias does not. The sample uses `host.docker.internal`, which works in common Docker Desktop setups but is not universal on Linux or Podman. The server must be reachable **from the router container**. Keep local HTTP and the demo gateway key on a private development machine. Leave `quality: 0.0` and `evidence: "unknown"` until you evaluate that model; then send `min_quality: 0` and `evidence_requirement: "allow_unknown"` for a connectivity test. A finite `max_energy_j` cannot pass with unknown energy. The registry is loaded at startup, so rebuild and restart after changing a copied route file.

To switch the loopback-only demo router to the local registry, use the backend-test Compose overlay. Set `CY_REGISTRY` to your copied path inside the image if you renamed the file:

```sh
docker compose -f compose.yaml -f compose.backend-test.yaml up --build -d
```

Call `http://127.0.0.1:18080/v1/execute`. Use the same chat request shape as above, but set the constraints to `{"min_quality":0,"evidence_requirement":"allow_unknown"}`. For an embedding route, use `{"kind":"embedding","capability":"retrieval.embed","text":"heat pump"}`. Expect the registered route ID, a valid response shape, and `forecast.evidence: "unknown"`; a connection or contract error returns `502`. To restore the mocks, run `docker compose up --build -d` without the overlay. Do not use this demo router for production traffic.

For an external OpenAI-compatible API, start from `config/routes.production.example.json`. Use a fixed HTTPS endpoint, pin the provider model ID, and set `key_env` to the name of an environment variable supplied to the router. Do not put the token in the registry or the repository. Set `CY_REGISTRY` to the copied config path and supply `CY_UPSTREAM_KEY` from a local secret source before using the backend-test overlay. The Compose file hardcodes demo mode and demo keys, so an external route under Compose is a **private connectivity test**, not a production deployment. [Gemini's compatibility endpoint](https://ai.google.dev/gemini-api/docs/openai) uses `https://generativelanguage.googleapis.com/v1beta/openai/chat/completions` with Bearer authentication; put that URL in `url` and a supported model ID in `version` for a direct closed-model test. [vLLM](https://docs.vllm.ai/en/latest/serving/online_serving/openai_compatible_server/) is an open-model server option. Both require a contract check for the chosen model. If a provider has a native-only API, place a managed [LiteLLM proxy](https://docs.litellm.ai/docs/simple_proxy) between it and ContextYield, register the proxy's specific chat or embedding endpoint, and test the normalized response contract. LiteLLM expands provider coverage; it does not make every model, modality, authentication method, or provider feature zero-configuration compatible. Keep provider secrets at the proxy and a scoped proxy credential at ContextYield.

## Evaluate a route before promoting its claims

Use the same workload and acceptance criteria for every candidate route. Keep training or prompt-tuning examples separate from the held-out evaluation set. Record the dataset digest, evaluator version, prompt template, model version, region, hardware, concurrency, and date with each result.

1. **Contract and policy:** Run `pytest` and the Compose smoke test. Check valid and invalid chat and embedding shapes, authentication, receipt redaction, idempotency, and `422` hard-constraint rejection. Confirm that the selected route meets the configured quality floor and region requirement. A configured quality score is a benchmark claim, not a score computed for each request.
2. **Task quality:** Run `contextyield evaluate --target <id> --fixture <private.json> --out <report.json> --confirm-live-requests` for a first chat check. The fixture uses `schema_version: 1` and 10 to 1000 `samples`, each with an `id`, text `messages`, and `expected_substring`; `fixtures/quality-demo.json` shows the shape. The command records every attempted case. HTTP, transport, timeout, and invalid-response failures count as incorrect, appear by error code, and prevent suggested route fields. The reported p95 covers all attempts. For a real quality gate, score a representative held-out task set with a documented rubric and human review of disputed cases. For embeddings, measure retrieval Recall@k or nDCG on labeled query-document pairs; also check vector dimension stability. Report failures and confidence intervals, not only an average. The exact-substring score is a narrow proxy, not a general answer-quality evaluator. Promote a positive `quality` value only with a traceable `quality_eval_id`.
3. **Latency and reliability:** Run warm and cold trials at expected request sizes and concurrency. Report p50, p95, p99, throughput, `429` saturation, timeout, and `502` rates. Compare observed p95 with the declared `p95_ms`; the registry does not update itself. Test database outage before dispatch, an ambiguous backend timeout, three failures that open the process-local circuit, and recovery. The [operations runbook](operations.md) records the expected semantics. Test multi-replica behavior separately before relying on global capacity limits.
4. **Energy:** Keep `evidence: "unknown"` for remote APIs unless the provider supplies trustworthy, comparable backend-side evidence. For a co-located NVIDIA chat route, follow the [measurement guide](measurement.md) and optional `contextyield profile` workflow on an operator-isolated GPU. Repeat and randomize runs, measure idle/background load, validate on held-out tasks, and report the counter's **gross GPU-device-window joules** with its uncertainty. The command's empirical min/max is not a guaranteed forecast interval, and a gateway-side GPU counter does not measure a remote model. Compare policies only at the same energy boundary and quality/latency floor.
5. **Agent task outcome:** Connect an agent orchestrator as a caller of `POST /v1/execute`, not as an assumed built-in agent. Evaluate complete tasks: success rate, unsafe or invalid tool use, steps per task, latency, cost, unknown-energy share, and receipt coverage. Use one `receipt_id` per model, embedding, or explicit read-only tool call and correlate those IDs in the agent's own trace. ContextYield does not run the agent loop, execute model-generated tool calls, enforce workflow-wide energy budgets, or export OpenTelemetry traces today.

The useful production pattern is an agent or application → ContextYield policy gateway → registered model, embedding, or read-only tool route. The agent owns task state and tool approval; ContextYield owns per-call eligibility, dispatch, and the decision receipt. An MLOps pipeline owns datasets, evaluations, profile validation, model-version promotion, and rollback. A future workflow-level budget and drift gate could use correlated receipts to abstain when the remaining evidence is weak; neither is implemented now. This split makes the gateway reusable across projects without claiming universal model support or automatic energy optimization.

Promote a route only after its held-out quality floor, observed latency target, contract checks, and reliability target pass. Publish an energy benefit only after a controlled physical comparison at the same measurement boundary. Unknown energy is a valid operating state, not a result to fill with zero.

When finished with the demo, run `docker compose down`. This keeps the named PostgreSQL volume. Do not use `docker compose down -v` unless you intend to delete the demo database.
