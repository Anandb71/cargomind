"""Watchtower: proactive background monitor that raises alerts before the operator asks.

Scans the network every N seconds for SLA breaches, delayed/exception shipments, low stock,
fleet problems and hub weather risk. Each alert carries a ``suggested_prompt`` the dashboard
turns into a one-click question for the concierge (or the Disruption Autopilot for weather).

    scan_once() -> {"new": [...], "active": [...]}
    latest_alerts() -> [...]
    run_watchtower(interval_s)   # long-running asyncio task (started by the web server)
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
from typing import Any

from cargomind.data import db
from cargomind.healing import journal
from cargomind.tools import weather

log = logging.getLogger(__name__)

HUBS = ["Mumbai", "Delhi", "Bengaluru", "Chennai", "Kolkata", "Hyderabad"]
_active: dict[str, dict[str, Any]] = {}


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def _parse(ts: str | None) -> _dt.datetime | None:
    if not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(ts)
    except ValueError:
        return None


def _alert(id_: str, kind: str, severity: str, title: str, detail: str, prompt: str) -> dict[str, Any]:
    return {"id": id_, "kind": kind, "severity": severity, "title": title, "detail": detail,
            "suggested_prompt": prompt, "raised_at": _now().isoformat(timespec="seconds")}


def _shipment_alerts() -> list[dict]:
    out = []
    for s in db.list_shipments(limit=500).get("shipments", []):
        if s.get("status") == "delivered":
            continue
        sid, prio = s["id"], s.get("priority", "standard")
        eta, due = _parse(s.get("eta")), _parse(s.get("promised_by"))
        late = eta is not None and due is not None and eta > due
        if s.get("status") == "exception":
            out.append(_alert(f"exc:{sid}", "exception", "critical", f"{sid} exception",
                              f"{s.get('customer')} · {s.get('origin_city')} → {s.get('destination_city')}",
                              f"Where is {sid} and what went wrong?"))
        elif late or s.get("status") == "delayed":
            hrs = round((eta - due).total_seconds() / 3600, 1) if late else None
            sev = "critical" if prio == "critical" else "warning"
            detail = f"{prio} · {s.get('customer')} · " + (f"ETA {hrs}h past promise" if hrs else "marked delayed")
            out.append(_alert(f"sla:{sid}", "sla_risk", sev, f"{sid} at risk of SLA breach", detail,
                              f"Where is {sid} and will it make its SLA? Draft a delay notice if not."))
    return out


def _stock_alerts() -> list[dict]:
    out = []
    for i in db.get_inventory(low_stock_only=True).get("items", []):
        if i.get("open_po"):
            continue
        sev = "critical" if (i.get("qty_on_hand") or 0) <= (i.get("reorder_point") or 0) * 0.25 else "warning"
        out.append(_alert(f"stock:{i['sku']}:{i['warehouse_id']}", "low_stock", sev,
                          f"{i['sku']} low at {i['warehouse_id']}",
                          f"{i.get('name')}: {i.get('qty_on_hand')} on hand vs reorder point {i.get('reorder_point')}",
                          f"Reorder {i['sku']} for {i['warehouse_id']}"))
    return out


def _fleet_alerts() -> list[dict]:
    out = []
    for v in db.list_vehicles().get("vehicles", []):
        fuel = v.get("fuel_pct")
        if fuel is not None and fuel < 20:
            out.append(_alert(f"fuel:{v['id']}", "fleet", "warning", f"{v['id']} fuel {v.get('fuel_pct')}%",
                              f"{v.get('driver_name')} in {v.get('current_city')}", f"Status of vehicle {v['id']}?"))
        if (v.get("driver_hours_remaining") if v.get("driver_hours_remaining") is not None else 99) < 1:
            out.append(_alert(f"hours:{v['id']}", "fleet", "warning", f"{v['id']} driver near hours limit",
                              f"{v.get('driver_name')}: {v.get('driver_hours_remaining')}h left",
                              f"Plan today's dispatch from {v.get('current_city')}"))
    return out


async def _weather_alerts() -> tuple[list[dict], set[str]]:
    """Return alerts and city alert IDs whose weather check failed."""
    async def one(city: str) -> tuple[dict | None, str | None]:
        try:
            r = await asyncio.wait_for(weather.get_weather_risk(city=city, hours_ahead=24), timeout=8)
            lvl = r.get("risk_level")
            if r.get("status") == "error" or lvl not in ("low", "medium", "high"):
                raise ValueError(r.get("error") or "Weather risk unavailable")
        except Exception as exc:  # noqa: BLE001 - weather down → retain the previous city alert until a healthy scan
            log.debug("watchtower weather %s failed: %s", city, exc)
            return None, f"wx:{city}"
        if lvl not in ("high", "medium"):
            return None, None
        return (_alert(f"wx:{city}", "weather", "critical" if lvl == "high" else "info",
                       f"{lvl.title()} weather risk at {city} hub", r.get("summary") or "",
                       f"Severe weather alert in {city}, severity {lvl} — run disruption autopilot"), None)

    results = await asyncio.gather(*(one(c) for c in HUBS))
    return [a for a, _ in results if a], {failed for _, failed in results if failed}


async def scan_once() -> dict[str, list[dict]]:
    """Run every check once; returns newly raised alerts and the full active set."""
    found: list[dict] = []
    failed_ids: set[str] = set()
    for check, prefixes in (
        (_shipment_alerts, ("exc:", "sla:")),
        (_stock_alerts, ("stock:",)),
        (_fleet_alerts, ("fuel:", "hours:")),
    ):
        try:
            found.extend(check())
        except Exception as exc:  # noqa: BLE001 - one broken check must not blind the others
            journal.record("fault", f"watchtower:{check.__name__}", str(exc), action="skipped check")
            failed_ids.update(i for i in _active if i.startswith(prefixes))
    weather_alerts, failed_weather_ids = await _weather_alerts()
    found.extend(weather_alerts)
    failed_ids.update(failed_weather_ids)
    current = {i: {**a, "stale": True} for i, a in _active.items() if i in failed_ids}
    current.update({a["id"]: a for a in found})
    new = [a for i, a in current.items() if i not in _active]
    for i, a in current.items():  # keep first-raised time for alerts that persist
        if i in _active:
            a["raised_at"] = _active[i]["raised_at"]
    _active.clear()
    _active.update(current)
    return {"new": new, "active": latest_alerts()}


def latest_alerts() -> list[dict]:
    rank = {"critical": 0, "warning": 1, "info": 2}
    return sorted(_active.values(), key=lambda a: (rank.get(a["severity"], 3), a["id"]))


async def run_watchtower(interval_s: float = 60) -> None:
    """Scan forever (cancel the task to stop)."""
    while True:
        try:
            r = await scan_once()
            if r["new"]:
                log.info("watchtower: %d new alert(s), %d active", len(r["new"]), len(r["active"]))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the monitor must outlive any single bad scan
            journal.record("fault", "watchtower", str(exc), action="retry next interval")
        await asyncio.sleep(interval_s)
