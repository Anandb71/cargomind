# Evaluation plan and boundaries

## Claim

CargoMind proposes constraint-feasible paths for a bounded synthetic logistics graph, and independently verifies their factual totals, provenance and stated weighted objective. Compare its optimizer with a locally greedy next-edge baseline. The language model interprets operator goals and coordinates tools; deterministic code owns route facts, hard constraints and approval.

## Frozen dataset and labels

The simulation benchmark generator has 16 declared families: nine feasible backbone families, six infeasible families, and one mixed random family. Variations cover cost/time/risk weighting, capacity, cold chain, deadlines, closures, parallel lanes and zero objective components. All generated cases are retained. Public development uses seed `20261017`; final verification uses seed `8162026` after source review. These are synthetic operational IDs and numbers, not personal or customer records.

The independent oracle defines the labels: a feasible proposal must have an existing continuous simple path, correct totals and objective, bound evidence, and no hard-constraint violation. Optimality needs a complete independent search. A no-route answer passes only if infeasibility is independently established. A missing or incomplete oracle must return unverified/rejected. Ties are acceptable when their objective is equal under the documented numerical policy.

## Metrics and denominators

| Measurement | Denominator / interpretation |
|---|---|
| Feasible cases served | Independently proven feasible cases; abstaining on all cases cannot score well. |
| Safe, grounded, truthful proposals | Actual proposals; withheld outputs are not counted as correct routes. |
| Correct infeasible abstentions | Independently proven infeasible cases. |
| False no-route claims | Cases where a feasible route exists but the system abstains. |
| Objective gap | Verified candidate objective minus independent optimum, over feasible returned proposals. |
| Tamper detection | Forged totals, paths, evidence hashes, constraints and extra claims, including re-signed forgeries. |
| Privacy controls | Explicit attacks plus benign operational negative controls; finite coverage only. |
| Metamorphic controls | Node/edge reordering, positive objective-weight scaling and exhausted budgets. |
| Latency | Observed p50/p95 for local deterministic work; not model/network latency. |

The raw JSON report retains inputs, both systems' proposals, evaluator issues, timings and all counts. No reported synthetic score establishes general LLM truthfulness, universal privacy protection, production safety, or performance on the unreleased judge simulation. This is a greedy baseline, not a claim to outperform industrial optimization solvers.

## Adversarial and failure validation

Unit/integration tests cover strict schema types, duplicate keys and IDs, non-finite numbers, deep/oversized JSON, exact resource boundaries, invented/disconnected paths, false totals, provenance mismatches, protocol role/status/parent/hop spoofing, cross-task replay, concurrency overload/recovery, unavailable evaluators, time/expansion limits, unsafe tools, force-type confusion, private feedback, credential redaction, and model/tool output sanitation. Consequential operations remain blocked unless explicitly approved; simulation operations are read-only.

Independent review found and fixed numerical verdict forgeries (negative near-zero claims and an inflated large-valued total), initial-role spoofing, JSON-depth crashes, and privacy redaction/formatting gaps. These have explicit regressions. Further failures are reported with safe abstention rather than hidden by a success banner.

## Human control, retention and limitations

No automatic real carrier bookings, notifications or dispatches occur in the simulator. Logs omit rejected raw text; bounded in-memory healing history and local SQLite feedback persist sanitized content. Data remains local unless a configured model/API is explicitly used. A fresh operational namespace avoids reading prior demo contacts; old databases are not rewritten or deleted. Delete only a deliberately selected runtime data directory to reset its sanitized records; preserve original developer data.

Free-text personal-name detection and rule-based prompt-injection checks have finite coverage. Semantic model judging is a separate, provider-dependent measurement; `evals/guardrails.evalconfig.json` specifies groundedness and privacy/authority rubrics but a validated config is not a measured result. LLM judges can also be wrong. Decisions use deterministic independent verification as the final authority.

## Reproduce

```powershell
$env:CARGOMIND_OFFLINE='1'
$env:CARGOMIND_MCP_LOCAL='1'
$env:PYTHONUTF8='1'
uv run pytest -q
uv run cargomind benchmark --seed 8162026 --cases 64 --out docs/SIMULATION_BENCHMARK.json
```

See [SIMULATION.md](SIMULATION.md) for schemas, protocol limits, research sources and the harness adapter. The published criteria are privacy/guardrails, grounded agent evaluation, A2A orchestration, latency/execution, and token economics; the actual simulation input and scoring format remain unknown.

## OpenAI execution and economics

The default native Responses backend routes fast coordination to GPT-6 Luna and planning/criticism to GPT-6.1 Sol, with GPT-5 Mini then offline fallbacks. The earlier multi-provider chain is retained as an opt-in. Tool call IDs, opaque encrypted reasoning continuity, actual served model IDs and usage receipts are preserved; `store=False` is used. Chat is capped at 90 seconds and 24 model calls. Semantic judging is enabled with `CARGOMIND_SEMANTIC_JUDGE=1`; it is a separate tool-free OpenAI call, potentially the same model/provider, and is explicitly labeled accordingly.

Live controlled GPT-6.1 judging accepted a supported shipment claim (446 tokens,5.61s) and rejected a contradictory one (449 tokens,4.59s). A full live tracking run used6 calls,9489 input/765 output tokens in27.53s with a conservative review-required final narrative. These are individual measurements, not statistical accuracy claims.

`GET /api/usage` and SSE usage events expose bounded receipts with no prompt/evidence payloads. Costs are estimates for standard short-context text at the verified model rates, not invoices; unknown/unpriced calls remain explicit. Structured routing uses zero model tokens. Sources: [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol), [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna). The SDK-native evaluation config is validated but not represented as an executed ADK semantic benchmark.
