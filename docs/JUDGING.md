# Judging checklist

**Problem statement:** Logistics Handler. **Tool sets:** Agent SDK + Google products.

This page maps each hackathon criterion to the exact files and symbols that implement it, and gives a short way to verify each one. Paths are relative to the repo root. Symbols are given instead of line numbers, so the references stay valid.

> **Prepared 90-second presentation:** follow [DEMO_SCRIPT.md](DEMO_SCRIPT.md) for MCP tracking, the parallel Disruption Autopilot, tool recovery and operator feedback. This path uses the offline rule-engine and synthetic data. Setup is outside the timer.
>
> **Reproduce locally (zero keys needed):**
> ```bash
> git clone https://github.com/Anandb71/cargomind
> cd cargomind
> uv sync
> uv run cargomind demo --chaos 0.3     # 8-step scenario under 30% fault injection
> uv run cargomind web                  # http://localhost:8080
> uv run cargomind autopilot Chennai --event cyclone   # Disruption Autopilot
> uv run cargomind eval --mode offline  # 18-scenario eval report
> ```
> Set `CARGOMIND_OFFLINE=1` for a predictable offline run even when your environment has keys. Set `CARGOMIND_MCP_LOCAL=0` to attempt stdio MCP; inspect the journal for any in-process fallback. Optional provider keys enable a separate live demonstration (see the [README](../README.md#getting-the-api-keys)). Trace `served_by` values establish the actual providers.

---

## Tool set: Agent SDK (Google ADK 2.12)

- [x] Agent graph built only from ADK primitives: `LlmAgent`, `AgentTool`, `SequentialAgent`, `ParallelAgent`, `LoopAgent`, `exit_loop` and `output_key`. See [`src/cargomind/agent.py`](../src/cargomind/agent.py): `root_agent`, `dispatch_pipeline`, `ops_briefing`, and [`autopilot.py`](../src/cargomind/autopilot.py): `disruption_autopilot` (Sequential → Parallel → commander), 23 agents in total.
- [x] ADK `App` with a runner-wide `BasePlugin`. See [`agent.py`](../src/cargomind/agent.py) `app`, and [`healing/plugin.py`](../src/cargomind/healing/plugin.py) `SelfHealingPlugin`.
- [x] ADK `Runner` + `InMemorySessionService`, streaming events. See [`runtime.py`](../src/cargomind/runtime.py): `get_runner`, `stream_chat`.
- [x] Custom ADK model classes (`BaseLlm` subclasses). See [`healing/resilient_llm.py`](../src/cargomind/healing/resilient_llm.py) `ResilientLlm`, and [`healing/offline_llm.py`](../src/cargomind/healing/offline_llm.py) `OfflineLlm`.
- [x] ADK `McpToolset` used as an MCP client. See [`mcp_tools.py`](../src/cargomind/mcp_tools.py) `ResilientMcpToolset`.
- [x] Works in the ADK Dev UI: `src/cargomind/__init__.py` exposes `app` and `root_agent`.

**Verify:** run `uv run adk web src`, open http://localhost:8000, pick **cargomind**, ask *"Plan today's dispatch from Mumbai"*. Then inspect the Events and State panels.

## Tool set: Google products

- [x] **Gemini** in four roles. See [`config.py`](../src/cargomind/config.py) `GEMINI_FAST`, `GEMINI_PRO`, `GEMINI_BACKUP` and `GEMINI_VISION`, and [`healing/resilient_llm.py`](../src/cargomind/healing/resilient_llm.py) `build_model`.
- [x] **Gemini multimodal** proof-of-delivery inspection with JSON output. See [`tools/vision.py`](../src/cargomind/tools/vision.py) `inspect_delivery_photo`. The image is also attached to the user turn in [`runtime.py`](../src/cargomind/runtime.py) `build_message`.
- [x] **Google Maps Routes API v2** (`computeRoutes`, waypoint optimisation) + **Geocoding API**. See [`tools/geo.py`](../src/cargomind/tools/geo.py): `get_route`, `optimize_multi_stop_route`, `geocode`.
- [x] **Cloud Run + Cloud Build + Secret Manager** deployment. See [`deploy/cloudrun.sh`](../deploy/cloudrun.sh) and [`Dockerfile`](../Dockerfile).
- [x] **Gemini CLI** as an MCP client of our server. See the [README MCP section](../README.md#use-it-from-claude-code-gemini-cli-or-the-mcp-inspector).

**Verify:** `uv run cargomind doctor` lists configured providers. Inspect successful route answers for `source: google_routes`; fallback sources demonstrate degraded operation, not a successful Google API call.

## "No half-baked information: A–Z instructions"

- [x] Zero-key quickstart in 5 commands: [README Quickstart](../README.md#quickstart).
- [x] Step-by-step setup for Windows, macOS and Linux, including PowerShell 5.1 caveats: [README setup](../README.md#full-setup-windows-macos-linux).
- [x] Every API key, click by click: [README keys](../README.md#getting-the-api-keys).
- [x] Every env var: [README configuration](../README.md#configuration-reference), mirroring [`config.py`](../src/cargomind/config.py).
- [x] Every command: [README CLI](../README.md#cli-reference), including `autopilot`, `watchtower` and `eval`.
- [x] Measured, not claimed: [EVAL_REPORT.md](EVAL_REPORT.md) (offline rule-engine mode: 18/18 task success, routing F1 0.974), reproducible with `uv run cargomind eval --mode offline --out docs/EVAL_REPORT.md`.
- [x] Dashboard, ADK UI, MCP clients, Docker, Cloud Run and troubleshooting are all documented in the README.
- [x] Built-in self-check: `uv run cargomind doctor` ([`cli.py`](../src/cargomind/cli.py) `_doctor`).
- [x] Architecture with sequence diagrams: [ARCHITECTURE.md](ARCHITECTURE.md). Live demo run-sheet: [DEMO_SCRIPT.md](DEMO_SCRIPT.md).

## Agent criterion: Multi-model agents

- [x] Four tiers (`fast`, `pro`, `critic`, `vision`), each a cross-vendor chain: Gemini → Gemini backup → Claude/GPT (LiteLLM) → offline engine. See [`healing/resilient_llm.py`](../src/cargomind/healing/resilient_llm.py): `build_model`, `_make_gemini`, `_make_second_vendor`.
- [x] **Planner and critic prefer different vendors.** The `critic` tier puts Claude or GPT *first*; fallback may change the actual vendor. See `build_model` and each turn's `served_by` evidence.
- [x] Models assigned per agent role in [`agent.py`](../src/cargomind/agent.py): `build_model("pro", …)` for `dispatch_planner`, `build_model("critic", …)` for `dispatch_critic`, `build_model("vision", …)` for `vision_inspector`, `build_model("pro", …)` for the autopilot's `reroute_planner` ([`autopilot.py`](../src/cargomind/autopilot.py)).
- [x] Every response is stamped with `served_by`, `fallbacks` and `tier` (`ResilientLlm.generate_content_async`) and shown in the CLI and dashboard trace.

**Verify:** with working provider credentials, ask *"Plan today's dispatch from Mumbai"* and inspect both agents' `served_by` values. The recorded development key had no Gemini Pro quota, and its Anthropic key lacked workspace scope; Gemini backups/OpenAI are fallback candidates. Configuration does not prove a particular model ran. Allow several minutes: dispatch previously took about 125 seconds.

## Agent criterion: Conversational agents

- [x] Conversational root agent `cargomind_concierge` with a dynamic instruction. See [`agent.py`](../src/cargomind/agent.py) `root_agent`, `concierge_instruction`.
- [x] Session memory across turns (`InMemorySessionService`, session id per chat). *"commit the plan"* acts on a plan produced turns earlier. See [`runtime.py`](../src/cargomind/runtime.py) and [`tools/planning.py`](../src/cargomind/tools/planning.py) `commit_dispatch_plan` (reads `state["current_plan"]`).
- [x] Clarifying questions when details are missing, and multi-specialist turns ("call several in one turn when the request spans domains").
- [x] Proactive, not only reactive: the **Watchtower** ([`watchtower.py`](../src/cargomind/watchtower.py) `run_watchtower`, started by the web server) raises SLA, exception, stock, fleet and weather alerts, each with a `suggested_prompt` the dashboard sends to the concierge in one click (`GET /api/alerts`, `POST /api/alerts/scan`).
- [x] Three conversational front ends: dashboard chat ([`web/server.py`](../src/cargomind/web/server.py) `POST /api/chat`), terminal (`uv run cargomind chat`) and the ADK Dev UI.

Weather alerts do not independently launch the autopilot: the operator clicks the suggested prompt to submit that conversation turn.

**Verify:** `uv run cargomind chat`. Ask *"Plan today's dispatch from Mumbai"*, then *"commit the plan"*.

## Agent criterion: API calls

- [x] Google Routes API v2 + Geocoding, then OSRM + Nominatim (public). See [`tools/geo.py`](../src/cargomind/tools/geo.py).
- [x] Open-Meteo forecast API. See [`tools/weather.py`](../src/cargomind/tools/weather.py): `get_weather_risk`, `check_route_disruptions`.
- [x] Gemini API (google-genai) for vision. See [`tools/vision.py`](../src/cargomind/tools/vision.py).
- [x] Anthropic / OpenAI APIs through LiteLLM. See [`healing/resilient_llm.py`](../src/cargomind/healing/resilient_llm.py) `_make_second_vendor`.
- [x] CargoMind's own REST + SSE API (OpenAPI at `/docs`). See [`web/server.py`](../src/cargomind/web/server.py).
- [x] Every API result carries `source` + `degraded`, and each fallback hop is journaled.

**Verify:** ask *"Any weather risk on Mumbai → Pune today?"* and *"Quote carriers Delhi → Bengaluru for 800kg express"*.

## Agent criterion: MCP integration

- [x] **MCP server** `cargomind-logistics` (Python MCP SDK, stdio): 11 tools + 2 resources (`logistics://kpis`, `logistics://warehouses`). See [`mcp_server/server.py`](../src/cargomind/mcp_server/server.py).
- [x] **MCP client**: specialists consume the server over stdio through ADK `McpToolset`. See [`mcp_tools.py`](../src/cargomind/mcp_tools.py) `mcp()`, used in [`agent.py`](../src/cargomind/agent.py) by `tracking_agent`, `inventory_agent`, `comms_agent`, `kpi_watch` and `stock_watch`.
- [x] **Self-healing MCP**: in-process failover with the same tool names, a breaker, and automatic reconnect. See `ResilientMcpToolset.get_tools`.
- [x] **Interoperability**: ready-made config for Claude Desktop ([`deploy/claude_desktop_config.example.json`](../deploy/claude_desktop_config.example.json)) plus commands for Claude Code, Gemini CLI and the MCP Inspector.
- [x] CI runs with `CARGOMIND_MCP_LOCAL=0`, so the real stdio transport is exercised on every push ([`.github/workflows/ci.yml`](../.github/workflows/ci.yml)).

**Verify:** `npx @modelcontextprotocol/inspector uv --directory <repo> run cargomind mcp` (needs Node.js), or add the server to Claude Desktop and ask *"which critical shipments are delayed?"*.

## Agent criterion: Feedback loop must be implemented

- [x] **Agent ⇄ agent loop:** `LoopAgent(plan_critique_loop)` = `dispatch_planner` ⇄ `dispatch_critic`, exiting through `exit_loop` when valid (max 4). See [`agent.py`](../src/cargomind/agent.py). The plan revises from critic issues stored in `state["plan_issues"]`: see [`tools/planning.py`](../src/cargomind/tools/planning.py) `propose_dispatch_plan`, `validate_dispatch_plan`.
- [x] **Human → agent loop:** dashboard thumbs/comments → `POST /api/feedback`, and the `remember_operator_feedback` tool → SQLite → `lessons()` → `render_lessons_block()`, which is injected into the concierge instruction **every turn**. See [`feedback/store.py`](../src/cargomind/feedback/store.py) and [`agent.py`](../src/cargomind/agent.py) `concierge_instruction`.
- [x] **Human-in-the-loop gate:** the plan is applied only on *"commit the plan"* (`commit_dispatch_plan` refuses unvalidated revisions unless forced). Sends, reorders and status changes happen only when explicitly asked. The Disruption Autopilot ends in an incident action plan marked *Approval needed*; its notices are drafts (`sent: false`).
- [x] **Tool → LLM loop:** structured errors with `suggestions` + `healing_hint` drive reflect-and-retry. See [`healing/plugin.py`](../src/cargomind/healing/plugin.py) `after_tool_callback`, `on_tool_error_callback`.

**Verify:** say *"Remember: always prioritise critical pharma shipments over express ones"*. The **Lessons learned** panel updates, or check from the terminal: `uv run python -c "from cargomind.feedback.store import render_lessons_block; print(render_lessons_block())"`.

## Agent criterion: Self-healing agents

- [x] **L1 Model chain** with retries, per-model circuit breakers and request rollback between attempts. See [`healing/resilient_llm.py`](../src/cargomind/healing/resilient_llm.py) `ResilientLlm`, `_RequestSnapshot`.
- [x] **L2 SelfHealingPlugin** with argument repair (`repair_args`, `normalize_id`), transient retry with backoff, per-tool breakers, reflect hints, unknown-tool recovery and graceful model-error responses. See [`healing/plugin.py`](../src/cargomind/healing/plugin.py).
- [x] **L3 API fallback chains**, flagged `degraded`. See [`tools/geo.py`](../src/cargomind/tools/geo.py), [`tools/weather.py`](../src/cargomind/tools/weather.py), [`tools/vision.py`](../src/cargomind/tools/vision.py).
- [x] **L4 MCP failover.** See [`mcp_tools.py`](../src/cargomind/mcp_tools.py) `ResilientMcpToolset`.
- [x] **L5 Chaos Monkey + journal.** See [`healing/chaos.py`](../src/cargomind/healing/chaos.py) (`maybe_fail`, `force_fail`, `set_rate`), [`healing/journal.py`](../src/cargomind/healing/journal.py) (`record`, `subscribe`, `stats`) and [`healing/breaker.py`](../src/cargomind/healing/breaker.py). Live in the dashboard (`/api/healing/stream`, chaos slider → `POST /api/chaos`).
- [x] Watchtower checks are isolated: a failing check is journaled as a `fault` and skipped, and the loop retries on the next interval ([`watchtower.py`](../src/cargomind/watchtower.py) `scan_once`, `run_watchtower`).
- [x] Last line of defence: `OfflineLlm` always answers. CI proves the full scenario completes under 30% chaos.

**Verify:** dashboard → Chaos Monkey 30% → *"Morning ops briefing"*. Inspect journaled faults and recovery; probabilistic injection may leave a turn fault-free. The [90-second demo](DEMO_SCRIPT.md) includes an offline tool-recovery backup. The [recorded eval](EVAL_REPORT.md) used 0% chaos and in-process MCP, so its 18/18 result is not a recovery or stdio-transport benchmark. For a separate live model-failover demonstration with working fallback credentials:
`uv run python -c "from cargomind.healing import chaos; chaos.force_fail('model:gemini', 50); import asyncio; from cargomind.runtime import ask; print(asyncio.run(ask('Where is SHP-1003?', verbose=True)))"`

## Submission

- [x] Public GitHub repo with an MIT [LICENSE](../LICENSE).
- [x] Detailed [README](../README.md) with end-to-end information and how to run.
- [x] CI: lint, tests, the offline end-to-end demo under chaos, and a Docker build + health probe ([`.github/workflows/ci.yml`](../.github/workflows/ci.yml)).
- [x] Reproducible installs (`uv.lock`), a container image ([`Dockerfile`](../Dockerfile)) and a one-command cloud deploy ([`deploy/cloudrun.sh`](../deploy/cloudrun.sh)).
