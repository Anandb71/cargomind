# CargoMind: 90-second judge demo

**Goal:** show ADK orchestration, MCP operations, recovery and operator feedback on a prepared dashboard. The timed path uses the offline rule-engine, synthetic data and API fallbacks. It does not demonstrate live model quality or current weather.

## Prepare before the timer

Install with `uv sync`, then start a fresh PowerShell terminal:

```powershell
$env:CARGOMIND_OFFLINE="1"
$env:CARGOMIND_MCP_LOCAL="0"
$env:PYTHONUTF8="1"
uv run cargomind web
```

macOS/Linux: `CARGOMIND_OFFLINE=1 CARGOMIND_MCP_LOCAL=0 uv run cargomind web`.

Open [localhost:8080](http://localhost:8080) with the trace, healing journal and Lessons learned panel visible. Set Chaos Monkey to 0%. `CARGOMIND_MCP_LOCAL=0` attempts the real stdio MCP server; the journal identifies any in-process fallback. Confirm transport during rehearsal. Use explicit shipment IDs in offline prompts.

## Timed path (target: 90 seconds after startup)

| Time | Action | Explain and verify |
|---|---|---|
| 0:00–0:10 | Show the network dashboard and Watchtower alerts. | One conversation over shipments, routes, stock and dispatch. State explicitly that this fast path uses the offline rule-engine. |
| 0:10–0:25 | Ask **`Where is SHP-1003 and will it make its SLA?`** | Show the tracking specialist and logistics tool calls. The specialist consumes the MCP toolset; transport fallback is separately journaled. |
| 0:25–0:45 | Ask **`Cyclone alert in Chennai, severity high - run disruption autopilot`**. | Show impact assessment → **parallel** reroute planner and notice drafter → commander. These are proposed actions and notification drafts. Inspect `sent: false` and approval wording. This is a simulated disruption. |
| 0:45–1:05 | Set Chaos Monkey to **40%** and ask **`Which SKUs need reordering?`**. Restore **0%** afterwards. | Inspect any injected fault and recovery in the journal. Injection is probabilistic; a turn may have no fault. Offline mode demonstrates tool recovery, not cross-provider model failover. |
| 1:05–1:20 | Ask **`Remember: always prioritise critical pharma shipments over express ones`**. | Show the saved lesson. Operator feedback persists and is injected into subsequent concierge instructions. |
| 1:20–1:30 | Open [EVAL_REPORT.md](EVAL_REPORT.md) and [JUDGING.md](JUDGING.md). | Recorded offline result: **18/18** task success, routing F1 **0.974**. That run used **0% chaos and in-process MCP**; recovery and stdio transport require separate evidence. |

These times are a presentation target, not a benchmark. If a step runs long, skip repeat chaos prompts and show the report. Watchtower alerts provide a **suggested prompt**: clicking it submits a conversation turn. Weather alerts do not independently launch the autopilot.

## Repeatable recovery backup

With offline mode still enabled, rehearse this in a second terminal for a deterministic tool failure:

```powershell
uv run python -c "from cargomind.healing import chaos; chaos.force_fail('tool:get_weather_risk', 1); import asyncio; from cargomind.runtime import ask; print(asyncio.run(ask('Any weather risk in Chennai in the next 24 hours?', verbose=True)))"
```

Inspect emitted journal evidence. Describe a tool retry as tool recovery. `uv run cargomind demo --chaos 0.3` is a longer terminal alternative when the dashboard is unavailable.

## Live alternative (allow several minutes)

Restart without `CARGOMIND_OFFLINE=1`, with optional keys in `.env`, then run `uv run cargomind doctor`. Its provider list reflects configuration; successful calls and trace `served_by` values establish which providers actually worked.

Measured development runs took about **33 seconds for tracking**, **52 seconds for quote plus weather**, and **125 seconds for dispatch**. Do not promise this full live flow in 90 seconds or three minutes.

1. Ask **`Where is SHP-1003 and will it make its SLA?`**. Show facts and trace.
2. Ask **`Plan today's dispatch from Mumbai`**. Show the planner/critic loop and deterministic validator. Apply assignments with **`commit the plan`** only when you intend to change demo data.
3. Optionally ask **`Any weather risk on Mumbai → Pune today?`**, or attach `assets/samples/pod_damaged_SHP-1007.jpg` for an inspection. Show `source` and `degraded` alongside results.

The planner prefers Gemini Pro and the critic prefers a second vendor, but actual providers depend on fallback. On the recorded development key, Gemini Pro lacked quota and the Anthropic key lacked workspace scope; Gemini backups and OpenAI are fallback candidates. **The trace proves the provider used on a particular turn.** Offline photo inspection is a heuristic, not Gemini vision.

## Judge questions

- **Is the critic independent?** Its tier prefers another vendor; fallback can change that. A deterministic tool validates capacity, driver hours and other constraints regardless of model.
- **What if models fail?** Retry and breakers lead through provider fallbacks to an ADK `BaseLlm` rule-engine. Show live model failover separately from this offline path.
- **Where does learning live?** SQLite feedback/preferences become lessons in subsequent concierge instructions; no retraining.
- **Why MCP?** Logistics tools are reusable by Claude Desktop, Gemini CLI and other clients. The stdio process boundary also has in-process recovery.
- **Are customers contacted?** Notifications are simulated outbox entries. Autopilot notices are drafts; no external customer message is delivered.
- **What do eval numbers prove?** The [report](EVAL_REPORT.md) documents offline routing and phrase/ID checks, with mode and limitations. It is not a live-provider or chaos-recovery benchmark.
