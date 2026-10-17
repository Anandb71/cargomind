# CargoMind — agent handoff notes (keep this file current; commit it with every milestone)

Hackathon: **"Problem statement: Logistics Handler"** (source: Excalidraw brief, events@2023 "D6").
Tool sets: **Agent SDK + Google products**. Judged on: multi-model agents, conversational agents, API calls,
MCP integration, feedback loop, self-healing agents, and an **A–Z README** (no half-baked info).
Submission = public GitHub link (repo PUBLIC: github.com/Anandb71/cargomind; visibility and history audited 2026-10-17)
via the Google Form `https://forms.gle/dZZ1BLH8vDAKr5wJ9` (registration form: forms.gle/z8TdF1V4yq3mfESN8).
Team must be 2–4 people. "First come first serve" → ship early, iterate after.

## Stack (verified versions)
- Python 3.12, **uv** project, src layout (`src/cargomind`). Windows dev boxes; repo at `C:\Users\anand\Repos\cargomind`
  or `C:\Github\cargomind` (keep paths short — Windows MAX_PATH).
- google-adk **2.12.0** (graph-first 2.x; Sequential/Parallel/LoopAgent deprecated but work), google-genai 2.31,
  mcp **2.3.0** (FastMCP is gone → `from mcp.server.mcpserver import MCPServer`), litellm 1.105, fastapi.
- Models (config.py): fast `gemini-3.8-flash`, pro `gemini-4-argon`, backup `gemini-3.6-flash`, vision = fast,
  second vendor via LiteLLM `anthropic/claude-sonnet-5-5` or `openai/gpt-5-mini`, last link `offline/rule-engine`.
- Keys live in `.env` (gitignored; placeholders `PASTE_*` are treated as unset). The same keys are GitHub Actions secrets
  (ANTHROPIC/GOOGLE/GOOGLE_MAPS/OPENAI_API_KEY, GOOGLE_GENAI_USE_VERTEXAI, CARGOMIND_OFFLINE, CARGOMIND_CHAOS) but secrets
  are write-only, so a new dev box needs `.env` filled by hand (template: `.env.example`).

## Commands
```
uv sync
uv run pytest -q                       # all tests (set CARGOMIND_OFFLINE=1 CARGOMIND_MCP_LOCAL=1 for hermetic)
uv run cargomind doctor | chat | demo [--chaos 0.3] | web [--port] | mcp | reset-db
uv run cargomind autopilot Chennai --event cyclone --severity high [--agent]   # Disruption Autopilot
uv run cargomind watchtower            # one proactive alert scan
uv run cargomind eval --mode offline --out docs/EVAL_REPORT.md                  # flags pass to evals.runner
uv run adk web src                     # ADK dev UI (loads cargomind.app)
```

## Architecture map (who owns what)
| Path | What | Status |
|---|---|---|
| `config.py` | env settings, model ids, keys, chaos knobs | done |
| `agent.py` | root `cargomind_concierge` + AgentTool specialists: tracking, route_risk (+Google Maps MCP), inventory, dispatch_pipeline (LoopAgent planner⇄critic + reporter), comms, vision_inspector, ops_briefing (Parallel fan-out + writer), disruption_autopilot; `app = App(plugins=[SelfHealingPlugin()])` | done, LIVE verified |
| `runtime.py` | Runner + `stream_chat()` merging agent events + healing journal events | done, needs live run |
| `cli.py` | `cargomind` CLI: chat, demo, web, mcp, doctor, reset-db, autopilot, watchtower, eval | done |
| `mcp_tools.py` | `ResilientMcpToolset` (stdio MCP → in-process fallback) + `GoogleMapsMcpToolset` (Google hosted Maps MCP, optional) | done |
| `healing/offline_llm.py` | deterministic rule-engine BaseLlm (zero-key mode, last fallback) | done, tested |
| `healing/{resilient_llm,plugin,journal,chaos,breaker}.py` | self-healing layer | done, 26 tests |
| `feedback/store.py` | ratings, preferences, lessons → injected in concierge instruction | done |
| `data/{db,gazetteer}.py`, `mcp_server/server.py` | SQLite seed (India network) + MCP server (11 tools) | done, 14 tests |
| `tools/{geo,weather,planning,vision,comms}.py` | Google Routes/Geocoding→OSRM/Nominatim→haversine, Open-Meteo, dispatch planner/critic tools, Gemini vision POD, notices | done, 24 tests; LIVE verified |
| `web/server.py` + `web/static/*` | FastAPI + SSE mission-control dashboard; lifespan starts watchtower (`CARGOMIND_WATCHTOWER=0` disables) | done |
| `autopilot.py` | Disruption Autopilot: Sequential[impact_assessor → Parallel[reroute_planner, notice_drafter] → autopilot_commander]; `run_autopilot()` direct helper | done, `tests/test_autopilot.py` |
| `watchtower.py` | proactive monitor: `scan_once()`, `latest_alerts()`, `run_watchtower(60)`; SLA/exception/low-stock/fleet/hub-weather alerts with `suggested_prompt` | done |
| `evals/*` | eval harness (`scenarios.py`, `runner.py`) → `docs/EVAL_REPORT.md` + `docs/eval_results.json` | done (offline routing F1 0.97) |
| `README.md`, `docs/*`, `Dockerfile`, CI, `deploy/` | submission docs (A–Z README, ARCHITECTURE, DEMO_SCRIPT, JUDGING) | done; keep numbers in sync with EVAL_REPORT |

Contracts between modules: `docs/CONTRACTS.md`.

## Conventions
- Tools: typed args, LLM-grade docstrings, return dicts, never raise on "not found" (return status=error + suggestions),
  raise on transient infra errors, call `maybe_fail("tool:<name>")` first (chaos).
- Offline routing: every LlmAgent model = `build_model(tier, OfflinePolicy(rules=[OfflineRule(tool, keywords)], default_tool=...))`.
  If a prompt routes wrong offline, fix keywords in `agent.py` ROUTER_RULES / specialist policies.
- Commit after every working slice with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Workflow: small feature branches + one PR per slice (`gh pr create`), stacked when dependent. Don't merge without the user's OK.**
- Shell gotcha: in this harness, bash heredocs collapse `\\` and mangle `\n` inside Python snippets — edit files directly instead.

## Live-run findings (2026-10-17, real keys)
- Live works end-to-end: tracking ~33s, quote+weather ~52s, network dispatch ~125s (loop converges rev 2, score 85).
- `gemini-4-argon` → **429 RESOURCE_EXHAUSTED** on this key (free tier has no pro quota) → auto-fallback to `gemini-3.6-flash`.
- Anthropic key → **"API key is not scoped to a workspace"** (400). Needs a workspace-scoped key from console.anthropic.com,
  else critic falls through to OpenAI. ResilientLlm now treats quota/auth errors as permanent: no retry, breaker trips at once.
- Google **Geocoding API not enabled** on the Maps key → Nominatim fallback (Routes API works).
- Tests: 108 passed (hermetic: `CARGOMIND_OFFLINE=1 CARGOMIND_MCP_LOCAL=1 PYTHONUTF8=1 uv run pytest -q`).
- On Windows always set `PYTHONUTF8=1` when printing agent output (₹, ⇄ in text).

## Status log
- Pitch demo check (2026-10-17): hosted tracking completed in 22s and Chennai cyclone proposals/notices in 75s without stream errors. Local zero-key backup verified tracking, dispatch, cyclone response and a forced tool fault/retry/recovery. Fixed offline dispatch reports retaining a revision-1 critic verdict after a newer validation; 108 tests pass, including real ADK context-fencing regressions.
- Map cache refresh (2026-10-17): versioned the dashboard script URL after confirming production serves the keyless Esri basemap. Reopening the dashboard now fetches the replacement script instead of a cached CARTO version; no browser-facing API key is required.
- Final integration: PRs #1–#6 merged; full hermetic suite 106 passed. Weather-aware staging rejects unknown/high-risk hubs; basemap and cache fixes included.
- 2026-10-17 (release review): PRs #1–#4 merged after 100 tests, offline 18/18 and 30%-chaos 18/18 evaluations.
  Public Vercel demo: https://cargomind-lilac.vercel.app; live SSE tracking verified with Gemini backup/OpenAI fallback.
  Vercel entrypoint app.py uses temporary instance-local data; background Watchtower is disabled, manual scans work.
  Submission copy: docs/SUBMISSION.md. Original developer checkout's uncommitted files were left untouched.
- 2026-10-17 (session 2, C:\Github\cargomind): pulled the WIP snapshot (dashboard, evals, docs, CI, Docker). Added
  `autopilot.py` + `watchtower.py`, wired autopilot into the concierge (AgentTool + ROUTER keywords: autopilot, cyclone,
  flood, strike, bandh, closure, disruption, "alert in", landslide), CLI `autopilot` / `watchtower` / `eval`.
  Offline fixes: SKU regex needs a digit ("SKUs" was parsed as SKU-S); single-city weather → get_weather_risk instead of
  two-city check_route_disruptions; event/severity arg extraction. Created local `.env` with PASTE_* placeholders.
  Offline eval 18/18 (also fixed "Which" → WH-ICH regex, table cap dropping `status`). Live smoke run done (above).
  PR stack (each based on the previous): Anandb71/cargomind#1 autopilot+watchtower ← #2 CLI + offline fixes ←
  #3 eval 18/18 + handoff notes ← #4 README/docs ← #5 weather-aware staging hub + live findings ←
  #6 keyless Esri basemap (CARTO `dark_all` now returns HTTP-200 "API KEY REQUIRED" tiles, so `tileerror` never fires)
  + `/static` sent with `Cache-Control: no-cache`. PR #6 contains no screenshots or README changes.
  Teammate merges the stack bottom-up (#1 into main first; GitHub retargets the next PR when its base branch is deleted).
- Optional screenshot recipe (screenshots remain a TODO): Playwright via `uv run --no-project --with playwright python shot.py` using the installed Edge
  (`chromium.launch(channel="msedge")`), so no browser download; the dev server is in `.claude/launch.json` (port 8099).
- PR #6 review milestone: completed Esri attribution with the GIS user community credit, matching both service
  metadata responses; base and reference India tiles returned HTTP 200 without credentials. Corrected PR scope to
  basemap/cache restoration only; screenshots are not included. No autopilot or deployment behavior changed.

## NEXT STEPS / TODO (update as you go)
1. User authorized merging PRs #1–#4. Review hardening verified: real warehouse alternatives, outbound HOLD precedence,
   zero-fuel alerts, stale alert retention, city-aware offline risk routing; 100 tests and 18/18 offline eval scenarios pass.
2. Live run with real keys (`cargomind doctor`, `cargomind autopilot Chennai --agent`, `cargomind web`): record per-step
   served_by/fallbacks above. Google Maps MCP may need "Maps Grounding Lite" API enabled (degrades gracefully).
3. Screenshots of dashboard → `assets/screenshots/`, embed in README.
4. Optional: Cloud Run deploy (`deploy/cloudrun.sh`; needs user `gcloud auth login` + project with billing).
5. Repo is public and team names are filled from confirmed GitHub collaborators: Anand B (@Anandb71), Junaid Ahamed
   (@JunaidAhamed-7777). Submit the Google Form (user does this). Use docs/DEMO_SCRIPT.md for the 90-second judge path.

- PR #5 review fix (2026-10-17): alternate staging checks real warehouse cities and accepts only confirmed low/medium weather risk; unavailable/high-risk alternatives produce a soft error with no movements. Main review fixes retained. Targeted autopilot/watchtower tests: 28 passed (offline/local MCP).
