# CargoMind synthetic simulation benchmark

Seed: `8162026`; all **64** generated cases retained.

| Raw count / measurement | Greedy baseline | Optimizer |
|---|---:|---:|
| cases | 64 | 64 |
| proposals | 36 | 36 |
| independently_approved | 46 | 64 |
| constraint_safe_proposals | 36 | 36 |
| unsafe_proposals | 0 | 0 |
| grounded_proposals | 36 | 36 |
| truthful_proposals | 36 | 36 |
| proven_feasible_cases | 36 | 36 |
| feasible_cases_served | 36 | 36 |
| correct_infeasible_abstentions | 28 | 28 |
| false_no_route_claims | 0 | 0 |
| oracle_unknown_cases | 0 | 0 |
| objective_gap_count | 36 | 36 |
| objective_gap_mean | 1.3114824462569148 | 0.0 |
| objective_gap_max | 10.80400295906625 | 0.0 |
| latency_ms_p50 | 0.1246 | 0.128 |
| latency_ms_p95 | 0.4187 | 0.3793 |

Tampered evidence rejected: **252/252**.
Input controls passed: **7/7** (available: True).

Ordering, positive scaling and exhausted-budget controls: **130/130**.

Groundedness, truthfulness and safety counts use proposals as their denominator; feasibility coverage uses independently proven feasible cases.

- Synthetic directed graphs of 3–7 nodes; not real road or weather data
- Independent exhaustive simple-path oracle with explicit 50,000-expansion/1-second budget
- Not an LLM hallucination-rate estimate or a guarantee on arbitrary production inputs
- All seeded cases retained, including disconnected and infeasible cases; no training on hidden judge cases

Raw scenarios, decisions, verifier issues and timings are in the accompanying JSON.
