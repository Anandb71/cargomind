# CargoMind evaluation report

> 2026-10-17 13:36 India Standard Time · mode **offline** (effective: offline rule-engine) · chaos **0%** (seed 7) · **18 scenarios**, one fresh session each

## Headline numbers

| Metric | Value |
|---|---|
| **Task success** (routing recall = 1, answer pass, no error) | **16/18 (88.9%)** |
| Routing precision / recall / F1 (micro) | 0.950 / 1.000 / **0.974** |
| Routing exact-set match | 94.4% |
| Answer pass rate | 88.9% |
| Latency p50 / p95 (mean) | 0.42 s / 0.54 s (0.43 s) |
| Model fallbacks (journaled) | 0 |
| Self-healing events | 1 (reflect 1) |
| Runtime errors | 0 |

## Scenarios

| # | Scenario | Category | Prompt | Routed to | Routing F1 | Answer | Latency | Healing | Result |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `track_basic` | tracking | Where is SHP-1003? | `tracking_agent` | ✅ 1.00 | ✅ | 0.48 s | – | ✅ |
| 2 | `track_arg_repair` | tracking | shp 1001 status | `tracking_agent` | ✅ 1.00 | ✅ | 0.44 s | – | ✅ |
| 3 | `eta_sla` | eta/sla | Will SHP-1003 make its SLA? What's the ETA? | `tracking_agent` | ✅ 1.00 | ✅ | 0.42 s | – | ✅ |
| 4 | `delayed_list` | tracking | List all delayed shipments | `tracking_agent` | ✅ 1.00 | ❌ | 0.43 s | – | ❌ |
| 5 | `route_distance` | route | Mumbai → Pune distance | `route_risk_agent` | ✅ 1.00 | ✅ | 0.45 s | – | ✅ |
| 6 | `route_disruption` | weather | Any weather risk on Mumbai → Pune today? | `route_risk_agent` | ✅ 1.00 | ✅ | 0.42 s | – | ✅ |
| 7 | `weather_city` | weather | Any weather risk in Chennai in the next 24 hours? | `route_risk_agent` | ✅ 1.00 | ✅ | 0.40 s | – | ✅ |
| 8 | `carrier_quote` | carrier quote | Quote carriers Delhi → Bengaluru 800kg express | `route_risk_agent` | ✅ 1.00 | ✅ | 0.41 s | – | ✅ |
| 9 | `multi_stop` | route optimisation | Optimise a multi-stop route from the Bengaluru depot with s… | `route_risk_agent` | ✅ 1.00 | ✅ | 0.41 s | – | ✅ |
| 10 | `inventory_low` | inventory | Which SKUs are low on stock? | `inventory_agent` | ✅ 1.00 | ❌ | 0.45 s | reflect×1 | ❌ |
| 11 | `reorder` | inventory | Raise a purchase order to reorder SKU-114 at WH-HYD | `inventory_agent` | ✅ 1.00 | ✅ | 0.44 s | – | ✅ |
| 12 | `dispatch_plan` | dispatch | Plan today's dispatch from Mumbai | `dispatch_pipeline` | ✅ 1.00 | ✅ | 0.40 s | – | ✅ |
| 13 | `comms_draft` | comms | Draft a WhatsApp delay notice for SHP-1007 | `comms_agent` | ✅ 1.00 | ✅ | 0.44 s | – | ✅ |
| 14 | `ops_briefing` | briefing | Morning ops briefing | `ops_briefing` | ✅ 1.00 | ✅ | 0.42 s | – | ✅ |
| 15 | `feedback` | feedback | Remember: always prioritise critical pharma shipments over … | `remember_operator_feedback`, `tracking_agent` | ✅ 0.67 | ✅ | 0.41 s | – | ✅ |
| 16 | `vision_pod` | vision | Inspect this delivery photo pod_damaged_SHP-1007.jpg for SH… | `vision_inspector` | ✅ 1.00 | ✅ | 0.54 s | – | ✅ |
| 17 | `multi_intent` | multi-intent | Where is SHP-1002 and is there weather risk in Delhi? | `route_risk_agent`, `tracking_agent` | ✅ 1.00 | ✅ | 0.48 s | – | ✅ |
| 18 | `disruption_autopilot` | autopilot | Cyclone alert in Chennai — run disruption autopilot | `disruption_autopilot` | ✅ 1.00 | ✅ | 0.40 s | – | ✅ |

## Findings (failures and over-routing)

### `delayed_list` — FAILED

- Prompt: “List all delayed shipments”
- Expected root tools: `tracking_agent` · routed: `tracking_agent` (P 1.00 / R 1.00)
- Missing in answer: any of: delayed
- Answer excerpt: `### Shipment Tracking **list shipments** - **status**: ok - **count**: 4 **shipments**: \| id \| order id \| customer \| customer email \| customer phone \| origin city \| destination city \| \|---\|---\|---\|---\|---\|---\|---\| \| SHP-10…`

### `inventory_low` — FAILED

- Prompt: “Which SKUs are low on stock?”
- Expected root tools: `inventory_agent` · routed: `inventory_agent` (P 1.00 / R 1.00)
- Failure markers in answer: '**status**: error'
- Answer excerpt: `### Inventory **check inventory** - **status**: error - **error**: Warehouse 'WH-ICH' not found - **suggestions**: ["WH-HYD"] - **hint**: Warehouse ids: WH-BOM, WH-DEL, WH-BLR, WH-MAA, WH-HYD (or a city name). - **healing hint**: Re-check …`

### `feedback` — passed, but over-routed

- Prompt: “Remember: always prioritise critical pharma shipments over express ones”
- Expected root tools: `remember_operator_feedback` · routed: `remember_operator_feedback`, `tracking_agent` (P 0.50 / R 1.00)
- Answer excerpt: `- **status**: ok - **stored**: True - **preference**: operator_rule:remember_always_prioritise_critical_pharma_shipm - **lessons active**: 1 - **message**: Saved as a lasting preference; it will be applied to all future turns. ### Shipment…`


## Agent patterns covered

| Scenario | Pattern exercised |
|---|---|
| `track_basic` | AgentTool -> tracking_agent -> MCP track_shipment + estimate_eta |
| `track_arg_repair` | sloppy ID -> arg normalisation (SHP-1001) |
| `eta_sla` | AgentTool -> tracking_agent -> estimate_eta |
| `delayed_list` | AgentTool -> tracking_agent -> MCP list_shipments(status=delayed) |
| `route_distance` | AgentTool -> route_risk_agent -> get_route (Routes API -> OSRM -> haversine) |
| `route_disruption` | AgentTool -> route_risk_agent -> check_route_disruptions |
| `weather_city` | AgentTool -> route_risk_agent -> get_weather_risk (single city) |
| `carrier_quote` | AgentTool -> route_risk_agent -> quote_carriers |
| `multi_stop` | AgentTool -> route_risk_agent -> optimize_multi_stop_route (NN + 2-opt) |
| `inventory_low` | AgentTool -> inventory_agent -> MCP check_inventory(low_stock_only) |
| `reorder` | AgentTool -> inventory_agent -> MCP create_reorder |
| `dispatch_plan` | Sequential[Loop(planner <-> critic, exit_loop), reporter] |
| `comms_draft` | AgentTool -> comms_agent -> draft_customer_notification |
| `ops_briefing` | Sequential[Parallel(kpi, weather, stock watchers), writer] |
| `feedback` | concierge tool remember_operator_feedback -> lessons injected into instructions |
| `vision_pod` | AgentTool -> vision_inspector -> inspect_delivery_photo (Gemini multimodal / Pillow) |
| `multi_intent` | concierge fans out to 2 specialists in one turn |
| `disruption_autopilot` | autonomous disruption workflow |

## Self-healing journal (this run)

| Kind | Count |
|---|---|
| reflect | 1 |

Journal recovery rate (recovered / (recovered + failed)): n/a

## Environment

- Date: 2026-10-17 13:36 India Standard Time
- Mode requested: `offline` · effective: **offline rule-engine**
- Live providers: none (offline rule-engine) · offline fallback: always on
- Turns served by: `offline/rule-engine` ×18
- Configured models: fast=`gemini-3.8-flash`, pro=`gemini-4-argon`, vision=`gemini-3.8-flash`, backup=`gemini-3.6-flash`, claude=`anthropic/claude-sonnet-5-5`, openai=`openai/gpt-5-mini`
- Chaos rate 0%, seed 7 · MCP: in-process (CARGOMIND_MCP_LOCAL)
- Python 3.12.15 · google-adk 2.12.0 · Windows 11
- Reproduce: `uv run python -m cargomind.evals.runner --mode offline --out docs/EVAL_REPORT.md`

_Metric definitions: routing scores compare the set of tools the root concierge called with the expected set; answer pass = at least one expected phrase, all required IDs/cities, and no failure marker (e.g. a clarification fallback or a raw tool error); latency is wall-clock per turn including all sub-agents and tool calls; recovery rate = scenarios that still fully succeeded while faults were injected._
