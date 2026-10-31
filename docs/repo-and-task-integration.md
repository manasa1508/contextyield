# Repository inventory and whole-task analysis

ContextYield's original energy-aware, quality-constrained route gateway remains
the execution layer. The new task layer helps an agent host measure its **whole
trajectory**. It is deliberately not a replacement for LangGraph, CrewAI,
AutoGen, OpenAI Agents, Claude, or an MLOps platform.

## What a repository scan can and cannot tell you

Run locally; no code is executed or uploaded:

```sh
contextyield scan-repo --path /absolute/path/to/your/repo --out -
```

The current scanner examines bounded Python and JavaScript/TypeScript source
files. It recognizes selected imports (model SDKs, agent frameworks, MCP,
observability, MLOps) and possible call sites. It skips symlinks, generated
directories, large files, and non-source files. Output contains relative paths,
line numbers, and categories—no source snippets or credentials. It is a
**best-effort inventory**, not a semantic data-flow or security audit. Dynamic
imports, wrappers, generated code, other languages, and vendor-specific SDKs
may be missed. A call site count is not a call count. It cannot derive billed
tokens, task quality, provider cost, or energy from code. False positives are
possible; inspect findings before instrumenting.

The useful workflow is: scan → choose the real model/tool boundaries → attach
task IDs and trace IDs in the host → emit metadata-only events → compare paired
tasks. The scanner identifies where to instrument; runtime events supply the
measurements. This division is what makes the task layer portable across
frameworks rather than pretending to understand every SDK automatically.

## Portable event contract

One JSON object per line, or `POST /v1/tasks/events` with the same object:

```json
{
  "schema_version": 1,
  "task_id": "incident-123",
  "event_id": "model-1",
  "kind": "model",
  "started_at": "2026-11-01T00:00:00Z",
  "ended_at": "2026-11-01T00:00:01Z",
  "target": "approved-model-id",
  "receipt_id": "optional-contextyield-receipt-id",
  "trace_id": "optional-trace-id",
  "prompt_tokens": 120,
  "completion_tokens": 35,
  "cost_usd": 0.001
}
```

Allowed kinds are `model`, `decision`, `retrieval`, `tool_discovery`,
`tool_execution`, `retry`, `human_escalation`, and `task_outcome`. An outcome
event can set `outcome_pass` only with `outcome_basis` of `external_eval` or
`human_review`. Missing metrics stay null; do not report zero for an unknown
cost. The JSON schema rejects extra fields, so raw prompts, responses, token text,
headers, and secrets cannot be accidentally stored under arbitrary names.
Identifiers should be opaque and non-sensitive. The event's energy basis is a
**caller attestation** (`caller_attested_local_device`) or provider report, not
independent verification by this API. Energy is broken out by basis and is not
summed across incomparable boundaries.

Events are unique per `(tenant, task_id, event_id)`. Reposting an identical
event is a no-op; changing its content returns a conflict. The tenant is taken
from the gateway API key, not from the event. Task summaries are tenant-scoped
and capped at 1,000 events. There is no retention job, distributed quota,
route-level RBAC, or independent trace-signature verification yet.

Offline example:

```sh
contextyield analyze-trace --trace fixtures/trajectory-demo.jsonl --out -
contextyield compare-traces --baseline before.jsonl --candidate after.jsonl --out -
```

The comparison pairs identical task IDs and reports step/token/cost deltas,
quality regressions, and rebound candidates. It is **descriptive**, not a
causal A/B test. Missing token/cost coverage yields null deltas. Cached tokens
are reported separately and not added to prompt tokens a second time.

## Agent/SDK integration

At each boundary in your existing agent host, emit one event with a stable
task ID. For a model call routed through ContextYield, copy the response's
`receipt_id` and `token_usage` counters. For a direct provider call, use that
provider's reported usage and mark unavailable counters as null. Record a
decision-only model (such as Jev), retrieval, tool discovery, tool execution,
retry, and human escalation as separate events—even if the host SDK calls them
all “tools.” Add a task outcome only after an external check or human review;
never mark a model's own confidence as ground-truth quality.

The HTTP endpoints are framework-neutral:

```text
POST /v1/tasks/events          append one metadata-only event
GET  /v1/tasks/{id}/summary   tenant-scoped whole-task summary
POST /v1/actions/choose       deterministic advisory next-action choice
GET  /v1/tools/search?q=...   bounded name-based discovery of registered tools
```

`/v1/actions/choose` takes a task budget, current state, and operator-supplied
candidate forecasts. It excludes actions with unvalidated predicted quality
or unverifiable hard budgets; an optional `advisor_preference` only breaks
exact ties after those checks. It returns a suggestion; the host still owns
execution, approvals, and outcome checking. It does **not** reserve budget or
enforce global concurrency. A Jev-like advisor can provide that preference
through a host adapter, but no live Jev API call is bundled or tested here.

### Local MCP adapter

Install the optional SDK and run the read-only stdio server:

```sh
uv sync --extra mcp --extra dev
CY_SCAN_ROOT=/absolute/path/to/approved/repo \
CY_GATEWAY_KEY=dev-local-key \
uv run python -m contextyield.mcp_adapter
```

The adapter exposes only `scan_project`, `search_registered_tools`, and
`get_task_summary`; it does **not** expose arbitrary paths, backend URLs,
model execution, or event writes. Set `CY_SCAN_ROOT` to one approved repo. Set
`CY_GATEWAY_URL` if the loopback gateway URL differs. For Claude Code, register
the same absolute launch command with `claude mcp add`; for Claude Desktop,
use a local `mcpServers` stdio entry. For organization-wide Cowork/custom
connectors, this local adapter is not yet a hosted OAuth-secured MCP service.
Do not put API keys into a repository config or skill file. The
[official MCP Python SDK host guide](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/get-started/real-host.md)
shows current host-specific launch syntax; verify your client's supported
protocol version.

## Evaluation plan

Use matched tasks with externally verified outcomes. Compare at least
always-strong, always-cheap, current route-only policy, task policy, and
task policy without the optional decision-model call. Report task success,
quality-floor violations, p50/p95 wall time, model/decision/tool counts,
provider token coverage and totals, actual bills if available, and energy
**only where measured at a declared boundary**. Track abstentions and human
escalations; cheap but uncompleted tasks are not savings. Freeze model versions,
route registry, prompts, budgets, and dataset splits, then repeat on held-out
tasks. The mock fixture is a contract test, not a scientific result.
