# Operations and deployment notes

The checked-in Compose stack is a loopback-only learning environment with public demo credentials. It is not a production manifest.

## Local runbook

```sh
docker compose up --build -d
docker compose ps
docker compose --profile smoke run --rm smoke
docker compose logs --no-color router
docker compose down
```

`docker compose down` removes the project's containers and network, **not** its named PostgreSQL volume. `docker compose down -v` would delete the demo database and is not part of the normal runbook. The router is on host port `18080` to avoid a common local `8080` collision.

If `/health/ready` is 503, inspect PostgreSQL/migration logs. The API refuses new dispatches until it can commit a decision. `contextyield reconcile --older-than-s 300` marks old pending receipts unknown; it never resends an upstream call. Do not run it with an age shorter than the maximum configured upstream timeout plus a safety margin.

If requests return 502, inspect the receipt's attempt error code and target ID. A timeout or connection loss may have reached the backend, so do not retry without the same idempotency key and a conscious billing/load decision. Three failures open a circuit locally for 30 seconds; it resets when the process restarts. If the chosen target's eight local slots are busy, clients receive 429 with `Retry-After: 1`.

## Production checklist (operator-owned)

1. Use managed PostgreSQL with TLS, a least-privilege DB user, durable settings, backup/restore drills, and retention/deletion policy. Run reviewed migrations before serving traffic.
2. Supply `CY_MODE=production`, `CY_DATABASE_URL`, `CY_API_KEYS_JSON`, `CY_HASH_KEY`, and `CY_REGISTRY` through a secret/config manager. `CY_HASH_KEY` must stay stable across replicas; changing it changes idempotency comparisons for stored requests. Set backend keys via `key_env`. Never commit credentials. Rotate tenant and backend keys with a tested procedure.
3. Put the service behind an HTTPS ingress with authentication/RBAC as required, body/rate limits, and private upstream egress. The app's static keys are tenant identifiers, not enterprise SSO. Use read-only credentials on tool backends; `read_only=true` in JSON is only an attestation.
4. Keep one worker per container. Size the replica count with the explicit knowledge that eight slots and circuit state are **per replica**. For global quotas/fairness, implement shared admission/leases first.
5. Scrape authenticated `/metrics` without exporting API keys in labels/logs. The app emits metadata-only JSON events keyed by receipt ID; keep prompt and tool payload capture disabled at ingress too. Alert on readiness 503, ledger failures, 429/502 rates, circuit-open counts (add a gauge before relying on this alert), and rising unknown-energy share. Add distributed tracing separately; it is not built in.
6. Validate a route's quality evaluator, profile ID/expiry, hardware fingerprint, counter support, and model version before enabling profiled evidence. Never use a local gateway GPU reading to claim remote-provider energy.
7. Test crash-after-commit, backend timeout, DB outage/recovery, multi-replica idempotency, load shedding, and restore on the exact deployment topology. Define latency and availability SLOs with business owners.

Do not treat passing the local smoke test as evidence that the production checklist is complete.
