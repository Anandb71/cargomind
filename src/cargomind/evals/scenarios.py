"""Golden evaluation scenarios for CargoMind.

Each scenario is one operator turn, replayed in a fresh session. Together they
cover every specialist and every ADK pattern in the agent graph:

* AgentTool delegation to each specialist (tracking, route & risk, inventory,
  comms, vision) - MCP tools, Google Maps / Open-Meteo tools, multimodal.
* SequentialAgent + LoopAgent planner <-> critic (``dispatch_pipeline``).
* SequentialAgent + ParallelAgent fan-out (``ops_briefing``).
* Direct concierge tools (``remember_operator_feedback``).
* Multi-intent fan-out (one turn -> two specialists).
* Self-healing arg repair (``shp 1001`` -> ``SHP-1001``).

Scoring (see :mod:`cargomind.evals.runner`):

* ``expected_root_tools`` - tools the concierge (root agent) should call; scored
  with precision / recall / F1 against the tools it actually called.
* ``must_contain`` - lowercase substrings; the answer passes if ANY appears.
* ``must_contain_all`` - lowercase substrings that must ALL appear (IDs, cities).
* ``must_not_contain`` - failure markers (clarification fallbacks, raw errors);
  the answer fails if any appears.

Only cities from :data:`cargomind.data.gazetteer.CITIES` are used, so the
offline rule-engine can resolve every place name.
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field

# Text that means the system did not actually complete the task.
DEFAULT_FAIL_MARKERS: tuple[str, ...] = (
    "need a bit more detail",  # offline rule-engine could not extract required args
    "**status**: error",  # raw tool error surfaced as the final answer
    "traceback (most recent call last)",
)


@dataclass(frozen=True)
class Scenario:
    id: str
    category: str
    prompt: str
    expected_root_tools: frozenset[str]
    must_contain: tuple[str, ...] = ()
    must_contain_all: tuple[str, ...] = ()
    must_not_contain: tuple[str, ...] = DEFAULT_FAIL_MARKERS
    image_ref: str | None = None
    pattern: str = ""  # agent pattern exercised, for the report
    requires: str | None = None  # optional module; scenario is skipped when absent
    tags: tuple[str, ...] = field(default_factory=tuple)

    def available(self) -> bool:
        if not self.requires:
            return True
        try:
            return importlib.util.find_spec(self.requires) is not None
        except (ImportError, ValueError):
            return False


def _s(id, category, prompt, tools, any_=(), all_=(), **kw) -> Scenario:  # noqa: A002 - terse table below
    return Scenario(
        id=id,
        category=category,
        prompt=prompt,
        expected_root_tools=frozenset(tools),
        must_contain=tuple(s.lower() for s in any_),
        must_contain_all=tuple(s.lower() for s in all_),
        **kw,
    )


SCENARIOS: list[Scenario] = [
    # --- tracking (MCP logistics server) -------------------------------------------------
    _s("track_basic", "tracking", "Where is SHP-1003?",
       {"tracking_agent"}, any_=["shp-1003"],
       pattern="AgentTool -> tracking_agent -> MCP track_shipment + estimate_eta"),
    _s("track_arg_repair", "tracking", "shp 1001 status",
       {"tracking_agent"}, any_=["shp-1001"],
       pattern="sloppy ID -> arg normalisation (SHP-1001)", tags=("self-healing",)),
    _s("eta_sla", "eta/sla", "Will SHP-1003 make its SLA? What's the ETA?",
       {"tracking_agent"}, any_=["eta"], all_=["shp-1003"],
       pattern="AgentTool -> tracking_agent -> estimate_eta"),
    _s("delayed_list", "tracking", "List all delayed shipments",
       {"tracking_agent"}, any_=["delayed"], all_=["shp-"],
       pattern="AgentTool -> tracking_agent -> MCP list_shipments(status=delayed)"),
    # --- routes, risk, carriers (Google Maps / OSRM / Open-Meteo with fallbacks) --------------
    _s("route_distance", "route", "Mumbai → Pune distance",
       {"route_risk_agent"}, any_=["km"], all_=["pune"],
       pattern="AgentTool -> route_risk_agent -> get_route (Routes API -> OSRM -> haversine)"),
    _s("route_disruption", "weather", "Any weather risk on Mumbai → Pune today?",
       {"route_risk_agent"}, any_=["risk"], all_=["pune"],
       pattern="AgentTool -> route_risk_agent -> check_route_disruptions"),
    _s("weather_city", "weather", "Any weather risk in Chennai in the next 24 hours?",
       {"route_risk_agent"}, any_=["risk"], all_=["chennai"],
       pattern="AgentTool -> route_risk_agent -> get_weather_risk (single city)"),
    _s("carrier_quote", "carrier quote", "Quote carriers Delhi → Bengaluru 800kg express",
       {"route_risk_agent"}, any_=["inr", "₹", "cr-0"],
       pattern="AgentTool -> route_risk_agent -> quote_carriers"),
    _s("multi_stop", "route optimisation",
       "Optimise a multi-stop route from the Bengaluru depot with stops Chennai, Coimbatore and Kochi",
       {"route_risk_agent"}, any_=["km"], all_=["chennai", "coimbatore", "kochi"],
       pattern="AgentTool -> route_risk_agent -> optimize_multi_stop_route (NN + 2-opt)"),
    # --- inventory (MCP) -----------------------------------------------------------------
    _s("inventory_low", "inventory", "Which SKUs are low on stock?",
       {"inventory_agent"}, any_=["sku-1"],
       pattern="AgentTool -> inventory_agent -> MCP check_inventory(low_stock_only)"),
    _s("reorder", "inventory", "Raise a purchase order to reorder SKU-114 at WH-HYD",
       {"inventory_agent"}, any_=["po-"], all_=["sku-114"],
       pattern="AgentTool -> inventory_agent -> MCP create_reorder"),
    # --- dispatch: planner <-> critic loop ---------------------------------------------------
    _s("dispatch_plan", "dispatch", "Plan today's dispatch from Mumbai",
       {"dispatch_pipeline"}, any_=["commit the plan", "assignment"], all_=["mumbai"],
       pattern="Sequential[Loop(planner <-> critic, exit_loop), reporter]"),
    # --- comms ---------------------------------------------------------------------------
    _s("comms_draft", "comms", "Draft a WhatsApp delay notice for SHP-1007",
       {"comms_agent"}, any_=["whatsapp", "message"], all_=["shp-1007"],
       pattern="AgentTool -> comms_agent -> draft_customer_notification"),
    # --- ops briefing: parallel fan-out ----------------------------------------------------
    _s("ops_briefing", "briefing", "Morning ops briefing",
       {"ops_briefing"}, any_=["risk", "stock", "kpi"],
       pattern="Sequential[Parallel(kpi, weather, stock watchers), writer]"),
    # --- feedback loop ---------------------------------------------------------------------
    _s("feedback", "feedback", "Remember: always prioritise critical pharma shipments over express ones",
       {"remember_operator_feedback"}, any_=["saved", "remember", "preference", "noted", "lesson"],
       pattern="concierge tool remember_operator_feedback -> lessons injected into instructions"),
    # --- multimodal proof-of-delivery ------------------------------------------------------------
    _s("vision_pod", "vision", "Inspect this delivery photo pod_damaged_SHP-1007.jpg for SHP-1007",
       {"vision_inspector"}, any_=["damage", "severity", "finding"], all_=["shp-1007"],
       image_ref="pod_damaged_SHP-1007.jpg",
       pattern="AgentTool -> vision_inspector -> inspect_delivery_photo (Gemini multimodal / Pillow)"),
    # --- multi-intent: one turn, two specialists --------------------------------------------
    _s("multi_intent", "multi-intent", "Where is SHP-1002 and is there weather risk in Delhi?",
       {"tracking_agent", "route_risk_agent"}, any_=["risk", "weather"], all_=["shp-1002", "delhi"],
       pattern="concierge fans out to 2 specialists in one turn"),
    # --- optional: disruption autopilot (only if the module exists) -------------------------
    _s("disruption_autopilot", "autopilot", "Cyclone alert in Chennai — run disruption autopilot",
       {"disruption_autopilot"}, any_=["chennai"],
       pattern="autonomous disruption workflow", requires="cargomind.autopilot"),
]

SAMPLE_IMAGES: dict[str, str] = {
    # image_ref in UPLOAD_DIR -> source path relative to repo root
    "pod_damaged_SHP-1007.jpg": "assets/samples/pod_damaged_SHP-1007.jpg",
}


def get_scenarios(only: list[str] | None = None) -> list[Scenario]:
    """Available scenarios (optional modules present), optionally filtered by id or category."""
    items = [s for s in SCENARIOS if s.available()]
    if only:
        wanted = {o.strip().lower() for o in only if o and o.strip()}
        items = [s for s in items if s.id.lower() in wanted or s.category.lower() in wanted]
    return items


def by_id(scenario_id: str) -> Scenario:
    for s in SCENARIOS:
        if s.id == scenario_id:
            return s
    raise KeyError(scenario_id)
