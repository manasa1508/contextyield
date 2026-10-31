# System-design patterns you can study in ContextYield

Each pattern below solves a concrete problem in this repository. Read the named code/tests, change one condition, and rerun the relevant test to see why the pattern exists.

| Pattern | Where | Why it is useful | Limit / exercise |
| --- | --- | --- | --- |
| **Functional core, imperative shell** | `contextyield/core.py` versus `api.py`, `store.py`, `energy.py` | Eligibility/ranking stays deterministic and testable without PostgreSQL or a model. | Add a new hard residency rule in `choose()` and prove no selected target violates it. |
| **Hard constraints before optimization** | `choose()` | Quality, region, latency, evidence, and energy caps are never traded for a lower score. | Set an impossible floor and inspect each candidate's rejection reasons. |
| **Conservative ranking under uncertainty** | `choose()` | Known upper energy bounds rank before unknown values; an unknown cannot satisfy a hard energy cap. | Expire a profile and compare `profiled` versus `allow_unknown`. Empirical maximum is not a statistical guarantee. |
| **Ports/adapters** | `api.py` calls registered HTTP targets; `energy.py` abstracts meter reads | Upstream and meter effects can be replaced by deterministic mocks in tests. | Swap one model route with a local vLLM endpoint without changing policy code. |
| **Write-ahead receipt / transactional boundary** | `store.py` | A decision is committed before external work; attempt and terminal state commit atomically afterward. | Stop PostgreSQL before dispatch: no upstream call occurs. This does not make backend effects exactly once. |
| **Idempotency key** | unique `(tenant, idempotency_key)` in `migrations/001_init.sql` | Concurrent/repeated client submissions cannot silently double-dispatch with the same key. | Repeat a key with changed content: 422. Response caching is deliberately absent. |
| **Bulkhead/load shedding** | `api.py` per-target `BoundedSemaphore(8)` | Caps in-process parallelism and returns 429 instead of unbounded waiting. | Load with >8 simultaneous slow calls; this is per process, not a distributed quota. |
| **Circuit breaker** | `api.py` `CircuitRegistry` | Repeated failures stop choosing a failing target for 30 seconds, preserving capacity for healthy routes on later requests. | `tests/test_api.py` shows the fourth call using another route. No immediate retry after a failed call. |
| **State machine with explicit ambiguity** | `receipts.status` and `contextyield reconcile` | Pending, completed, failed, rejected, and unknown prevent a crash from being mislabeled success or loss. | Kill a process after decision commit; reconcile only after the maximum call window. |
| **Content-addressed experiment inputs** | `replay.py` manifest | Fixture and registry hashes make a synthetic comparison rerunnable and sensitive to input changes. | Change one fixture value and see the manifest digest change. This is not a cryptographic audit log. |
| **Health/metrics separation** | `api.py` endpoints | Liveness proves the process exists; readiness checks the DB; metrics show outcomes and latency without prompt content. | Stop DB and confirm readiness/dispatch return 503, then restart and retry. |

Patterns not implemented: fair queuing, global rate limiting, distributed leases/circuit state, an outbox, event sourcing, service mesh, autoscaling policy, and online learning. Adding these by name without a measured requirement would make the small reference harder to operate. The [architecture](architecture.md) and [requirements](requirements.md) show when the next pattern is justified.
