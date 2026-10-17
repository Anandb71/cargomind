"""Disruption Autopilot: an autonomous multi-agent workflow for city-level disruptions.

    disruption_autopilot (SequentialAgent, exposed to the concierge as an AgentTool)
    ├ impact_assessor        assess_disruption_impact  → state["autopilot_impact"]
    ├ ParallelAgent mitigation_fanout
    │  ├ reroute_planner      propose_disruption_reroutes → state["autopilot_reroutes"]
    │  └ notice_drafter       draft_disruption_notices    → state["autopilot_notices"]
    └ autopilot_commander    writes the incident action plan (human approves before anything is sent)

Trigger: "Cyclone alert in Chennai, severity high — run disruption autopilot".
Every tool is deterministic over the logistics DB + weather/gazetteer, so the workflow also runs
offline (rule-engine LLM) and under chaos (tools call ``maybe_fail`` and the plugin retries).
"""
from __future__ import annotations

import asyncio
from typing import Any

from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent

from cargomind.data import db, gazetteer
from cargomind.healing.chaos import maybe_fail
from cargomind.healing.offline_llm import OfflinePolicy
from cargomind.healing.offline_llm import OfflineRule as R
from cargomind.healing.resilient_llm import build_model
from cargomind.tools import comms, weather

HUBS = ["Mumbai", "Delhi", "Bengaluru", "Chennai", "Kolkata", "Hyderabad", "Pune", "Ahmedabad"]
OPEN = ("created", "picked_up", "in_transit", "delayed", "out_for_delivery", "exception")
PRIORITY_RANK = {"critical": 0, "express": 1, "standard": 2}
EVENTS = ("cyclone", "flood", "strike", "bandh", "closure", "landslide", "heatwave", "fog", "storm", "earthquake", "fire")


def _norm_event(event: str | None) -> str:
    e = (event or "").strip().lower().replace(" ", "")
    return next((x for x in EVENTS if x in e), e or "disruption")


def _norm_severity(severity: str | None) -> str:
    s = (severity or "").strip().lower()
    if s in ("severe", "critical", "red", "extreme"):
        return "high"
    return s if s in ("low", "medium", "high") else "high"


def _city_or_error(city: str) -> tuple[str | None, dict | None]:
    resolved = gazetteer.resolve_city(city)
    if resolved:
        return resolved, None
    return None, {
        "status": "error",
        "error": f"Unknown city '{city}'.",
        "suggestions": HUBS,
    }


def _affected(city: str) -> list[dict[str, Any]]:
    rows = db.list_shipments(limit=500).get("shipments", [])
    out = []
    for s in rows:
        if s.get("status") not in OPEN:
            continue
        legs = {"origin": s.get("origin_city"), "destination": s.get("destination_city"), "current": s.get("last_city")}
        touch = [k for k, v in legs.items() if v == city]
        if touch:
            out.append({**s, "exposure": touch})
    out.sort(key=lambda s: (PRIORITY_RANK.get(s.get("priority"), 3), s.get("promised_by") or ""))
    return out


async def _alternate_hub(city: str, avoid: set[str] | None = None, max_checked: int = 3) -> dict[str, Any] | None:
    """Nearest real warehouse hub with confirmed low or medium weather risk.

    Returns None when no checked warehouse has a confirmed suitable weather risk.
    """
    avoid = avoid or set()
    hubs = {
        resolved
        for w in db.list_warehouses().get("warehouses", [])
        if (resolved := gazetteer.resolve_city(w.get("city") or ""))
        and resolved != city and resolved not in avoid
    }
    if not hubs:
        return None
    candidates = sorted(hubs, key=lambda h: gazetteer.haversine_km(city, h))
    skipped = []
    for hub in candidates[:max_checked]:
        try:
            result = await asyncio.wait_for(weather.get_weather_risk(city=hub, hours_ahead=48), timeout=8)
            risk = result.get("risk_level") if result.get("status") != "error" else "unknown"
        except Exception:  # noqa: BLE001 - try another warehouse when weather is unavailable
            risk = "unknown"
        if risk in ("low", "medium"):
            return {"hub": hub, "km": round(gazetteer.haversine_km(city, hub), 1), "weather_risk": risk, "skipped": skipped}
        skipped.append({"hub": hub, "weather_risk": risk})
    return None


async def assess_disruption_impact(city: str, event: str = "disruption", severity: str = "high") -> dict:
    """Assess the blast radius of a city-level disruption (cyclone, flood, strike, road closure...).

    Step 1 of the Disruption Autopilot. Finds every open shipment that starts, ends or is currently
    in the city, vehicles and warehouses in the city, low stock there, and the live weather risk.

    Args:
        city: Affected city, e.g. "Chennai".
        event: Kind of disruption: cyclone, flood, strike, closure, landslide, heatwave, fog...
        severity: low, medium or high.

    Returns:
        dict with counts, the affected shipments (most urgent first), vehicles, warehouses, weather.
    """
    maybe_fail("tool:assess_disruption_impact")
    resolved, err = _city_or_error(city)
    if err:
        return err
    ev, sev = _norm_event(event), _norm_severity(severity)
    ships = _affected(resolved)
    vehicles = [v for v in db.list_vehicles().get("vehicles", []) if v.get("current_city") == resolved]
    warehouses = [w for w in db.list_warehouses().get("warehouses", []) if w.get("city") == resolved]
    wh_ids = {w["id"] for w in warehouses}
    low = [i for i in db.get_inventory(low_stock_only=True).get("items", []) if i.get("warehouse_id") in wh_ids]
    try:
        wx = await weather.get_weather_risk(city=resolved, hours_ahead=48)
        wx = {k: wx.get(k) for k in ("risk_level", "summary", "source", "degraded")}
    except Exception as exc:  # noqa: BLE001 - weather is context, not a blocker
        wx = {"risk_level": "unknown", "summary": f"weather unavailable: {exc}", "degraded": True}
    critical = [s for s in ships if s.get("priority") == "critical"]
    return {
        "status": "ok",
        "city": resolved,
        "event": ev,
        "severity": sev,
        "weather": wx,
        "affected_shipments": len(ships),
        "critical_shipments": len(critical),
        "shipments": [
            {k: s.get(k) for k in ("id", "customer", "priority", "status", "origin_city", "destination_city", "last_city", "promised_by", "exposure")}
            for s in ships
        ],
        "vehicles_in_city": [{k: v.get(k) for k in ("id", "status", "type", "driver_name", "refrigerated")} for v in vehicles],
        "warehouses_in_city": [{k: w.get(k) for k in ("id", "name", "utilization_pct", "cold_chain")} for w in warehouses],
        "low_stock_in_city": [{k: i.get(k) for k in ("sku", "name", "warehouse_id", "qty_on_hand", "reorder_point")} for i in low],
        "headline": (
            f"{ev.title()} ({sev}) in {resolved}: {len(ships)} open shipments exposed "
            f"({len(critical)} critical), {len(vehicles)} vehicles and {len(warehouses)} warehouse(s) in the zone."
        ),
    }


async def propose_disruption_reroutes(city: str, event: str = "disruption", severity: str = "high") -> dict:
    """Propose a mitigation action for every shipment exposed to a city-level disruption.

    Step 2a of the Disruption Autopilot. Outbound loads are held (high severity) or departed early,
    inbound loads are diverted to the nearest hub that is not itself at high weather risk, loads stuck in the city are staged
    at the alternate hub, and vehicles in the zone are moved out. Nothing is applied: the operator
    approves the plan first.

    Args:
        city: Affected city, e.g. "Chennai".
        event: Kind of disruption (cyclone, flood, strike, closure...).
        severity: low, medium or high.

    Returns:
        dict with per-shipment actions, vehicle moves and the alternate hub used.
    """
    maybe_fail("tool:propose_disruption_reroutes")
    resolved, err = _city_or_error(city)
    if err:
        return err
    ev, sev = _norm_event(event), _norm_severity(severity)
    pick = await _alternate_hub(resolved)
    if pick is None:
        return {
            "status": "error", "city": resolved, "event": ev, "severity": sev,
            "error": f"No alternate warehouse hub with confirmed low or medium weather risk is available outside {resolved}.",
            "suggestions": ["Ask the operator to verify an alternate staging location before moving loads."],
            "shipment_actions": [], "vehicle_moves": [], "requires_approval": True,
        }
    alt, alt_km = pick["hub"], pick["km"]
    actions = []
    for s in _affected(resolved):
        exp = s["exposure"]
        if "origin" in exp and s.get("status") in ("created", "picked_up"):
            action = (f"HOLD at origin until the {ev} clears" if sev == "high" and s.get("priority") != "critical"
                      else f"Depart early via {alt} before the {ev} peaks")
        elif "current" in exp and "destination" not in exp:
            action = f"Stage at {alt} hub ({alt_km} km) until the {ev} clears, then resume to {s['destination_city']}"
        elif "destination" in exp:
            action = f"Divert to {alt} hub ({alt_km} km) and hold for last-mile once {resolved} reopens"
        else:
            action = f"Reroute around {resolved} via {alt}"
        if s.get("priority") == "critical":
            action += "; escalate to account manager, consider air/express carrier"
        actions.append({"shipment_id": s["id"], "priority": s.get("priority"), "customer": s.get("customer"),
                        "route": f"{s.get('origin_city')} → {s.get('destination_city')}", "action": action})
    moves = [
        {"vehicle_id": v["id"], "action": f"Move to {alt} staging yard" if v.get("status") != "maintenance" else "Secure in depot"}
        for v in db.list_vehicles().get("vehicles", []) if v.get("current_city") == resolved
    ]
    out = {"status": "ok", "city": resolved, "event": ev, "severity": sev, "alternate_hub": alt,
           "alternate_hub_km": alt_km, "alternate_hub_weather": pick["weather_risk"],
           "hubs_skipped_for_weather": pick["skipped"], "shipment_actions": actions, "vehicle_moves": moves,
           "requires_approval": True}
    if pick.get("warning"):
        out["warning"] = pick["warning"]
    return out


async def draft_disruption_notices(city: str, event: str = "disruption", channel: str = "email") -> dict:
    """Draft (do not send) proactive customer notices for critical/express shipments hit by a disruption.

    Step 2b of the Disruption Autopilot. Standard-priority customers are batched into a status-page
    update instead of individual messages.

    Args:
        city: Affected city, e.g. "Chennai".
        event: Kind of disruption (cyclone, flood, strike, closure...).
        channel: email, sms or whatsapp.

    Returns:
        dict with drafted notices (not sent) and the count of customers covered by the batch update.
    """
    maybe_fail("tool:draft_disruption_notices")
    resolved, err = _city_or_error(city)
    if err:
        return err
    ev = _norm_event(event)
    ships = _affected(resolved)
    urgent = [s for s in ships if s.get("priority") in ("critical", "express")][:6]
    drafts = []
    for s in urgent:
        d = comms.draft_customer_notification(shipment_id=s["id"], reason=f"{ev} in {resolved}", channel=channel)
        if d.get("status") == "error":
            continue
        drafts.append({"shipment_id": s["id"], "customer": s.get("customer"), "priority": s.get("priority"),
                       "channel": d.get("channel", channel), "subject": d.get("subject"),
                       "message": d.get("message") or d.get("body")})
    return {"status": "ok", "city": resolved, "event": ev, "drafts": drafts, "sent": False,
            "batched_status_update_customers": max(0, len(ships) - len(urgent)),
            "next_step": "Operator approves → comms_agent send_notification per draft."}


async def run_autopilot(city: str, event: str = "disruption", severity: str = "high") -> dict:
    """Run the three autopilot steps directly (no LLM) — used by the CLI and evals."""
    impact = await assess_disruption_impact(city=city, event=event, severity=severity)
    if impact.get("status") == "error":
        return impact
    reroutes = await propose_disruption_reroutes(city=city, event=event, severity=severity)
    notices = await draft_disruption_notices(city=city, event=event)
    result = {"status": "ok", "impact": impact, "reroutes": reroutes, "notices": notices}
    if reroutes.get("status") == "error":
        result.update(status="error", error=reroutes["error"], suggestions=reroutes.get("suggestions", []))
    return result


_AP = """You are part of CargoMind's Disruption Autopilot for an Indian 3PL network. Use tools for every
fact; pass the affected city, the event type and severity from the request. Be concise."""

impact_assessor = LlmAgent(
    name="impact_assessor",
    description="Assesses which shipments, vehicles and warehouses a disruption hits.",
    model=build_model("fast", OfflinePolicy(agent="impact_assessor", title="Disruption Impact", default_tool="assess_disruption_impact")),
    instruction=_AP + "\nCall assess_disruption_impact and summarise the blast radius in 3-5 bullets.",
    tools=[assess_disruption_impact],
    output_key="autopilot_impact",
)
reroute_planner = LlmAgent(
    name="reroute_planner",
    description="Plans reroutes/holds for exposed shipments and moves vehicles out of the zone.",
    model=build_model("pro", OfflinePolicy(agent="reroute_planner", title="Reroute Plan", default_tool="propose_disruption_reroutes")),
    instruction=_AP + "\nCall propose_disruption_reroutes and present the actions as a markdown table (critical first).",
    tools=[propose_disruption_reroutes],
    output_key="autopilot_reroutes",
)
notice_drafter = LlmAgent(
    name="notice_drafter",
    description="Drafts proactive customer notices for urgent shipments (never sends).",
    model=build_model("fast", OfflinePolicy(
        agent="notice_drafter", title="Customer Notices (drafts)",
        rules=[R(tool="draft_disruption_notices", keywords=[""])], default_tool="draft_disruption_notices",
    )),
    instruction=_AP + "\nCall draft_disruption_notices and list the drafts (customer, channel, first line). Do not send.",
    tools=[draft_disruption_notices],
    output_key="autopilot_notices",
)
autopilot_commander = LlmAgent(
    name="autopilot_commander",
    description="Writes the incident action plan for the operator.",
    model=build_model("fast", OfflinePolicy(
        agent="autopilot_commander", title="Disruption Autopilot - Incident Action Plan", echo_context=True,
        help_text=(
            "Review this plan and apply reroutes manually. To queue a notice in the simulated outbox, "
            "approve a specific shipment's draft through comms_agent."
        ),
    )),
    instruction="""You are the incident commander. Write the incident action plan from:
Impact: {autopilot_impact?}
Reroutes: {autopilot_reroutes?}
Customer notices: {autopilot_notices?}
Sections: Situation (1-2 lines), Exposure (numbers), Actions (numbered, owner + deadline, critical first),
Customer comms (drafts ready, not sent), Approval needed. Nothing has been executed yet.
Reroutes must be applied manually. An operator can approve a specific shipment notice through
comms_agent to queue it in the simulated outbox.""",
)

disruption_autopilot = SequentialAgent(
    name="disruption_autopilot",
    description=(
        "Autonomous disruption workflow for a city-level event (cyclone, flood, strike, road closure): "
        "impact assessment, then reroute plan and customer notice drafts in parallel, then an incident "
        "action plan for operator approval. Input: city, event and severity."
    ),
    sub_agents=[
        impact_assessor,
        ParallelAgent(name="mitigation_fanout", sub_agents=[reroute_planner, notice_drafter]),
        autopilot_commander,
    ],
)
