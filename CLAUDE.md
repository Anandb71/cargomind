# CargoMind — agent handoff notes (keep this file current; commit it with every milestone)

Hackathon: **"Problem statement: Logistics Handler"** (source: Excalidraw brief, events@2023 "D6").
Tool sets: **Agent SDK + Google products**. Judged on: multi-model agents, conversational agents, API calls,
MCP integration, feedback loop, self-healing agents, and an **A–Z README** (no half-baked info).
Submission = public GitHub link (repo currently PRIVATE: github.com/Anandb71/cargomind — make public before
submitting) via the Google Form `https://forms.gle/dZZ1BLH8vDAKr5wJ9` (registration form: forms.gle/z8TdF1V4yq3mfESN8).
Team must be 2–4 people. "First come first serve" → ship early, iterate after.

## Stack (verified versions)
- Python 3.12, **uv** project, src layout (`src/cargomind`). Windows dev box; repo at `C:\Users\anand\Repos\cargomind`
  (scratch path was too long for Windows MAX_PATH — keep short paths).
- google-adk **2.12.0** (graph-first 2.x; Sequential/Parallel/LoopAgent deprecated but work), google-genai 2.31,
  mcp **2.3.0** (FastMCP is gone → `from mcp.server.mcpserver import MCPServer`), litellm 1.105, fastapi.
- Models (config.py): fast `gemini-3.8-flash`, pro `gemini-4-argon`, backup `gemini-3.6-flash`, vision = fast,
  second vendor via LiteLLM `anthropic/claude-sonnet-5-5` or `openai/gpt-5-mini`, last link `offline/rule-engine`.
- Keys live in `.env` (gitignored; placeholders `PASTE_*` are treated as unset). All 4 keys are set on the dev box.

## Commands
```
uv sync
uv run pytest -q                       # all tests (set CARGOMIND_OFFLINE=1 CARGOMIND_MCP_LOCAL=1 for hermetic)
uv run cargomind doctor | chat | demo [--chaos 0.3] | web [--port] | mcp | reset-db
uv run adk web src                     # ADK dev UI (loads cargomind.app)
uv run python -m cargomind.evals.runner --mode offline --out docs/EVAL_REPORT.md   # (builder G)
```

## Architecture map (who owns what)
| Path | What | Status |
|---|---|---|
| `config.py` | env settings, model ids, keys, chaos knobs | done |
| `agent.py` | root `cargomind_concierge` + AgentTool specialists: tracking, route_risk (+Google Maps MCP), inventory, dispatch_pipeline (LoopAgent planner⇄critic + reporter), comms, vision_inspector, ops_briefing (Parallel fan-out + writer); `app = App(plugins=[SelfHealingPlugin()])` | done, needs live run |
| `runtime.py` | Runner + `stream_chat()` merging agent events + healing journal events | done, needs live run |
| `cli.py` | `cargomind` CLI | done (add `eval` + `autopilot` subcommands) |
| `mcp_tools.py` | `ResilientMcpToolset` (stdio MCP → in-process fallback) + `GoogleMapsMcpToolset` (Google hosted Maps MCP, optional) | done |
| `healing/offline_llm.py` | deterministic rule-engine BaseLlm (zero-key mode, last fallback) | done, tested |
| `healing/{resilient_llm,plugin,journal,chaos,breaker}.py` | self-healing layer | done, 26 tests |
| `feedback/store.py` | ratings, preferences, lessons → injected in concierge instruction | done |
| `data/{db,gazetteer}.py`, `mcp_server/server.py` | SQLite seed (India network) + MCP server (11 tools) | done, 14 tests |
| `tools/{geo,weather,planning,vision,comms}.py` | Google Routes/Geocoding→OSRM/Nominatim→haversine, Open-Meteo, dispatch planner/critic tools, Gemini vision POD, notices | builder B (in progress) |
| `web/server.py` + `web/static/*` | FastAPI + SSE mission-control dashboard | builder D (in progress) |
| `autopilot.py`, `watchtower.py` | Disruption Autopilot workflow + proactive SLA monitor | builder F (in progress) |
| `evals/*` | eval harness + ADK evalset + EVAL_REPORT.md | builder G (in progress) |
| `README.md`, `docs/*`, `Dockerfile`, CI, `deploy/` | submission docs | builder E (in progress) |

Contracts between modules: `docs/CONTRACTS.md`.

## Conventions
- Tools: typed args, LLM-grade docstrings, return dicts, never raise on "not found" (return status=error + suggestions),
  raise on transient infra errors, call `maybe_fail("tool:<name>")` first (chaos).
- Offline routing: every LlmAgent model = `build_model(tier, OfflinePolicy(rules=[OfflineRule(tool, keywords)], default_tool=...))`.
  If a prompt routes wrong offline, fix keywords in `agent.py` ROUTER_RULES / specialist policies.
- Commit after every working slice with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; push to origin/main.

## NEXT STEPS / TODO (update as you go)
1. When builders B, D, E, F, G finish: commit their files, run `uv run pytest -q`, fix integration breaks.
2. Wire `autopilot.disruption_autopilot` into agent.py (AgentTool + ROUTER rule keywords: cyclone, flood, strike,
   closure, disruption, autopilot, "alert in") and `watchtower` into web startup (builder D was asked) + CLI.
3. Add `cargomind eval` + `cargomind autopilot "<city>" --event cyclone` CLI subcommands.
4. Live run with real keys: `uv run cargomind demo` and `uv run cargomind web`; check model fallbacks, Google Maps
   MCP access (may need "Maps Grounding Lite" API enabled — otherwise it degrades gracefully).
5. Screenshots of dashboard → `assets/screenshots/`, embed in README; run evals, paste numbers in README.
6. Optional: Cloud Run deploy (gcloud installed but not logged in — needs user `gcloud auth login` + project with billing).
7. Before submission: make repo public (`gh repo edit Anandb71/cargomind --visibility public --accept-visibility-change-consequences`),
   fill team names in README, submit the Google Form (user does this).
