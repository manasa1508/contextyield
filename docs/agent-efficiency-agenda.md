# Agent efficiency: product direction and research agenda

Status: mixed implementation and research agenda as of 2026-11-01.
ContextYield now includes provider-reported token counters, a bounded static
repository inventory, portable task-event ingest and summaries, a deterministic
advisory next-action policy, simple registered-tool search, an offline paired
trace comparison, and an optional local read-only MCP facade. See the
[integration guide](repo-and-task-integration.md) for exact boundaries. The
remaining features below are **not implemented or validated savings**.

## The problem to solve

Optimizing one model call misses the unit that users pay for: a **completed
task**. A task can contain model calls, decision-only calls, retrieval, tool
discovery, tool execution, retries, and human escalation. More efficient
tokens per call can even increase total use if agents take more steps. The
product hypothesis is that a quality-constrained **marginal-action governor**
can decide when to retrieve, search for a tool, call a small decision model,
generate with an LLM, reuse evidence, escalate, or stop. Its decisions and
counterfactual limits should be auditable.

`max_tokens` is an output ceiling, **not the number of tokens billed or
consumed**. A high ceiling is a risk for runaway outputs, not proof of waste.
The first instrumentation step records the provider's token counts when the
supported response includes non-negative integer `prompt_tokens`,
`completion_tokens`, or `total_tokens`. Missing/incompatible usage remains
`unavailable`; it never becomes zero or energy. These are **provider-reported**
counts, not independently metered compute or necessarily invoice totals.

## What exists today, and the gap we would target

- [RouteLLM](https://github.com/lm-sys/RouteLLM) already studies/implements
  cheaper-versus-stronger model selection. Model routing alone is not a novel
  claim for this project.
- [LiteLLM virtual keys](https://docs.litellm.ai/docs/proxy/virtual_keys)
  already cover budgets and spend tracking. A new proxy should not duplicate
  provider connectivity, key management, or billing infrastructure without a
  strong reason.
- Anthropic's [tool-search work](https://www.anthropic.com/research/advanced-tool-use)
  and [code execution with MCP](https://www.anthropic.com/research/code-execution-with-mcp)
  describe on-demand tool definitions and filtering tool results before they
  enter model context. MCP is **not dead**: the
  [2026-07-28 specification](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
  moved to a stateless core and extension model. The issue is indiscriminate
  tool/schema/result exposure, not the existence of the connector protocol.
- [TypeSafe's Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev)
  is a decision-only model for structured choices. It is an optional comparator,
  not an assumption that every decision model is calibrated for our tasks or
  that a decision call always saves resources.
- Recent [agentic-workload characterization](https://arxiv.org/abs/2608.15127)
  reports control-plane overhead, heterogeneous resources, and cross-request
  redundancy. [TwinRouterBench](https://arxiv.org/abs/2605.18859) explicitly
  tests routing at intermediate agent steps. These narrow the defensible gap:
  end-to-end, quality-constrained *action* selection with honest measurement.

Novelty remains a **hypothesis**, not a verified first-in-the-world claim.
Before any research submission, conduct a systematic literature and code
review and preregister baselines and evaluation rules.

## Seven possible product bets (not seven promises)

| Bet | Distinctive behavior | Cheapest falsification test |
| --- | --- | --- |
| 1. Task budget envelope | Allocate a budget across the whole agent trajectory rather than one call; count router and tool overhead too. | Replay annotated trajectories; compare task success, total provider tokens/cost, and p95 completion time. |
| 2. Marginal-action gate | Choose `answer`, `retrieve`, `search_tool`, `decision_model`, `strong_model`, `human`, or `abstain` from an allowlist. Hard quality/security constraints remain deterministic. | Small labeled next-action set with always-answer and always-escalate baselines. |
| 3. Progressive tool exposure | Present only relevant MCP tool families; summarize/tool-filter outputs with provenance and a drill-down handle. Skills guide workflows; MCP still transports tools/data. | Measure schema + result tokens and downstream task accuracy against full exposure. |
| 4. Evidence reuse with freshness | Cache only permission-scoped, versioned, read-only evidence; emit cache age, source, and invalidation reason. No unsafe cross-tenant reuse. | Duplicate-heavy workload plus stale-data adversarial cases. |
| 5. Decision-model plug-in | Optionally use Jev-like or open decision models for typed *advisory* scores; account for their latency/cost and calibrate on held-out data. | Compare against deterministic rules and cheap classifiers, including net overhead. |
| 6. Token/energy honesty ledger | Separate provider-reported tokens, estimated cost, measured local device energy, remote unknown energy, and synthetic fixtures; never convert one into another silently. | Contract tests for missing/partial usage and dashboard audit of evidence labels. |
| 7. Portfolio-level guardrails | Detect where per-call savings increase total daily usage, tool loops, retries, or low-quality completions (a rebound effect). | A/B trial with per-task and aggregate usage, not just per-call averages. |

The opposite-design experiment is important: **remove** the extra router/agent
step for simple tasks. A deterministic policy may beat an additional model
call once its overhead and mistakes are counted.

## Recommended architecture

```text
host app / agent framework / Claude client
  ├─ skills: task instructions and evaluation rubric (not credentials)
  ├─ MCP: optional, permission-scoped tool discovery and execution transport
  └─ task adapter: task ID, hard constraints, budget envelope, trace context
       ↓
policy control plane: deterministic eligibility → optional calibrated advice
       ↓                                  ↓
model gateway (existing)            read-only tool/MCP gateway
       ↓                                  ↓
provider or local server            source systems
       └──────── task receipt/trace collector ────────┘
```

The host owns the agent loop. The gateway owns authorization, allowed targets,
hard constraints, metering provenance, and receipts. An optional MCP adapter can
expose **a small stable set** of project operations to Claude/Cowork/other MCP
clients, while applications can call HTTP/SDK directly. Do not make the
optimization policy depend on one host, agent framework, or proprietary model.
No user credentials should be placed in skill files or tool descriptions.

`GET /v1/capabilities` is a simple listing; `GET /v1/tools/search` is only
name-based discovery. `POST /v1/execute` is still one synchronous step. The new
`POST /v1/actions/choose` is advisory, not a globally enforced task budget.
The MCP facade is local stdio and read-only, not a hosted OAuth service.
Tenant keys currently protect receipt visibility, not route-level RBAC. An org
deployment needs OIDC/workload identity at ingress, per-tenant route and tool
authorization, egress allowlists, secret rotation, and trace redaction before
connecting sensitive sources. Basic-auth-only backends can be wrapped behind
an internal credential broker so agents never see the Basic secret.

## Vertical slice and next build targets

1. Implemented: a versioned task-event contract, bounded per-task metadata
   summary, externally sourced quality outcome, and receipt/trace IDs. The
   event API does not verify the caller's external quality or energy claim.
2. Implemented: deterministic action eligibility under supplied quality and
   token/cost/time/energy budgets, plus an optional advisory tie-break input.
   The host owns execution and must enforce any broader task budget.
3. Implemented: simple tool-name search and a local read-only MCP facade.
   These do not filter tool **results** or provide a hosted enterprise gateway.
4. Next: a permission-scoped evidence cache with freshness tests, true
   decision-model adapters and calibration, framework-specific trace helpers,
   OpenTelemetry propagation, and deployment-grade RBAC/rate limits.

## Evaluation that could support a paper

The unit is one **task with an externally checked outcome**. Use a frozen,
held-out task suite containing easy/hard tasks, long-context tasks, duplicate
retrieval, stale evidence, many-tool catalogs, tool failures, and privacy-zone
constraints. Compare: always-strong; always-cheap; static deterministic policy;
model-only routing; full-tool exposure; progressive tool exposure; and each
proposed component ablated in turn. Include the cost and latency of *every*
router, judge, retrieval, and tool call. Report task success/quality-floor
violation, prompt/completion/cached tokens when available, monetary cost,
latency distribution, tool bytes/tokens, number of steps, abstentions, and
retries. For local hardware, separately report physically observed GPU-device
energy with boundary, concurrency, and uncertainty; for remote APIs, mark
energy unknown unless the provider supplies credible measurement. A
[SCI for AI](https://greensoftware.foundation/standards/sci-ai/) analysis can
be a separate extension if its required boundaries and data are available.

Predeclare the quality floor, statistical tests, and failure treatment. Randomize
paired task order where live providers are involved; pin model versions and
registries; bootstrap confidence intervals by task; measure drift by time and
tenant; publish negative results and raw anonymized metadata. Never infer
energy savings from token savings alone.

Promising research questions:

1. Under what conditions does an extra decision-only model reduce **total
   task** cost/energy without increasing quality-floor violations?
2. Can progressive MCP tool disclosure reduce control-plane tokens while
   retaining downstream task success under noisy, changing tool catalogs?
3. Does a task-wide budget reduce harmful agent loops without causing premature
   abstention on hard cases?
4. How should a policy route when remote energy is unobservable but local energy
   is measured—without laundering estimates into observations?
5. When does evidence caching create a rebound in usage, stale answers, or
   cross-tenant privacy risk despite lower per-call cost?

The strongest candidate is **quality-constrained marginal-action routing over
complete agent trajectories, with explicit measurement provenance**. It is
specific enough for a falsifiable benchmark and broader than a model router.
