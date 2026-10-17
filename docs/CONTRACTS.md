# CargoMind internal contracts (module ownership + interfaces)

Python 3.12, package at `src/cargomind`, run things with `uv run ...` from repo root
(`C:\Users\anand\Repos\cargomind`). Google ADK **2.12** is installed (verified APIs below).
Settings live in `cargomind/config.py` (DB_PATH, UPLOAD_DIR, keys, model ids, CHAOS_RATE, ...).
Domain: India-based logistics network (Mumbai, Delhi, Bengaluru, Chennai, Hyderabad, Pune,
Kolkata, Ahmedabad, Jaipur, Kochi, + a few more). IDs: shipments `SHP-1001`, orders `ORD-5001`,
SKUs `SKU-xxx`, warehouses `WH-BOM`/`WH-DEL`/`WH-BLR`/`WH-MAA`/`WH-HYD`, vehicles `TRK-101`,
carriers `CR-xx`, POs `PO-xxxx`.

All tool functions: plain Python (sync or async), full type hints, Google-style docstring
(the docstring IS the LLM tool description), JSON-serialisable `dict` return. Never raise for
"not found" — return `{"status": "error", "error": "...", "suggestions": [...]}`. DO raise for
transient infra failures (network, timeout) so the self-healing plugin can retry.
Every tool that does I/O starts with `maybe_fail("tool:<name>")` from `cargomind.healing.chaos`.

## A. Data + MCP  (`cargomind/data/*`, `cargomind/mcp_server/*`)
- `data/gazetteer.py`: `CITIES: dict[str, tuple[float, float]]`, `ALIASES` (bangalore→Bengaluru,
  bombay→Mumbai, madras→Chennai, gurgaon→Gurugram, ...), `resolve_city(name) -> str | None`,
  `find_cities_in_text(text) -> list[str]` (ordered by appearance), `haversine_km(a, b)`.
- `data/db.py` (sqlite3, WAL, `DB_PATH`, auto-init + seed on first `get_conn()`):
  `init_db(reset=False)`, `get_shipment(id)`, `list_shipments(status=None, city=None, priority=None, limit=50)`,
  `update_shipment_status(id, status, city=None, note="")`, `shipment_history(id)`,
  `get_inventory(sku=None, warehouse_id=None, low_stock_only=False)`, `create_reorder(sku, warehouse_id, qty=None)`,
  `list_vehicles(status=None, city=None)`, `assign_vehicle(shipment_id, vehicle_id)`,
  `list_carriers()`, `list_warehouses()`, `kpis()`, `add_outbox(...)`, `list_outbox(limit)`.
  Shipment fields: id, order_id, customer, customer_email, customer_phone, origin_city,
  destination_city, status(created|picked_up|in_transit|delayed|out_for_delivery|delivered|exception),
  priority(standard|express|critical), weight_kg, carrier_id, vehicle_id, eta, promised_by,
  last_update, last_city, notes. Seed ~25 shipments, 5 warehouses, ~20 SKUs (some below reorder
  point), 10 vehicles, 5 carriers, event history. Deterministic seed (random.Random(42)).
- `mcp_server/server.py`: FastMCP stdio server `python -m cargomind.mcp_server.server` exposing tools
  `track_shipment, list_shipments, shipment_history, update_shipment_status, check_inventory,
  create_reorder, list_vehicles, assign_vehicle, list_carriers, list_warehouses, get_kpis`
  + resources (`logistics://kpis`, `logistics://warehouses`). Also usable from Claude Desktop/any MCP client.

## B. External-API tools  (`cargomind/tools/*`)
- `geo.py`: `geocode(place)`, `get_route(origin, destination, avoid_tolls=False)`,
  `optimize_multi_stop_route(depot, stops: list[str])`. Chain: Google Routes/Geocoding (if
  GOOGLE_MAPS_API_KEY) → OSRM/Nominatim public → haversine/gazetteer estimate. Result carries
  `source` + `degraded: bool`; each fallback hop recorded in the healing journal.
- `weather.py`: `get_weather_risk(city, hours_ahead=24)`, `check_route_disruptions(origin, destination)` (Open-Meteo, no key; offline → seasonal heuristic, degraded=True).
- `planning.py`: `quote_carriers(origin, destination, weight_kg, priority="standard")`,
  `estimate_eta(shipment_id)`, `propose_dispatch_plan(tool_context, city=None)` (greedy vehicle
  assignment, writes `tool_context.state["current_plan"]`; if `state["plan_issues"]` exists it REVISES the
  plan to fix them), `validate_dispatch_plan(tool_context)` (critic: capacity, priority/SLA, weather risk,
  driver hours; writes `state["plan_issues"]`, returns `{"valid": bool, "issues": [...], "score": 0-100}`),
  `commit_dispatch_plan(tool_context)` (applies assignments via db.assign_vehicle).
- `vision.py`: `inspect_delivery_photo(image_ref, shipment_id="")` → Gemini multimodal (google-genai, JSON
  output: damage_detected, severity, findings, recommended_action); offline → Pillow heuristic, degraded=True.
  `image_ref` = filename in `UPLOAD_DIR`.
- `comms.py`: `draft_customer_notification(shipment_id, reason, channel="email")`,
  `send_notification(shipment_id, channel, message)` (simulated outbox via db.add_outbox).

## C. Self-healing + feedback  (`cargomind/healing/*`, `cargomind/feedback/*`)
- `healing/journal.py`: `record(kind, component, detail, action="", outcome="")` → dict event
  (kinds: fault, retry, fallback, breaker_open, breaker_close, arg_repair, reflect, recovered);
  `recent(limit=50)`, `subscribe() -> asyncio.Queue`, `stats()`. In-memory ring + sqlite table.
- `healing/chaos.py`: `maybe_fail(component)`, `set_rate(r)`, `get_rate()`, `ChaosError(TransientError)`.
  Skips injection when `chaos.retrying` contextvar is set.
- `healing/breaker.py`: `CircuitBreaker(name)` with `allow()`, `success()`, `failure()`, state.
- `healing/resilient_llm.py`: `ResilientLlm(BaseLlm)` — `chain: list[BaseLlm]`; tries each in order (skip
  open breakers), restores the request between attempts, journals every fallback. Last link is
  always `OfflineLlm`. `build_model(tier: "fast"|"pro"|"vision"|"critic", policy: OfflinePolicy) -> ResilientLlm`
  — critic tier prefers Claude (second vendor) when ANTHROPIC_API_KEY set, else Gemini Pro.
- `healing/offline_llm.py` (owned by integrator): `OfflineLlm(model="offline/rule-engine", policy=OfflinePolicy(...))`.
- `healing/plugin.py`: `SelfHealingPlugin(BasePlugin)` — before_tool arg repair (ID/city normalisation),
  on_tool_error retry with backoff for transient errors (re-invoke `tool.run_async(args=..., tool_context=...)`),
  reflect hint for non-transient, per-tool breaker, after_tool detection of `{"status":"error"}`;
  on_model_error_callback graceful message. Journals everything.
- `feedback/store.py`: `add_feedback(session_id, rating, comment="", message="", agent="")`,
  `lessons(limit=8) -> list[str]`, `render_lessons_block() -> str`, `stats()`,
  `set_preference(key, value)`, `get_preferences()`; tool `remember_operator_feedback(feedback, rating=-1)`.

## D. Web dashboard  (`cargomind/web/*`)
FastAPI `cargomind.web.server:app`, run `uv run cargomind-web` (port 8080). Uses
`cargomind.runtime.stream_chat(session_id, text, image_ref=None) -> AsyncIterator[dict]` (integrator) which
yields `{"type": "agent"|"tool_call"|"tool_result"|"text"|"healing"|"done"|"error", "author": str, ...}`.

## Verified ADK 2.12 API
```python
from google.adk.agents import Agent, LlmAgent, SequentialAgent, ParallelAgent, LoopAgent
from google.adk.tools import AgentTool, FunctionTool, ToolContext, exit_loop
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams  # (server_params=StdioServerParameters, timeout=5.0)
from mcp import StdioServerParameters
from google.adk.models import BaseLlm, LlmRequest, LlmResponse, Gemini, LiteLlm, FallbackModel
from google.adk.plugins import BasePlugin, ReflectAndRetryToolPlugin
from google.adk.apps import App          # App(name=, root_agent=, plugins=[...])
from google.adk.runners import Runner, InMemoryRunner
from google.adk.sessions import InMemorySessionService
# BaseLlm: pydantic; field model: str; async def generate_content_async(self, llm_request, stream=False) -> AsyncGenerator[LlmResponse, None]
# LlmRequest: model, contents: list[types.Content], config: GenerateContentConfig, tools_dict: dict[str, BaseTool]
# BasePlugin hooks (all async, keyword args): before_tool_callback(*, tool, tool_args, tool_context),
#   after_tool_callback(*, tool, tool_args, tool_context, result), on_tool_error_callback(*, tool, tool_args, tool_context, error),
#   before_model_callback(*, callback_context, llm_request), after_model_callback(*, callback_context, llm_response),
#   on_model_error_callback(*, callback_context, llm_request, error). Returning non-None overrides.
# Gemini reads llm_request.model, so ResilientLlm must set it per candidate.
# Current Gemini ids: gemini-3.5-flash, gemini-3.1-pro-preview.
```
