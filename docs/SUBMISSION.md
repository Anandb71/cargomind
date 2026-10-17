# CargoMind — Logistics Handler

**One-line pitch:** A conversational logistics control tower that turns disruptions into reviewed action plans and keeps working when models, APIs or tools fail.

**Public repository:** [github.com/Anandb71/cargomind](https://github.com/Anandb71/cargomind)

**Team:** Anand B ([Anandb71](https://github.com/Anandb71)) and Junaid Ahamed ([JunaidAhamed-7777](https://github.com/JunaidAhamed-7777)).

**Hosted demo:** [cargomind-lilac.vercel.app](https://cargomind-lilac.vercel.app). Dashboard, health, KPIs and static assets passed anonymous HTTP checks; live SSE tracking completed with tool results and a final answer. Vercel demo data and sessions are temporary per instance, and Watchtower scans run on demand. Local dashboard instructions are in the [README](../README.md).

## Problem and solution

Logistics operators must reconcile shipment status, SLA risk, weather, inventory, fleet constraints and customer updates across separate systems. A delayed shipment can require several coordinated decisions, while a failed API or unavailable model interrupts the workflow.

CargoMind brings those operations into one conversation. Ask where a shipment is, assess whether it will miss its promise, compare routes and carriers, inspect a delivery photo, prepare dispatch assignments or respond to a city-level disruption. The dashboard exposes the agent trace, data sources, recovery events and operator lessons so decisions can be inspected.

## What makes it agentic

- **Google Agent Development Kit:** specialists work through a conversational concierge. Dispatch uses a planner/critic loop with deterministic constraint validation. Disruption Autopilot assesses impact, runs reroute planning and notice drafting in parallel, then produces an incident action plan.
- **Google products:** Gemini models power agent roles and multimodal parcel inspection; Google Maps Routes and Geocoding integrations support routing, with explicit fallback sources. A Docker image and Cloud Run deployment script are included.
- **Multiple models and providers:** role-specific model chains prefer Gemini and a second-vendor critic through LiteLLM, with backups and a deterministic offline rule-engine. The trace's `served_by` field records the actual provider; configured models are not a promise that every provider was used.
- **MCP integration:** an 11-tool logistics server exposes operational data over stdio for ADK and other MCP clients. The client can recover through equivalent in-process tools if the transport fails.
- **Feedback:** planner/critic revisions improve a proposed dispatch plan, while operator ratings and preferences become persistent lessons in subsequent concierge instructions.
- **Self-healing:** transient retries, argument repair, circuit breakers, provider/API fallbacks and a live healing journal keep failures visible. Chaos injection makes tool recovery demonstrable.

Dispatch assignments require an explicit commit. Autopilot notices are drafts; notification sending is simulated in an outbox. The seeded logistics network and sample parcel images are synthetic.

## Verified results and limits

| Check | Result |
|---|---|
| Automated tests | **100 passed** |
| Offline scenarios, 0% chaos | **18/18 passed**; routing F1 **0.974** |
| Offline scenarios, 30% chaos | **18/18 passed** |
| Journal in the 30% chaos run | **14 fault events, 12 retry events and 12 recovered events** |

Fault, retry and recovered counts are distinct journal event counts; 14 faults do not mean 14 completed recoveries. These offline results validate task routing, required answer content and completion under the tested conditions. They are not measurements of live model reasoning quality or proof of cross-provider failover. See the [evaluation report](EVAL_REPORT.md) for definitions and the recorded baseline environment.

Live development calls also completed tracking, quote/weather and dispatch workflows. Provider access varies: the development Gemini Pro key was quota-limited and the Anthropic key lacked workspace scope, so fallback behavior and actual `served_by` evidence matter.

## Judge walkthrough

Use the prepared [90-second demo](DEMO_SCRIPT.md): track an SLA-sensitive shipment through the logistics toolset, run a simulated Chennai cyclone through the parallel autopilot, inspect tool recovery under chaos, then save an operator preference. The [judging checklist](JUDGING.md) maps every criterion to implementation files and verification steps. The [README](../README.md) covers setup, API keys, configuration, MCP clients, deployment and troubleshooting.

## Submission

Submit the public repository link using the [official submission form](https://forms.gle/dZZ1BLH8vDAKr5wJ9). The team submits the form; this document is prepared submission copy, not confirmation that a submission was sent.
