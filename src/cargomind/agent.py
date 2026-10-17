"""CargoMind agent graph (Google ADK).

                         ┌────────────────────────── cargomind_concierge (Gemini Flash, conversational) ─────────────────────────┐
                         │ tools: AgentTool(specialists…), commit_dispatch_plan, remember_operator_feedback                       │
   ┌──────────────┬──────┴───────┬───────────────┬─────────────────────────────┬────────────────┬───────────────┬──────────────┐
tracking_agent  route_risk_agent inventory_agent  dispatch_pipeline (Sequential)  comms_agent    vision_inspector ops_briefing (Sequential)
 (MCP tools)    (Maps+Weather)   (MCP tools)      ├ LoopAgent: planner(Gemini Pro)  (MCP+drafts)  (Gemini multimodal) ├ ParallelAgent: kpi/risk/stock watchers
                                                  │            ⇄ critic(Claude/OpenAI) → exit_loop                    └ briefing_writer
                                                  └ dispatch_reporter
Every model is a ResilientLlm chain (Gemini → second vendor → offline rule-engine) and every tool call
passes through SelfHealingPlugin (arg repair, retries, breakers, reflect-and-retry).
"""
from __future__ import annotations

import asyncio

from google.adk.agents import LlmAgent, LoopAgent, ParallelAgent, SequentialAgent
from google.adk.apps import App
from google.adk.tools import AgentTool, exit_loop

from cargomind import config
from cargomind.autopilot import disruption_autopilot
from cargomind.feedback.store import remember_operator_feedback, render_lessons_block
from cargomind.healing.offline_llm import OfflinePolicy, OfflineRule as R
from cargomind.healing.plugin import SelfHealingPlugin
from cargomind.healing.resilient_llm import build_model
from cargomind.mcp_tools import google_maps_mcp, mcp
from cargomind.tools import comms, geo, planning, vision, weather

HUBS = ["Mumbai", "Delhi", "Bengaluru", "Chennai", "Kolkata", "Hyderabad"]

COMMON = """You are part of CargoMind, an AI logistics control tower for an Indian 3PL network.
Rules: use tools for every fact (never invent shipment data, distances, weather or stock);
IDs look like SHP-1003, SKU-..., TRK-101, WH-BOM. If a tool returns status=error, read its
healing_hint / suggestions and retry with corrected arguments once before asking the user.
Mention when a result is degraded (fallback data source). Be concise, use markdown tables for lists."""


async def hub_weather_sweep(hours_ahead: int = 24) -> dict:
    """Check weather disruption risk at all major CargoMind hubs at once.

    Use for network-wide briefings ("any weather risk today?").

    Args:
        hours_ahead: Forecast window in hours (default 24).

    Returns:
        dict with per-hub risk levels and the list of high-risk hubs.
    """
    results = await asyncio.gather(
        *(weather.get_weather_risk(city=h, hours_ahead=hours_ahead) for h in HUBS), return_exceptions=True
    )
    hubs = []
    for h, r in zip(HUBS, results):
        if isinstance(r, Exception):
            hubs.append({"city": h, "risk_level": "unknown", "summary": f"unavailable: {r}"})
        else:
            hubs.append({k: r.get(k) for k in ("city", "risk_level", "summary", "source", "degraded")})
    return {"hubs": hubs, "high_risk": [h["city"] for h in hubs if h.get("risk_level") == "high"]}


# ------------------------------------------------------------------ specialists
tracking_agent = LlmAgent(
    name="tracking_agent",
    description="Tracks shipments: status, location, history, ETA and SLA risk; updates shipment status.",
    model=build_model("fast", OfflinePolicy(
        agent="tracking_agent", title="Shipment Tracking",
        rules=[
            R(tool="update_shipment_status", keywords=["mark", "set status", "update status", "mark as"]),
            R(tool="shipment_history", keywords=["history", "timeline", "events", "journey"]),
            R(tool="estimate_eta", keywords=["eta", "when will", "arrive", "late", "sla"]),
            R(tool="track_shipment", keywords=["where", "track", "status", "shp", "locate"], then=["estimate_eta"]),
            R(tool="list_shipments", keywords=["delayed", "list", "all shipments", "show shipments", "exceptions", "critical"]),
        ],
        default_tool="list_shipments",
    )),
    instruction=COMMON + """
You are the Shipment Tracking specialist. Look up shipments via the MCP logistics tools, report
status, last known city, carrier/vehicle, promised date, and call estimate_eta to flag SLA risk.
Only change a shipment status when explicitly asked.""",
    tools=[mcp("track_shipment", "list_shipments", "shipment_history", "update_shipment_status"), planning.estimate_eta],
)

route_risk_agent = LlmAgent(
    name="route_risk_agent",
    description="Routes, distances, multi-stop optimisation, carrier quotes and weather/disruption risk (Google Maps + Open-Meteo).",
    model=build_model("fast", OfflinePolicy(
        agent="route_risk_agent", title="Routes & Risk",
        rules=[
            R(tool="quote_carriers", keywords=["quote", "carrier", "cost", "price", "cheapest", "rate"]),
            R(tool="optimize_multi_stop_route", keywords=["multi-stop", "multi stop", "stops", "optimize", "optimise", "milk run"]),
            R(tool="check_route_disruptions", min_cities=2, keywords=["risk", "weather", "rain", "storm", "monsoon", "fog", "disruption", "safe to"]),
            R(tool="get_weather_risk", keywords=["risk", "weather", "rain", "storm", "monsoon", "fog", "disruption", "safe to"]),
            R(tool="get_route", keywords=["route", "distance", "how far", "drive", "travel time", "→", "->", " to "]),
            R(tool="geocode", keywords=["coordinates", "geocode", "lat"]),
        ],
        default_tool="get_route",
    )),
    instruction=COMMON + """
You are the Route & Risk specialist. Use get_route / optimize_multi_stop_route for road distance and
time (Google Routes API with automatic fallbacks), check_route_disruptions / get_weather_risk for
weather risk, quote_carriers for carrier selection. If gmaps_* tools (Google's hosted Maps MCP server)
are present, use gmaps_search_places to find depots/fuel stops/places near a city and cross-check
weather with gmaps_lookup_weather. Always state the data source and recommend an action.""",
    tools=[geo.get_route, geo.optimize_multi_stop_route, geo.geocode, weather.get_weather_risk,
           weather.check_route_disruptions, planning.quote_carriers, *google_maps_mcp()],
)

inventory_agent = LlmAgent(
    name="inventory_agent",
    description="Warehouse inventory: stock levels, low-stock alerts, reorders/purchase orders, warehouse info.",
    model=build_model("fast", OfflinePolicy(
        agent="inventory_agent", title="Inventory",
        rules=[
            R(tool="create_reorder", keywords=["reorder sku", "raise po", "purchase order", "place order", "restock sku"]),
            R(tool="list_warehouses", keywords=["warehouses", "warehouse list", "capacity"]),
            R(tool="check_inventory", keywords=["stock", "inventory", "sku", "reorder", "low", "units"]),
        ],
        default_tool="check_inventory",
    )),
    instruction=COMMON + """
You are the Inventory specialist. Use check_inventory (low_stock_only=true for alerts). Recommend
reorders for SKUs below reorder point; only call create_reorder when the user asks to place it.""",
    tools=[mcp("check_inventory", "create_reorder", "list_warehouses")],
)

# --- dispatch: planner ⇄ critic feedback loop (two different model vendors) ---
dispatch_planner = LlmAgent(
    name="dispatch_planner",
    description="Builds and revises the vehicle dispatch plan.",
    model=build_model("pro", OfflinePolicy(agent="dispatch_planner", title="Dispatch Planner", default_tool="propose_dispatch_plan")),
    instruction=COMMON + """
You are the Dispatch Planner. Call propose_dispatch_plan (pass the hub city if the user named one).
If the critic reported issues, call propose_dispatch_plan again - it revises the plan to fix them.
Then summarise the plan in 3-5 lines.""",
    tools=[planning.propose_dispatch_plan],
    output_key="dispatch_plan_summary",
)

dispatch_critic = LlmAgent(
    name="dispatch_critic",
    description="Independent critic that validates the dispatch plan.",
    model=build_model("critic", OfflinePolicy(
        agent="dispatch_critic", title="Dispatch Critic",
        default_tool="validate_dispatch_plan", finish_tool="exit_loop",
    )),
    instruction=COMMON + """
You are an independent Dispatch Critic (deliberately a different model vendor than the planner).
Call validate_dispatch_plan. If valid is true, call exit_loop immediately. Otherwise list each issue
precisely so the planner can fix it. Never approve a plan with capacity, SLA, driver-hours or
high-weather-risk issues.""",
    tools=[planning.validate_dispatch_plan, exit_loop],
)

dispatch_reporter = LlmAgent(
    name="dispatch_reporter",
    description="Reports the final dispatch plan.",
    model=build_model("fast", OfflinePolicy(agent="dispatch_reporter", title="Dispatch Plan (planner ⇄ critic)", echo_context=True)),
    instruction="""Summarise the final dispatch plan from the planner/critic exchange: revisions made,
issues fixed, final assignments table, deferred shipments and why. End by asking the operator to
reply 'commit the plan' to apply it.""",
)
dispatch_reporter.model.chain[-1].policy.help_text = "Plan ready - reply 'commit the plan' to apply it."

dispatch_pipeline = SequentialAgent(
    name="dispatch_pipeline",
    description="Plans today's vehicle dispatch with a planner⇄critic self-correcting loop. Input: hub city or 'all'.",
    sub_agents=[
        LoopAgent(name="plan_critique_loop", sub_agents=[dispatch_planner, dispatch_critic], max_iterations=4),
        dispatch_reporter,
    ],
)

comms_agent = LlmAgent(
    name="comms_agent",
    description="Customer communications: drafts and sends delay notices / delivery updates (email, SMS, WhatsApp).",
    model=build_model("fast", OfflinePolicy(
        agent="comms_agent", title="Customer Comms",
        rules=[
            R(tool="send_notification", keywords=["send it", "send the", "notify now"]),
            R(tool="draft_customer_notification", keywords=["draft", "notice", "notify", "message", "email", "sms", "whatsapp", "apolog"]),
        ],
        default_tool="draft_customer_notification",
    )),
    instruction=COMMON + """
You are the Customer Communications specialist. Draft empathetic, specific notices (ETA, cause,
next step). Only call send_notification after the operator explicitly approves the draft.""",
    tools=[comms.draft_customer_notification, comms.send_notification, mcp("track_shipment")],
)

vision_inspector = LlmAgent(
    name="vision_inspector",
    description="Multimodal proof-of-delivery inspection: analyses uploaded package photos for damage.",
    model=build_model("vision", OfflinePolicy(
        agent="vision_inspector", title="Proof-of-Delivery Inspection",
        rules=[R(tool="inspect_delivery_photo", keywords=["photo", "image", "inspect", "damage", ".jpg", ".png", "picture"])],
        default_tool="inspect_delivery_photo",
    )),
    instruction=COMMON + """
You are the Proof-of-Delivery inspector. Call inspect_delivery_photo with the image_ref given in the
request (and shipment_id if present). If damage is major, recommend marking the shipment as
'exception' and drafting a customer notice.""",
    tools=[vision.inspect_delivery_photo],
)

# --- ops briefing: parallel fan-out, then a writer ---
kpi_watch = LlmAgent(
    name="kpi_watch", description="Network KPIs.",
    model=build_model("fast", OfflinePolicy(agent="kpi_watch", title="Network KPIs", default_tool="get_kpis")),
    instruction="Call get_kpis and report the 5 most important numbers in one short list.",
    tools=[mcp("get_kpis")], output_key="kpi_snapshot",
)
risk_watch = LlmAgent(
    name="risk_watch", description="Weather risk across hubs.",
    model=build_model("fast", OfflinePolicy(agent="risk_watch", title="Weather Risk", default_tool="hub_weather_sweep")),
    instruction="Call hub_weather_sweep and report hubs with medium/high risk in one short list.",
    tools=[hub_weather_sweep], output_key="risk_snapshot",
)
stock_watch = LlmAgent(
    name="stock_watch", description="Low-stock alerts.",
    model=build_model("fast", OfflinePolicy(agent="stock_watch", title="Low Stock", rules=[R(tool="check_inventory", keywords=[""])], default_tool="check_inventory")),
    instruction="Call check_inventory with low_stock_only=true and list SKUs below reorder point.",
    tools=[mcp("check_inventory")], output_key="stock_snapshot",
)
briefing_writer = LlmAgent(
    name="briefing_writer", description="Writes the morning ops briefing.",
    model=build_model("fast", OfflinePolicy(agent="briefing_writer", title="Morning Ops Briefing", echo_context=True, help_text="Next: ask me to plan dispatch, reorder low-stock SKUs, or run the disruption autopilot.")),
    instruction="""Write a crisp morning ops briefing for the control-tower lead using:
KPIs: {kpi_snapshot?}
Weather: {risk_snapshot?}
Stock: {stock_snapshot?}
Sections: Headline (1 line), Risks, Actions (numbered, owner + urgency).""",
)
ops_briefing = SequentialAgent(
    name="ops_briefing",
    description="Network-wide morning briefing: KPIs, hub weather risk and low stock gathered in parallel, then summarised.",
    sub_agents=[ParallelAgent(name="signal_fanout", sub_agents=[kpi_watch, risk_watch, stock_watch]), briefing_writer],
)


# ------------------------------------------------------------------ concierge
def concierge_instruction(ctx) -> str:
    return COMMON + """
You are **CargoMind Concierge**, the conversational front door of the logistics control tower.
Understand the operator's goal, delegate to the right specialist tool(s) - call several in one turn
when the request spans domains - then answer with a clear, actionable summary.
Specialists: tracking_agent, route_risk_agent, inventory_agent, dispatch_pipeline, comms_agent,
vision_inspector (photos: pass the image_ref), ops_briefing,
disruption_autopilot (city-level cyclone/flood/strike/closure: pass city, event and severity).
- 'commit the plan' → commit_dispatch_plan (only after the operator confirms).
- When the operator corrects you or states a lasting preference → remember_operator_feedback.
- Ask a short clarifying question only if a required detail is missing.
""" + "\n" + render_lessons_block()


ROUTER_RULES = [
    R(tool="remember_operator_feedback", keywords=["remember", "from now on", "always ", "never ", "you were wrong", "that's wrong", "prefer "]),
    R(tool="commit_dispatch_plan", keywords=["commit the plan", "apply the plan", "approve plan", "commit plan"]),
    R(tool="disruption_autopilot", keywords=["autopilot", "cyclone", "flood", "strike", "bandh", "closure", "disruption", "alert in", "landslide"]),
    R(tool="ops_briefing", keywords=["briefing", "morning", "overview", "summary of the network", "how are we doing", "daily report"]),
    R(tool="dispatch_pipeline", keywords=["dispatch", "assign vehicles", "plan today", "allocate trucks", "load plan"]),
    R(tool="vision_inspector", keywords=["photo", "image", "picture", "damage", ".jpg", ".png", "inspect"]),
    R(tool="comms_agent", keywords=["draft", "notify", "notice", "email", "sms", "whatsapp", "message the customer", "apolog"]),
    R(tool="inventory_agent", keywords=["stock", "inventory", "sku", "reorder", "warehouse", "restock"]),
    R(tool="route_risk_agent", keywords=["route", "distance", "how far", "weather", "risk", "quote", "carrier", "rain", "→", "->", "multi-stop", "optimi"]),
    R(tool="tracking_agent", keywords=["where", "track", "shp", "status", "eta", "delayed", "shipment", "late", "history"]),
]

root_agent = LlmAgent(
    name="cargomind_concierge",
    description="Conversational logistics control tower that orchestrates specialist agents.",
    model=build_model("fast", OfflinePolicy(
        agent="cargomind_concierge", multi=True, rules=ROUTER_RULES,
        help_text=(
            "Hi, I'm **CargoMind** - your logistics control tower. Try:\n"
            "- *Where is SHP-1003?*\n- *Plan today's dispatch from Mumbai*\n"
            "- *Any weather risk on Mumbai → Pune?*\n- *Which SKUs need reordering?*\n"
            "- *Quote carriers Delhi → Bengaluru 800kg express*\n- *Draft a delay notice for SHP-1007*\n"
            "- *Morning ops briefing*\n- *Cyclone alert in Chennai - run disruption autopilot*"
        ),
    )),
    instruction=concierge_instruction,
    tools=[
        AgentTool(agent=tracking_agent),
        AgentTool(agent=route_risk_agent),
        AgentTool(agent=inventory_agent),
        AgentTool(agent=dispatch_pipeline),
        AgentTool(agent=comms_agent),
        AgentTool(agent=vision_inspector),
        AgentTool(agent=ops_briefing),
        AgentTool(agent=disruption_autopilot),
        planning.commit_dispatch_plan,
        remember_operator_feedback,
    ],
)

app = App(name=config.APP_NAME, root_agent=root_agent, plugins=[SelfHealingPlugin()])
