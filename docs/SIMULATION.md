# Verified logistics simulation

Open `/simulation` on the CargoMind server. Load the synthetic example, run the agents, inspect the independent checks and handoff chain, then test a forged total. The example deliberately contains a closed fast lane and a greedy-routing trap. Editing weights can change the accepted path.

## Run locally or in a harness

```powershell
uv sync
uv run cargomind web --port 8080
uv run cargomind simulate --input scenario.json
uv run cargomind benchmark --seed 8162026 --cases 64 --out docs/SIMULATION_BENCHMARK.json
```

HTTP interfaces:

| Interface | Contract |
|---|---|
| `GET /api/simulation/schema` | Complete JSON Schema; unknown fields are rejected. |
| `GET /api/simulation/example` | Public synthetic example, not a hidden benchmark case. |
| `POST /api/simulation/run` | Scenario object → approved proposal, proven no-route result, or rejection. |
| `POST /api/simulation/evaluate` | `{scenario, decision}` → independent evidence, constraint, truthfulness and optimality checks. |
| `POST /api/a2a` | JSON-RPC 2.0 methods `agents.list`, `simulation.run`, `decision.evaluate`. |
| `GET /.well-known/agent-card.json` | Role permissions and transport description. |

```json
{"jsonrpc":"2.0","id":"task-1","method":"simulation.run","params":{"nodes":[{"id":"HubA"},{"id":"HubB"}],"edges":[{"id":"LaneAB","source":"HubA","target":"HubB","distance_km":12,"cost":30,"travel_minutes":25,"risk":0.1,"capacity_kg":800,"cold_chain":true}],"origin":"HubA","destination":"HubB","shipment_weight_kg":500,"cold_chain_required":true,"max_risk":0.3,"weights":{"time":1,"cost":1,"distance":1,"risk":1}}}
```

Hard risk and deadline sums use high-precision decimal accumulation of the supplied numeric values. Reported floating-point totals and objectives are checked within a small absolute/ULP allowance, not a loose relative percentage.

The graph is directed. Each edge must supply observed distance, cost, travel time and risk; missing evidence is not invented. Closed edges, insufficient capacity and cold-chain mismatches are excluded. The route must meet its deadline and cumulative risk budget. Risk is additive exposure, not a probability of independent events. Nonnegative weights select a weighted additive objective; time, cost and distance are divided by their largest edge value (minimum divisor one) in the supplied graph. Weights need not sum to one. The result optimizes this stated objective, not every possible business objective or a multi-vehicle fleet schedule.

## Agent roles and protocol

The ADK conversational coordinator invokes the same simulation tool. Within that tool, separate deterministic constraint, optimization, evaluation and output-gate roles exchange validated JSON messages. Each exchange has a unique task nonce, stable scenario fingerprint, message hash, previous-message reference, hop number and allowed role/status transition. Handoffs are returned for inspection.

This is **custom CargoMind JSON handoff 1.0 over JSON-RPC 2.0**, not a claim of standard A2A compliance. Role handlers are registered trusted code in one process; the public transport accepts scenarios and evaluation requests, not executable code or arbitrary remote-agent handoffs. Hashes detect content changes and bind decisions to inputs; they are not cryptographic proof of sender identity. Remote-agent authentication is outside this implementation.

The optimizer uses bounded uniform-cost search over feasible simple paths. The evaluator separately enumerates paths, recomputes every claimed total, verifies edge continuity and constraints, checks scenario-bound evidence, and compares against its own optimum. The evaluator imports neither the optimizer algorithm nor its validity flags. An exhausted oracle budget is **unknown**, never approval. A no-route claim needs an independent complete infeasibility check.

Limits: 64 nodes, 512 edges, 32-character operational IDs, 256 KiB request bodies, 12 JSON nesting levels, finite numeric bounds, strict booleans, unique fields/IDs, bounded search and verification, bounded handoff count/time, and four concurrent simulation jobs per process. Busy requests receive 503 and can retry. This is not a distributed account quota or an authenticated public service.

## Privacy and control boundaries

Personal/contact/address/credential fields are rejected at the scenario schema boundary. Common sensitive text patterns and attempts to request secrets, bypass instructions or export private records are blocked before model/tool execution. Model context, tool results, runtime streams, feedback and healing records are sanitized. Fresh seeds use operational role aliases and blank contact fields. The default data namespace is `data/operational-v1`; legacy databases are retained untouched. An explicitly configured external database must already contain operational-only data.

Image uploads/access are disabled because unreviewed images can contain identities or personal documents. Simulation decisions never dispatch a vehicle or book a carrier. Existing operational write tools require explicit invocation-user approval with matching IDs, and force overrides are blocked. Simulated outbox records are not proof of external message delivery.

Free-text pattern checks are finite defenses, not universal name recognition or a proof that arbitrary text is free of personal data. The strict structured simulation is the primary judge interface. General natural-language truthfulness needs separate semantic evaluation; numerical route benchmark results must not be presented as a general LLM hallucination rate.

## Evaluation and research

See [EVALS.md](EVALS.md) for the frozen method and [SIMULATION_BENCHMARK.md](SIMULATION_BENCHMARK.md) plus its raw JSON for measured results. Development and held-out seeds are separate; all generated cases, failures and infeasible cases remain in the report.

Research-informed checks include input/compute/concurrency limits, least-privilege tools, separate evaluation of intermediate actions and final responses, and independent review:

- [Google ADK evaluation criteria](https://github.com/google/adk-docs/blob/main/docs/evaluate/criteria.md): groundedness, tool behavior and final-answer quality are different evaluation questions.
- [OWASP excessive agency](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/): reduce tool authority and preserve user control of consequential actions.
- [OWASP unbounded consumption](https://genai.owasp.org/llmrisk/llm102025-unbounded-consumption/): bound inputs, work and concurrency, with graceful refusal.
- [NIST AI RMF core](https://airc.nist.gov/airmf-resources/airmf/5-sec-core/): repeatable evaluation, uncertainty reporting and independent review.

`evals/guardrails.evalconfig.json` is validated against the installed ADK 2.12 schema. It configures native model-based hallucination and response-rubric evaluation, including intermediate responses. It requires a working judge model and is separate from the executed deterministic benchmark. Configuration validation alone is not evidence that a semantic evaluation passed.
