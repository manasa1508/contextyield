# ContextYield decision record

This record captures the user's selected first-release direction. It distinguishes the working local reference from future production deployment.

| Decision | First-release interpretation | Boundary |
| --- | --- | --- |
| Local-first, cloud-portable | Docker Compose and a CPU-only synthetic demo make the project runnable locally. A stateless gateway, external PostgreSQL, environment-supplied secrets, and fixed route registry allow later packaging for cloud infrastructure. | No cloud deployment manifests, autoscaling, distributed rate limit, or multi-replica capacity coordination are delivered. The demo Compose file is not a production deployment. |
| Local models plus OpenAI-compatible APIs | Non-streaming chat-completions and single-text embeddings can call separately operated local or remote compatible servers. The route registry fixes each endpoint and pins a model version. A native-only provider can use an operator-run translation gateway. | A model serving engine and translation gateway are not bundled. Provider-specific API quirks, streaming, multimodal requests, model-emitted tool calls, and real-backend integration tests are not covered. Plain HTTP is local demo only; production routes require HTTPS. |
| Quality floor, then minimize energy | `choose()` removes any route below `min_quality` and other hard constraints, then ranks eligible routes by known upper-bound joules, p95 latency, priority, and ID. It never relaxes a hard floor to return an answer. | Quality values and energy profiles are operator inputs, not independently measured per prompt. Unknown energy ranks after known energy when allowed; it cannot satisfy a finite energy cap. A model with no verified quality stays at `0.0` and cannot satisfy a positive floor. |

## What to validate next

1. Select an actual local model and at least one API provider, then run the same held-out task set through both. Store evaluator version, dataset digest, model version, and observed quality; do not promote a positive score without this evidence.
2. Measure energy on eligible co-located hardware, check idle/background contribution and repeatability, and label all scopes accurately. Remote API energy remains unknown without trustworthy provider-side evidence.
3. Run the real routes in a private environment and exercise timeouts, credential rotation, database failure, and ingress TLS. The existing deterministic mock test does not substitute for this validation.

Project name **ContextYield** and the existing MIT license file remain proposed for the future public repository; no GitHub repository has been created. Publication is pending user approval and local review.
