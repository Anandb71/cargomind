"""Carrier quoting, ETA estimation and the propose -> validate -> revise dispatch loop.

The dispatch tools are designed for an ADK ``LoopAgent``: a planner agent calls
`propose_dispatch_plan`, a critic agent calls `validate_dispatch_plan`, and the critic's issues
(``state["plan_issues"]``) make the next `propose_dispatch_plan` call *revise* the plan. The
first proposal is a fast greedy first-fit (it ignores weather, remaining shift hours, cold chain,
corridor and SLA feasibility); the revision is constraint-aware, so the loop normally converges
in two iterations.
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from google.adk.tools import ToolContext

from cargomind.tools import geo, maybe_fail, weather

IST = timezone(timedelta(hours=5, minutes=30))

PRIORITY_RANK = {"critical": 0, "express": 1, "standard": 2}
PRIORITY_COST_MULT = {"standard": 1.0, "express": 1.35, "critical": 1.7}
HANDLING_H = {"standard": 6.0, "express": 3.0, "critical": 1.5}
SCORE_WEIGHTS = {  # (cost, eta, reliability)
    "standard": (0.5, 0.2, 0.3),
    "express": (0.3, 0.4, 0.3),
    "critical": (0.15, 0.5, 0.35),
}
MAX_DRIVER_HOURS = 10.0  # legal on-duty limit per driver per day (matches cargomind.data.db)
GREEDY_HOURS_CUTOFF = 9.0  # first-fit planner only skips drivers at/over this
MIN_SHIFT_HOURS = 4.0  # a driver must have this much shift left (or the whole trip) to depart
TEAM_DAY_DRIVE_H = 18.0  # two drivers alternating
CORRIDOR_DETOUR_MAX = 1.25  # multi-drop route km / longest direct km
LOADING_H = 1.5
UNLOAD_H = 0.5
AIR_MIN_KM = 250.0
PENDING_STATUSES = ("created", "delayed")


# ----------------------------------------------------------------------------- helpers
def _db():
    from cargomind.data import db

    return db


def _now() -> datetime:
    return datetime.now(IST).replace(microsecond=0)


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=IST)


def _iso(ts: datetime) -> str:
    return ts.astimezone(IST).replace(microsecond=0).isoformat()


def _human_ts(ts: datetime) -> str:
    return ts.astimezone(IST).strftime("%a %d %b, %H:%M IST")


def _is_error(obj: Any) -> bool:
    return isinstance(obj, dict) and obj.get("status") == "error" and "error" in obj


def _norm_priority(priority: str | None) -> str | None:
    p = (priority or "standard").strip().lower()
    return p if p in PRIORITY_RANK else None


def calendar_hours(drive_h: float, team: bool = False, hours_today: float = 0.0) -> float:
    """Wall-clock hours to complete `drive_h` of driving under driver duty limits."""
    if drive_h <= 0:
        return 0.0
    day_cap = TEAM_DAY_DRIVE_H if team else MAX_DRIVER_HOURS
    first = day_cap if team else max(0.0, MAX_DRIVER_HOURS - (hours_today or 0.0))
    if drive_h <= first:
        return drive_h
    remaining = drive_h - first
    rests = math.ceil(remaining / day_cap)
    return drive_h + rests * (24.0 - day_cap)


def _coords(city: str) -> tuple[float, float] | None:
    hit = geo.city_coords(city)
    return hit[1] if hit else None


def _shipments_of(res: Any) -> list[dict]:
    return list(res.get("shipments", [])) if isinstance(res, dict) else list(res or [])


def _vehicles_of(res: Any) -> list[dict]:
    return list(res.get("vehicles", [])) if isinstance(res, dict) else list(res or [])


def _carriers_of(res: Any) -> list[dict]:
    return list(res.get("carriers", [])) if isinstance(res, dict) else list(res or [])


def _modes(c: dict) -> list[str]:
    m = c.get("modes") or []
    return [x.strip() for x in m.split(",")] if isinstance(m, str) else list(m)


# ----------------------------------------------------------------------------- quote_carriers
def _price(c: dict, km: float, gc_km: float, weight: float, prio: str) -> dict | None:
    modes = _modes(c)
    air_only = "air" in modes and "road" not in modes and "rail" not in modes
    dist = gc_km if air_only else km
    speed = float(c.get("avg_speed_kmph") or c.get("avg_speed_kmh") or 45.0)
    base = float(c.get("base_rate_per_km") or 0.0)
    per_kg = float(c.get("per_kg_rate") or 0.0)
    cost = (base * dist + per_kg * weight) * PRIORITY_COST_MULT[prio]
    transit_h = dist / speed + (6.0 if air_only else 0.0) + HANDLING_H[prio]
    return {
        "carrier_id": c.get("id"),
        "carrier_name": c.get("name"),
        "mode": "air" if air_only else ("road" if "road" in modes else (modes[0] if modes else "road")),
        "cost_inr": round(cost),
        "eta_hours": round(transit_h, 1),
        "reliability_pct": round(float(c.get("reliability") or 0.0) * (100 if float(c.get("reliability") or 0) <= 1 else 1), 1),
    }


async def quote_carriers(origin: str, destination: str, weight_kg: float, priority: str = "standard") -> dict:
    """Compares partner carriers for a consignment and returns the 3 best quotes with reasons.

    Use this when the user asks "who should carry this / how much will it cost / cheapest or
    fastest option", or when the own fleet cannot take a shipment.

    Args:
        origin: Pickup city, e.g. "Mumbai".
        destination: Delivery city, e.g. "Delhi".
        weight_kg: Consignment weight in kg (must be > 0).
        priority: "standard", "express" (x1.35 price) or "critical" (x1.7 price). Higher
            priorities weight speed and reliability more heavily in the ranking.

    Returns:
        dict with distance_km, route_source, quotes (top 3, each with carrier_id, carrier_name,
        mode, cost_inr, eta_hours, eta, reliability_pct, score 0-100, reasons[]), excluded
        carriers with the reason, and a one-line recommendation. Costs are in INR.
    """
    maybe_fail("tool:quote_carriers")
    prio = _norm_priority(priority)
    if prio is None:
        return {"status": "error", "error": f"Unknown priority {priority!r}.", "suggestions": list(PRIORITY_RANK)}
    try:
        weight = float(weight_kg)
    except (TypeError, ValueError):
        weight = -1.0
    if weight <= 0:
        return {"status": "error", "error": f"weight_kg must be a positive number, got {weight_kg!r}.", "suggestions": ["e.g. 250"]}

    route = await geo.route(origin, destination)
    if _is_error(route):
        return route
    km = float(route["distance_km"])
    o, d = route["origin"], route["destination"]
    gc_km = geo.haversine((o["lat"], o["lon"]), (d["lat"], d["lon"]))

    carriers = _carriers_of(_db().list_carriers())
    quotes, excluded = [], []
    for c in carriers:
        maxw = float(c.get("max_weight_kg") or 1e12)
        if weight > maxw:
            excluded.append({"carrier_id": c.get("id"), "carrier_name": c.get("name"), "reason": f"max load {maxw:.0f} kg"})
            continue
        q = _price(c, km, gc_km, weight, prio)
        if q["mode"] == "air" and km < AIR_MIN_KM:
            excluded.append({"carrier_id": c.get("id"), "carrier_name": c.get("name"), "reason": f"air not economical under {AIR_MIN_KM:.0f} km"})
            continue
        quotes.append(q)
    if not quotes:
        return {
            "status": "error",
            "error": f"No carrier can take {weight:.0f} kg {origin} -> {destination}.",
            "suggestions": ["Split the consignment", "Use the own fleet (propose_dispatch_plan)"],
            "excluded": excluded,
        }

    def norm(vals: list[float], v: float) -> float:
        lo, hi = min(vals), max(vals)
        return 0.0 if hi - lo < 1e-9 else (v - lo) / (hi - lo)

    costs = [q["cost_inr"] for q in quotes]
    etas = [q["eta_hours"] for q in quotes]
    rels = [q["reliability_pct"] for q in quotes]
    wc, we, wr = SCORE_WEIGHTS[prio]
    now = _now()
    for q in quotes:
        q["score"] = round(
            100 * (wc * (1 - norm(costs, q["cost_inr"])) + we * (1 - norm(etas, q["eta_hours"])) + wr * (norm(rels, q["reliability_pct"]) if len(set(rels)) > 1 else 1.0)),
            1,
        )
        q["eta"] = _iso(now + timedelta(hours=q["eta_hours"]))
        reasons = []
        if q["cost_inr"] == min(costs):
            reasons.append("cheapest option")
        if q["eta_hours"] == min(etas):
            reasons.append("fastest delivery")
        if q["reliability_pct"] == max(rels):
            reasons.append(f"most reliable ({q['reliability_pct']:.0f}% on-time)")
        avg_cost = sum(costs) / len(costs)
        delta = (q["cost_inr"] - avg_cost) / avg_cost * 100 if avg_cost else 0
        reasons.append(f"{abs(delta):.0f}% {'below' if delta < 0 else 'above'} average price")
        reasons.append(f"~{q['eta_hours']:.0f} h door-to-door by {q['mode']}")
        q["reasons"] = reasons
    quotes.sort(key=lambda q: (-q["score"], q["cost_inr"]))
    top = quotes[:3]
    best = top[0]
    return {
        "status": "ok",
        "origin": o["name"],
        "destination": d["name"],
        "distance_km": km,
        "route_source": route["source"],
        "route_degraded": route["degraded"],
        "weight_kg": weight,
        "priority": prio,
        "currency": "INR",
        "quotes": top,
        "excluded": excluded,
        "recommendation": (
            f"{best['carrier_name']} ({best['carrier_id']}) at INR {best['cost_inr']:,} with ~{best['eta_hours']:.0f} h "
            f"transit is the best balance for a {prio} load ({', '.join(best['reasons'][:2])})."
        ),
    }


# ----------------------------------------------------------------------------- estimate_eta
async def estimate_eta(shipment_id: str) -> dict:
    """Re-estimates a shipment's arrival time from its current position and checks the SLA.

    Use this when asked "when will SHP-1002 arrive / will it be late / is it on track". It
    combines the road route from the shipment's last known city, driver duty limits, handling
    time and a weather delay buffer, then compares with the promised delivery time.

    Args:
        shipment_id: Shipment ID, e.g. "SHP-1002".

    Returns:
        dict with eta (ISO) / eta_human, promised_by, slack_hours (negative = late), sla_risk
        (low | medium | high), breakdown (drive/rest/handling/weather hours), weather_risk,
        route_source, degraded and a recommendation. Delivered shipments return delivered=True.
    """
    maybe_fail("tool:estimate_eta")
    s = _db().get_shipment(shipment_id)
    if _is_error(s):
        return s
    sid = s["id"]
    status = s.get("status")
    if status == "delivered":
        return {
            "status": "ok",
            "shipment_id": sid,
            "delivered": True,
            "delivered_at": s.get("eta"),
            "promised_by": s.get("promised_by"),
            "message": f"{sid} is already delivered.",
        }
    current = s.get("last_city") or s.get("origin_city")
    dest = s.get("destination_city")
    prio = _norm_priority(s.get("priority")) or "standard"
    route, risk = await asyncio.gather(geo.route(current, dest), weather._route_risk(current, dest))
    if _is_error(route):
        return {**route, "shipment_id": sid}

    drive_h = route["truck_duration_min"] / 60
    team = prio == "critical"
    cal_h = calendar_hours(drive_h, team=team)
    handling = {"created": LOADING_H + 1.0, "picked_up": LOADING_H, "delayed": 2.0, "exception": 6.0}.get(status, 0.5)
    if status == "out_for_delivery":
        handling = 1.5
    risk_level = risk.get("overall_risk", "low") if not _is_error(risk) else "low"
    buffer_pct = weather.DELAY_BUFFER_PCT.get(risk_level, 0)
    weather_h = cal_h * buffer_pct / 100
    total_h = cal_h + handling + weather_h
    eta = _now() + timedelta(hours=total_h)
    promised = _parse_ts(s.get("promised_by"))
    slack = round((promised - eta).total_seconds() / 3600, 1) if promised else None
    if slack is None:
        sla_risk = "medium"
    elif slack < 0:
        sla_risk = "high"
    elif slack < 4 or risk_level == "high":
        sla_risk = "medium"
    else:
        sla_risk = "low"
    if sla_risk == "high":
        rec = (
            f"Expected {abs(slack):.1f} h late: notify the customer now"
            + ("; consider team drivers or an express carrier." if prio != "standard" else ".")
        )
    elif sla_risk == "medium":
        rec = "Tight against the promise: monitor closely and pre-alert the customer if conditions worsen."
    else:
        rec = "On track; no action needed."
    return {
        "status": "ok",
        "shipment_id": sid,
        "shipment_status": status,
        "priority": prio,
        "from_city": route["origin"]["name"],
        "destination": route["destination"]["name"],
        "remaining_km": route["distance_km"],
        "eta": _iso(eta),
        "eta_human": _human_ts(eta),
        "recorded_eta": s.get("eta"),
        "promised_by": s.get("promised_by"),
        "slack_hours": slack,
        "sla_risk": sla_risk,
        "breakdown": {
            "drive_hours": round(drive_h, 1),
            "rest_hours": round(cal_h - drive_h, 1),
            "team_drivers": team,
            "handling_hours": round(handling, 1),
            "weather_buffer_hours": round(weather_h, 1),
        },
        "weather_risk": risk_level,
        "weather_summary": risk.get("recommendation") if not _is_error(risk) else None,
        "route_source": route["source"],
        "degraded": bool(route["degraded"] or (not _is_error(risk) and risk.get("degraded"))),
        "recommendation": rec,
    }


# ----------------------------------------------------------------------------- dispatch: data
def _pending_jobs(city: str | None) -> list[dict]:
    db = _db()
    jobs: list[dict] = []
    for st in PENDING_STATUSES:
        for s in _shipments_of(db.list_shipments(status=st, limit=200)):
            if s.get("vehicle_id"):
                continue
            pickup = geo.city_coords(s.get("last_city") or s.get("origin_city"))
            dest = geo.city_coords(s.get("destination_city"))
            if not pickup or not dest:
                continue
            if city and pickup[0] != city:
                continue
            jobs.append(
                {
                    "shipment_id": s["id"],
                    "status": s.get("status"),
                    "priority": _norm_priority(s.get("priority")) or "standard",
                    "weight_kg": float(s.get("weight_kg") or 0.0),
                    "pickup_city": pickup[0],
                    "destination": dest[0],
                    "promised_by": s.get("promised_by"),
                    "contents": s.get("contents") or "",
                    "notes": s.get("notes") or "",
                    "customer": s.get("customer"),
                }
            )
    jobs.sort(key=lambda j: (PRIORITY_RANK[j["priority"]], j["promised_by"] or "9999", j["shipment_id"]))
    return jobs


def _fleet(city: str | None) -> list[dict]:
    out = []
    for v in _vehicles_of(_db().list_vehicles(status="available")):
        loc = geo.city_coords(v.get("current_city") or v.get("city"))
        if not loc or (city and loc[0] != city):
            continue
        cap = float(v.get("remaining_capacity_kg", v.get("capacity_kg")) or 0.0)
        out.append(
            {
                "vehicle_id": v["id"],
                "vehicle_type": v.get("type"),
                "driver": v.get("driver_name") or v.get("driver"),
                "city": loc[0],
                "capacity_kg": round(cap, 1),
                "driver_hours_today": float(v.get("driver_hours_today") or 0.0),
                "refrigerated": bool(v.get("refrigerated")),
                "fuel_pct": v.get("fuel_pct"),
            }
        )
    out.sort(key=lambda v: v["vehicle_id"])
    return out


def _needs_cold_chain(job: dict) -> bool:
    text = f"{job.get('contents', '')} {job.get('notes', '')}".lower()
    return "cold chain" in text or "2-8c" in text or "reefer" in text


def _is_hazmat(job: dict) -> bool:
    text = f"{job.get('contents', '')} {job.get('notes', '')}".lower()
    return "hazmat" in text or "un3480" in text or "dangerous goods" in text


def _vehicle_metrics(veh: dict, shipments: list[dict], team: bool) -> dict:
    """Stop order, trip hours, detour ratio and per-shipment ETA for one loaded vehicle."""
    origin = _coords(veh["city"])
    dests = list(dict.fromkeys(s["destination"] for s in shipments))
    pts = [origin, *(_coords(d) for d in dests)]
    order = geo._nn_two_opt(pts, ret=False) if len(dests) > 1 else [1] * len(dests)
    seq = [dests[i - 1] for i in order]
    cum_h, cum_km, arrive_h, prev = 0.0, 0.0, {}, origin
    for k, d in enumerate(seq):
        km, minutes = geo.estimate_road(prev, _coords(d))
        cum_km += km
        cum_h += minutes / 60
        arrive_h[d] = calendar_hours(cum_h, team, veh["driver_hours_today"]) + LOADING_H + UNLOAD_H * k
        prev = _coords(d)
    longest = max((geo.estimate_road(origin, _coords(d))[0] for d in dests), default=0.0)
    return {
        "stops": seq,
        "route_km": round(cum_km, 1),
        "trip_hours": round(cum_h, 1),
        "detour_ratio": round(cum_km / longest, 2) if longest > 0 else 1.0,
        "arrive_h": arrive_h,
    }


def _shift_ok(veh: dict, trip_hours: float) -> bool:
    left = MAX_DRIVER_HOURS - veh["driver_hours_today"]
    return left + 1e-9 >= min(trip_hours, MIN_SHIFT_HOURS)


def _late(job: dict, arrive_h: float, now: datetime) -> bool:
    promised = _parse_ts(job.get("promised_by"))
    return bool(promised and now + timedelta(hours=arrive_h) > promised)


def _pick_carrier(job: dict, carriers: list[dict]) -> dict | None:
    pa, pb = _coords(job["pickup_city"]), _coords(job["destination"])
    km, _ = geo.estimate_road(pa, pb)
    best, best_key = None, None
    for c in carriers:
        if job["weight_kg"] > float(c.get("max_weight_kg") or 1e12):
            continue
        q = _price(c, km, geo.haversine(pa, pb), job["weight_kg"], job["priority"])
        if q["mode"] == "air" and km < AIR_MIN_KM:
            continue
        key = (-q["reliability_pct"], q["eta_hours"]) if job["priority"] == "critical" else (-q["reliability_pct"], q["cost_inr"])
        if best_key is None or key < best_key:
            best, best_key = q, key
    return best


# ----------------------------------------------------------------------------- dispatch: planner
def _build_plan(jobs: list[dict], fleet: list[dict], risk: dict[str, str], careful: bool, carriers: list[dict]) -> dict:
    now = _now()
    loads: dict[str, list[dict]] = {v["vehicle_id"]: [] for v in fleet}
    by_id = {v["vehicle_id"]: v for v in fleet}
    unassigned: list[dict] = []
    handoffs: list[dict] = []
    notes: list[str] = []
    mitigations: dict[str, str] = {}

    def unassign(job: dict, code: str, reason: str) -> None:
        unassigned.append(
            {
                "shipment_id": job["shipment_id"],
                "priority": job["priority"],
                "pickup_city": job["pickup_city"],
                "destination": job["destination"],
                "weight_kg": job["weight_kg"],
                "reason_code": code,
                "reason": reason,
            }
        )

    for job in jobs:
        city_fleet = [v for v in fleet if v["city"] == job["pickup_city"]]
        used = lambda v: sum(s["weight_kg"] for s in loads[v["vehicle_id"]])
        if not careful:
            choice = next(
                (
                    v
                    for v in city_fleet
                    if v["driver_hours_today"] < GREEDY_HOURS_CUTOFF and v["capacity_kg"] - used(v) >= job["weight_kg"]
                ),
                None,
            )
            if choice:
                loads[choice["vehicle_id"]].append(job)
            else:
                unassign(job, "no_vehicle", f"No available vehicle in {job['pickup_city']} with {job['weight_kg']:.0f} kg free.")
            continue

        # --- constraint-aware revision ---------------------------------------------------
        if risk.get(job["destination"]) == "high":
            if job["priority"] == "critical":
                mitigations[job["shipment_id"]] = (
                    f"Dispatch under weather protocol for {job['destination']}: waterproof sheeting, "
                    "driver briefing, alternate-route check, customer pre-alert."
                )
            else:
                unassign(job, "weather_deferred", f"Deferred: high weather risk at {job['destination']}; re-plan in next wave.")
                continue
        best, best_key = None, None
        for v in city_fleet:
            if v["capacity_kg"] - used(v) < job["weight_kg"]:
                continue
            if _needs_cold_chain(job) and not v["refrigerated"]:
                continue
            current = loads[v["vehicle_id"]]
            before = _vehicle_metrics(v, current, team=False) if current else None
            after = _vehicle_metrics(v, [*current, job], team=False)
            if not _shift_ok(v, after["trip_hours"]) or after["detour_ratio"] > CORRIDOR_DETOUR_MAX:
                continue
            if before:  # do not make an already-loaded priority shipment late
                newly_late = [
                    s for s in current
                    if s["priority"] != "standard"
                    and _late(s, after["arrive_h"][s["destination"]], now)
                    and not _late(s, before["arrive_h"][s["destination"]], now)
                ]
                if newly_late:
                    continue
            same_dest = any(s["destination"] == job["destination"] for s in current)
            leftover = v["capacity_kg"] - used(v) - job["weight_kg"]
            key = (not same_dest, leftover, -(MAX_DRIVER_HOURS - v["driver_hours_today"]), v["vehicle_id"])
            if best_key is None or key < best_key:
                best, best_key = v, key
        if best:
            loads[best["vehicle_id"]].append(job)
        elif job["priority"] in ("critical", "express"):
            q = _pick_carrier(job, carriers)
            if q:
                handoffs.append(
                    {
                        "shipment_id": job["shipment_id"],
                        "priority": job["priority"],
                        "pickup_city": job["pickup_city"],
                        "destination": job["destination"],
                        "weight_kg": job["weight_kg"],
                        **q,
                        "reason": f"No own-fleet vehicle in {job['pickup_city']} can take it within duty/capacity limits.",
                    }
                )
            else:
                unassign(job, "no_capacity_anywhere", "No vehicle or carrier can take this load; escalate to operations.")
        else:
            unassign(job, "no_vehicle", f"No suitable vehicle in {job['pickup_city']}; queued for next wave.")

    assignments: list[dict] = []
    for vid, ships in loads.items():
        if not ships:
            continue
        v = by_id[vid]
        team = False
        m = _vehicle_metrics(v, ships, team=False)
        if careful and any(s["priority"] != "standard" and _late(s, m["arrive_h"][s["destination"]], now) for s in ships):
            team = True
            m = _vehicle_metrics(v, ships, team=True)
            notes.append(f"{vid}: team drivers rostered so priority loads meet their promised time.")
        load = sum(s["weight_kg"] for s in ships)
        rows = []
        for s in ships:
            eta = now + timedelta(hours=m["arrive_h"][s["destination"]])
            late = _late(s, m["arrive_h"][s["destination"]], now)
            row = {
                "shipment_id": s["shipment_id"],
                "priority": s["priority"],
                "status": s["status"],
                "weight_kg": s["weight_kg"],
                "destination": s["destination"],
                "promised_by": s["promised_by"],
                "planned_eta": _iso(eta),
                "sla_ok": not late,
                "weather_risk": risk.get(s["destination"], "low"),
                "cold_chain": _needs_cold_chain(s),
                "hazmat": _is_hazmat(s),
            }
            if s["shipment_id"] in mitigations:
                row["weather_mitigation"] = mitigations[s["shipment_id"]]
            if careful and late and s["priority"] != "standard" and s["status"] != "delayed":
                row["sla_escalated"] = True
                notes.append(
                    f"{s['shipment_id']}: cannot meet promise even with team drivers; escalate to customer success "
                    "and consider an air/express partner."
                )
            rows.append(row)
        assignments.append(
            {
                "vehicle_id": vid,
                "vehicle_type": v["vehicle_type"],
                "driver": v["driver"],
                "city": v["city"],
                "capacity_kg": v["capacity_kg"],
                "load_kg": round(load, 1),
                "utilisation_pct": round(100 * load / v["capacity_kg"], 1) if v["capacity_kg"] else None,
                "driver_hours_today": v["driver_hours_today"],
                "driver_hours_left": round(MAX_DRIVER_HOURS - v["driver_hours_today"], 1),
                "refrigerated": v["refrigerated"],
                "fuel_pct": v["fuel_pct"],
                "team_drivers": team,
                "stops": m["stops"],
                "route_km": m["route_km"],
                "trip_hours": m["trip_hours"],
                "detour_ratio": m["detour_ratio"],
                "shipments": rows,
            }
        )
    return {"assignments": assignments, "unassigned": unassigned, "carrier_handoffs": handoffs, "notes": notes}


async def _risk_map(cities: set[str]) -> dict[str, str]:
    names = sorted(cities)
    res = await asyncio.gather(*(weather.city_risk(c, 24) for c in names))
    return {c: (r.get("risk_level", "low") if not _is_error(r) else "low") for c, r in zip(names, res, strict=True)}


# ----------------------------------------------------------------------------- dispatch: critic
def _check_plan(plan: dict, live_status: dict[str, str]) -> list[dict]:
    issues: list[dict] = []

    def add(kind: str, severity: str, message: str, fix: str, **ids: str) -> None:
        issues.append({"id": f"ISS-{len(issues) + 1}", "type": kind, "severity": severity, **ids, "message": message, "suggested_fix": fix})

    seen: dict[str, str] = {}
    for a in plan.get("assignments", []):
        vid = a["vehicle_id"]
        if live_status and live_status.get(vid, "available") != "available":
            add("vehicle_unavailable", "blocker", f"{vid} is now {live_status.get(vid)}.", "Re-plan with another vehicle.", vehicle_id=vid)
        if a["load_kg"] > a["capacity_kg"] + 1e-6:
            add("capacity", "blocker", f"{vid} overloaded: {a['load_kg']:.0f} kg on {a['capacity_kg']:.0f} kg free.",
                "Move the lightest lower-priority shipment to another vehicle.", vehicle_id=vid)
        left = a["driver_hours_left"]
        need = min(a["trip_hours"], MIN_SHIFT_HOURS)
        if left + 1e-9 < need:
            add("driver_hours", "blocker",
                f"{vid} driver {a.get('driver') or ''} has {left:.1f} h of duty left but the {a['trip_hours']:.1f} h trip needs at least {need:.1f} h before a safe handover.",
                "Swap to a vehicle whose driver has more hours left, or defer to the next shift.", vehicle_id=vid)
        if len(a["stops"]) > 1 and a["detour_ratio"] > CORRIDOR_DETOUR_MAX:
            add("route_divergence", "blocker",
                f"{vid} serves diverging destinations {', '.join(a['stops'])} (route {a['detour_ratio']:.2f}x the longest direct leg).",
                "Split loads by corridor.", vehicle_id=vid)
        if a.get("fuel_pct") is not None and float(a["fuel_pct"]) < 25:
            add("fuel", "warning", f"{vid} fuel at {float(a['fuel_pct']):.0f}%.", "Refuel before departure.", vehicle_id=vid)
        if a["capacity_kg"] >= 5000 and (a.get("utilisation_pct") or 0) < 15:
            add("utilisation", "warning", f"{vid} ({a['vehicle_type']}) only {a['utilisation_pct']}% utilised.",
                "Consider a smaller vehicle or consolidating more loads.", vehicle_id=vid)
        for s in a["shipments"]:
            sid = s["shipment_id"]
            if sid in seen:
                add("duplicate", "blocker", f"{sid} is assigned to both {seen[sid]} and {vid}.", "Keep one assignment.", shipment_id=sid)
            seen[sid] = vid
            if s.get("cold_chain") and not a.get("refrigerated"):
                add("cold_chain", "blocker", f"{sid} needs cold chain but {vid} is not refrigerated.", "Use a reefer vehicle.", shipment_id=sid, vehicle_id=vid)
            if s.get("hazmat"):
                add("hazmat", "warning", f"{sid} is dangerous goods: confirm {vid} driver is DG-certified and placarded.",
                    "Verify certification before loading.", shipment_id=sid, vehicle_id=vid)
            wr = s.get("weather_risk", "low")
            if wr == "high" and not (s["priority"] == "critical" and s.get("weather_mitigation")):
                add("weather", "blocker", f"{sid} ({s['priority']}) goes to {s['destination']} where weather risk is HIGH.",
                    "Defer non-critical loads; dispatch critical ones only under weather protocol.", shipment_id=sid, vehicle_id=vid)
            elif wr == "medium":
                add("weather", "warning", f"{sid}: medium weather risk at {s['destination']}.", "Add a 10% ETA buffer.", shipment_id=sid)
            if not s.get("sla_ok", True):
                blocking = s["priority"] != "standard" and s.get("status") != "delayed" and not s.get("sla_escalated")
                add("sla", "blocker" if blocking else "warning",
                    f"{sid} ({s['priority']}) planned arrival {s['planned_eta']} is after the promise {s['promised_by']}.",
                    "Roster team drivers / pick a faster option, or escalate and notify the customer.", shipment_id=sid, vehicle_id=vid)
    for h in plan.get("carrier_handoffs", []):
        if h["shipment_id"] in seen:
            add("duplicate", "blocker", f"{h['shipment_id']} is both on {seen[h['shipment_id']]} and handed to {h['carrier_id']}.",
                "Keep one.", shipment_id=h["shipment_id"])
    for u in plan.get("unassigned", []):
        if u["reason_code"] == "no_vehicle" and u["priority"] == "critical":
            add("critical_unassigned", "blocker", f"Critical {u['shipment_id']} ({u['pickup_city']} -> {u['destination']}) has no transport.",
                "Hand it to the most reliable partner carrier.", shipment_id=u["shipment_id"])
        elif u["reason_code"] == "no_vehicle" and u["priority"] == "express":
            add("express_unassigned", "warning", f"Express {u['shipment_id']} has no own-fleet vehicle.",
                "Consider a partner carrier.", shipment_id=u["shipment_id"])
    return issues


def _score(issues: list[dict]) -> int:
    blockers = sum(1 for i in issues if i["severity"] == "blocker")
    warnings = len(issues) - blockers
    return max(0, min(100, 100 - 15 * blockers - 3 * warnings))


def _live_vehicle_status() -> dict[str, str]:
    try:
        return {v["id"]: v.get("status", "available") for v in _vehicles_of(_db().list_vehicles())}
    except Exception:  # noqa: BLE001 - the critic still works on the plan snapshot
        return {}


def _describe_fix(issue: dict, plan: dict) -> str:
    sid = issue.get("shipment_id")
    where = None
    if sid:
        for a in plan["assignments"]:
            for s in a["shipments"]:
                if s["shipment_id"] == sid:
                    where = f"on {a['vehicle_id']}" + (" with team drivers" if a["team_drivers"] else "")
                    if s.get("weather_mitigation"):
                        where += " under weather protocol"
                    if s.get("sla_escalated"):
                        where += " (SLA escalated)"
        for h in plan["carrier_handoffs"]:
            if h["shipment_id"] == sid:
                where = f"handed to {h['carrier_name']} ({h['carrier_id']})"
        for u in plan["unassigned"]:
            if u["shipment_id"] == sid:
                where = f"deferred ({u['reason_code']})"
    elif issue.get("vehicle_id"):
        vid = issue["vehicle_id"]
        a = next((a for a in plan["assignments"] if a["vehicle_id"] == vid), None)
        where = f"{vid} now carries {', '.join(s['shipment_id'] for s in a['shipments'])}" if a else f"{vid} released"
    return f"Fixed [{issue['type']}] {issue.get('shipment_id') or issue.get('vehicle_id') or ''}: {where or 'addressed by constraint-aware re-plan'}."


def _summary(plan: dict) -> str:
    n_ship = sum(len(a["shipments"]) for a in plan["assignments"])
    return (
        f"Revision {plan['revision']} ({plan['strategy']}): {n_ship} shipment(s) on {len(plan['assignments'])} vehicle(s), "
        f"{len(plan['carrier_handoffs'])} carrier hand-off(s), {len(plan['unassigned'])} unassigned."
    )


# ----------------------------------------------------------------------------- dispatch tools
async def propose_dispatch_plan(tool_context: ToolContext, city: str | None = None) -> dict:
    """Builds (or revises) a dispatch plan assigning pending shipments to available own-fleet vehicles.

    Use this as the PLANNER step. The first call makes a quick greedy plan. If the critic
    (`validate_dispatch_plan`) has stored issues in session state, calling this again REVISES the
    plan to fix them (weather deferrals, driver-hour swaps, team drivers, carrier hand-offs for
    priority loads, corridor splits). The plan is saved to state["current_plan"].

    Args:
        city: Optional pickup city to plan for (e.g. "Mumbai"); omit to plan the whole network.

    Returns:
        dict with plan_id, revision, strategy, assignments[] (vehicle, driver, stops, load,
        utilisation, trip hours, shipments with planned ETA / SLA / weather), carrier_handoffs[],
        unassigned[] (with reason), notes[] and summary. Next step: call validate_dispatch_plan.
    """
    maybe_fail("tool:propose_dispatch_plan")
    city_name = None
    if city:
        hit = geo.city_coords(city)
        if not hit:
            return {"status": "error", "error": f"Unknown city {city!r}.", "suggestions": geo._suggest(city)}
        city_name = hit[0]
    state = tool_context.state
    prev = state.get("current_plan") or None
    issues = state.get("plan_issues") or []
    revising = bool(issues and prev and prev.get("city") == city_name and prev.get("plan_status") != "committed")

    jobs = _pending_jobs(city_name)
    fleet = _fleet(city_name)
    carriers = _carriers_of(_db().list_carriers()) if revising else []
    risk = await _risk_map({j["destination"] for j in jobs}) if jobs else {}
    built = _build_plan(jobs, fleet, risk, careful=revising, carriers=carriers)

    now = _now()
    revision = int(prev.get("revision", 1)) + 1 if revising else 1
    plan: dict[str, Any] = {
        "plan_id": prev["plan_id"] if revising else f"PLAN-{(city_name or 'NET')[:3].upper()}-{now:%m%d%H%M%S}",
        "revision": revision,
        "plan_status": "proposed",
        "city": city_name,
        "created_at": _iso(now),
        "strategy": "constraint-aware re-plan (critic feedback applied)" if revising else "greedy first-fit",
        **built,
        "weather": risk,
        "pending_considered": len(jobs),
        "vehicles_available": len(fleet),
    }
    if revising:
        # Final safety net: anything the critic would still block is deferred with a note.
        for _ in range(3):
            blockers = [i for i in _check_plan(plan, {}) if i["severity"] == "blocker" and i.get("shipment_id")]
            if not blockers:
                break
            for b in blockers:
                _defer_from_plan(plan, b)
        plan["notes"] = [
            *(_describe_fix(i, plan) if isinstance(i, dict) else f"Considered critic note: {i}" for i in issues),
            *plan["notes"],
        ]
    elif not jobs:
        plan["notes"].append("No pending (created/delayed) shipments without a vehicle" + (f" in {city_name}." if city_name else "."))
    plan["summary"] = _summary(plan)
    state["current_plan"] = plan
    state["plan_revision"] = revision
    return {"status": "ok", **plan, "next_step": "Call validate_dispatch_plan to critique this plan."}


def _defer_from_plan(plan: dict, issue: dict) -> None:
    sid = issue["shipment_id"]
    for a in list(plan["assignments"]):
        for s in list(a["shipments"]):
            if s["shipment_id"] != sid:
                continue
            a["shipments"].remove(s)
            a["load_kg"] = round(a["load_kg"] - s["weight_kg"], 1)
            plan["unassigned"].append(
                {
                    "shipment_id": sid,
                    "priority": s["priority"],
                    "pickup_city": a["city"],
                    "destination": s["destination"],
                    "weight_kg": s["weight_kg"],
                    "reason_code": "deferred_by_critic",
                    "reason": issue["message"],
                }
            )
        if not a["shipments"]:
            plan["assignments"].remove(a)


async def validate_dispatch_plan(tool_context: ToolContext) -> dict:
    """Critiques the current dispatch plan (the CRITIC step of the plan/critique loop).

    Checks every assignment for vehicle capacity, driver duty hours, cold-chain and dangerous
    goods handling, weather risk at the destination, SLA (promised delivery time) for critical
    and express loads, diverging multi-drop routes, duplicate assignments and critical shipments
    left without transport. Issues are stored in state["plan_issues"] so the next
    `propose_dispatch_plan` call revises the plan.

    Returns:
        dict with valid (True when there are no blocker issues), issues[] (type, severity
        blocker|warning, shipment_id/vehicle_id, message, suggested_fix), score (0-100),
        revision and next_step. When valid, stop iterating (exit the loop) and offer
        commit_dispatch_plan.
    """
    maybe_fail("tool:validate_dispatch_plan")
    state = tool_context.state
    plan = state.get("current_plan")
    if not plan:
        return {
            "status": "error",
            "error": "No dispatch plan in session state.",
            "suggestions": ["Call propose_dispatch_plan first."],
        }
    issues = _check_plan(plan, _live_vehicle_status())
    blockers = [i for i in issues if i["severity"] == "blocker"]
    valid = not blockers
    score = _score(issues)
    state["plan_issues"] = blockers  # only blockers drive a revision
    state["plan_validation"] = {"plan_id": plan["plan_id"], "revision": plan["revision"], "valid": valid, "score": score}
    return {
        "status": "ok",
        "plan_id": plan["plan_id"],
        "revision": plan["revision"],
        "valid": valid,
        "score": score,
        "blockers": len(blockers),
        "warnings": len(issues) - len(blockers),
        "issues": issues,
        "next_step": (
            "Plan is valid: exit the loop and offer commit_dispatch_plan."
            if valid
            else "Call propose_dispatch_plan again to revise the plan using these issues."
        ),
    }


def commit_dispatch_plan(tool_context: ToolContext, force: bool = False) -> dict:
    """Applies the validated dispatch plan: assigns each planned shipment to its vehicle in the TMS.

    Use only after `validate_dispatch_plan` returned valid=True for the current revision and the
    operator agreed. Carrier hand-offs are returned as booking actions (not auto-booked).

    Args:
        force: Commit even if the current revision was not validated (only if the operator
            explicitly insists).

    Returns:
        dict with status (ok | partial | error), applied[] (shipment, vehicle, warnings),
        failed[] (shipment, vehicle, error), carrier_bookings_needed[] and plan_id/revision.
    """
    maybe_fail("tool:commit_dispatch_plan")
    state = tool_context.state
    plan = state.get("current_plan")
    if not plan:
        return {"status": "error", "error": "No dispatch plan to commit.", "suggestions": ["Call propose_dispatch_plan first."]}
    tag = f"{plan['plan_id']}#r{plan['revision']}"
    if state.get("committed_plan") == tag:
        return {"status": "ok", "already_committed": True, "plan_id": plan["plan_id"], "revision": plan["revision"]}
    validation = state.get("plan_validation") or {}
    validated = (
        validation.get("valid") and validation.get("plan_id") == plan["plan_id"] and validation.get("revision") == plan["revision"]
    )
    if not validated and not force:
        return {
            "status": "error",
            "error": f"Revision {plan['revision']} of {plan['plan_id']} has not passed validation.",
            "suggestions": ["Call validate_dispatch_plan", "Revise with propose_dispatch_plan"],
        }
    db = _db()
    applied, failed = [], []
    for a in plan.get("assignments", []):
        for s in a["shipments"]:
            res = db.assign_vehicle(s["shipment_id"], a["vehicle_id"])
            if _is_error(res):
                failed.append({"shipment_id": s["shipment_id"], "vehicle_id": a["vehicle_id"], "error": res.get("error")})
            else:
                applied.append({"shipment_id": s["shipment_id"], "vehicle_id": a["vehicle_id"], "warnings": res.get("warnings", [])})
    bookings = [
        {k: h.get(k) for k in ("shipment_id", "carrier_id", "carrier_name", "mode", "cost_inr", "eta_hours")}
        for h in plan.get("carrier_handoffs", [])
    ]
    plan = {**plan, "plan_status": "committed" if not failed else "partially_committed", "committed_at": _iso(_now())}
    state["current_plan"] = plan
    state["committed_plan"] = tag
    state["plan_issues"] = []
    return {
        "status": "ok" if not failed else "partial",
        "plan_id": plan["plan_id"],
        "revision": plan["revision"],
        "forced": bool(force and not validated),
        "applied": applied,
        "failed": failed,
        "carrier_bookings_needed": bookings,
        "message": f"Assigned {len(applied)} shipment(s); {len(failed)} failed; {len(bookings)} carrier booking(s) to place.",
    }
