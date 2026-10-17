# CargoMind: 3-minute live demo run-sheet

**Goal.** In 180 seconds, show a conversational multi-agent system that uses several models, calls real Google APIs and MCP, checks its own plans, learns from feedback, and keeps working while we break it on purpose.

**Roles.** The **Driver** types and clicks. The **Narrator** talks. One person can do both.

---

## Pre-flight (T-10 minutes)

| Check | Command or action | Expected |
|---|---|---|
| Dependencies | `uv sync` | No errors |
| Keys | `.env` has `GOOGLE_API_KEY`, `GOOGLE_MAPS_API_KEY`, and `ANTHROPIC_API_KEY` **or** `OPENAI_API_KEY` | – |
| Self-check | `uv run cargomind doctor` | `live: gemini, claude (or openai), google_maps` |
| Fresh data | `uv run cargomind reset-db` | `database re-seeded` |
| Server | `uv run cargomind web` | `CargoMind mission control -> http://localhost:8080` |
| Browser | Open http://localhost:8080 full-screen (≥ 1280 px wide), zoom 110–125% for the room. Chaos slider at **0%** | Map, KPIs, Watchtower alerts and journal visible |
| Photo | Have a photo of a dented or crushed parcel ready on the desktop (any JPG/PNG) | – |
| Backup terminal | A second terminal open in the repo | – |
| Quiet machine | Notifications off, other tabs closed | – |

> **Venue Wi-Fi is bad?** Restart with `CARGOMIND_OFFLINE=1` in `.env`. The whole demo still runs on the offline engine and offline API fallbacks, and that becomes a talking point: *"this is the zero-network fallback."* In offline mode, use explicit IDs (*"…for SHP-1003"*) instead of pronouns.

---

## Run-sheet

| Time | Driver does | Narrator says (suggested) | Judges should notice |
|---|---|---|---|
| **0:00–0:15** | Dashboard already open | "Logistics control towers juggle a dozen tools, and AI assistants fail exactly when APIs rate-limit. CargoMind is one conversation over the whole network, built on Google ADK, Gemini, Google Maps and MCP. It doesn't go dark." | Live map of India lanes, KPI tiles, Watchtower alert panel (SLA risk, low stock, fleet, weather), empty healing journal |
| **0:15–0:40** | Type **`Morning ops briefing`** | "The concierge hands this to a briefing pipeline. Three watcher agents run **in parallel**: KPIs over our MCP server, weather at six hubs from Open-Meteo, and low stock. Then a writer agent composes the brief." | Three tool calls fire together in the trace. A structured Headline / Risks / Actions answer. `served_by` = Gemini |
| **0:40–1:00** | Type **`Where is SHP-1003 and will it make its SLA?`**, then **`Draft a WhatsApp delay notice for it`** | "Tracking goes through MCP tools, and the ETA tool flags SLA risk. It's conversational: *'it'* resolves to SHP-1003. Drafts are never sent without approval." | `track_shipment` (MCP) + `estimate_eta`. A critical PPE consignment held at a checkpost. A WhatsApp draft follows |
| **1:00–1:40** | Type **`Plan today's dispatch from Mumbai`**. When it finishes, type **`commit the plan`** | "This is the agent feedback loop. Gemini Pro **plans**, and a **different vendor**, Claude, **critiques**: capacity, driver hours, cold chain, hazmat, SLA, weather. The planner revises until the critic calls `exit_loop`. Nothing touches the database until I say commit." | Planner and critic alternate in the trace with different `served_by` values. Revision count and fixed issues. After commit, assignments are applied |
| **1:40–2:15** | Drag **Chaos Monkey to 40%**. Type **`Any weather risk on Mumbai → Pune today?`**, then **`Which SKUs need reordering?`** | "Now I'm breaking it on purpose: 40% of tool and model calls fail on the first try. Watch the journal. Faults, retries, model fallbacks and circuit breakers, and the answers still land." | Journal fills with `fault → retry → recovered`, maybe `fallback` / `breaker_open`. Both answers complete. Slide chaos back to **0%** afterwards |
| **2:15–2:35** | Type **`Remember: always prioritise critical pharma shipments over express ones`**, **or** click **thumbs-down** on an answer with the comment *"Too long, keep summaries to 3 bullets"* | "Human feedback loop: preferences and corrections are distilled into lessons (`PREFERENCE:` / `AVOID:` / `KEEP:`), which are added to the agent's instructions **on the very next turn**. No retraining, no restart." | **Lessons learned** panel updates immediately |
| **2:35–2:50** | Attach the parcel photo and type **`Inspect this delivery photo for SHP-1004`** | "Multimodal: Gemini inspects proof-of-delivery photos and returns damage, severity and a recommended action, like opening a claim." | Structured JSON-backed verdict. SHP-1004 is a seeded damage exception |
| **2:50–3:00** | Show the README criteria table, or Claude Desktop with the MCP server | "The same MCP server plugs into Claude Desktop or Gemini CLI. It runs with zero keys, deploys to Cloud Run in one command, and CI replays this whole demo under chaos on every push. That's CargoMind." | Repo, CI badge, `deploy/cloudrun.sh` |

**Short on time?** Skip the photo (2:35) and the second chaos prompt. **Extra time?** Type **`Cyclone alert in Chennai, severity high - run disruption autopilot`** (or click a Watchtower weather alert): impact assessment, then reroutes and customer-notice drafts **in parallel**, then an incident action plan awaiting approval. Or show `uv run adk web src` with the State panel (`current_plan`, `plan_issues`).

---

## Contingencies

| If this happens | Do this | Say this |
|---|---|---|
| Gemini returns 429 / quota | Nothing: keep going | "That's a real rate limit. Look at the journal: it failed over by itself." |
| An answer says `served_by: offline/rule-engine` | Keep going | "Every online model failed, so the deterministic last link answered. The turn still completed." |
| Wi-Fi drops mid-demo | Set chaos to 0 and keep going (route and weather answers show `degraded: true`). Or restart with `CARGOMIND_OFFLINE=1` | "Every external API has an offline fallback, and it's flagged so we never pretend." |
| Dashboard won't load | In the backup terminal: `uv run cargomind demo --chaos 0.3` | Narrate the same story from the terminal trace (`↳` tool calls, `⚕` healing events) |
| Autopilot via the agent is slow on a live key | In the backup terminal: `uv run cargomind autopilot Chennai --event cyclone` (direct tools, instant) | Same impact, reroutes and drafts, without the LLM write-up |
| Port 8080 busy | `uv run cargomind web --port 8090` | – |
| Photo upload fails | Skip the vision step | "Vision runs on Gemini multimodal, with an offline heuristic fallback." |

---

## Likely judge questions (one-liners)

- **"Is the critic really independent?"** Yes. The `critic` tier puts Claude or GPT **first** in its chain, and the plan's validity is decided by a deterministic validator tool, not by model opinion. See `build_model` in `healing/resilient_llm.py`.
- **"What if every model is down?"** The offline rule-engine is a real ADK `BaseLlm`. It chooses tools by intent and extracts arguments from each tool's own schema. The turn completes and CI proves it.
- **"Where does the learning live?"** In SQLite (`feedback` + `preferences`), distilled by `lessons()` and injected through a dynamic instruction on every turn.
- **"Why MCP if it's your own data?"** It makes the operations data reusable by any agent (Claude Desktop, Gemini CLI). It also gives us a real process boundary to heal across: if the server dies, the same tools are served in-process.
- **"Is anything actually sent to customers?"** No. `send_notification` writes to a simulated outbox (`/api/outbox`), and the Disruption Autopilot only drafts notices.
- **"Does it only react to questions?"** No. The Watchtower scans every 60 s and raises alerts with a ready-made question; weather alerts launch the Disruption Autopilot.
- **"How do you know it routes correctly?"** `uv run cargomind eval --mode offline` scores 18 scenarios: 18/18 task success, routing F1 0.974 in offline rule-engine mode ([EVAL_REPORT.md](EVAL_REPORT.md)).
