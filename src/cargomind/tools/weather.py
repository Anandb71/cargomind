"""Weather risk for dispatch decisions (Open-Meteo, no API key).

Falls back to a deterministic Indian seasonal heuristic (SW monsoon Jun-Sep on the west coast
and in the east, NE monsoon / cyclones Oct-Dec on the south-east coast, winter fog in the
north) when offline or when Open-Meteo is unreachable; such results carry ``degraded=True``.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any

from cargomind.tools import geo, http_json, maybe_fail

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Thresholds (per hour). IMD: >=7.6 mm/h is "heavy" rain, 2.5-7.6 "moderate".
RAIN_HIGH_MM = 7.6
RAIN_MED_MM = 2.5
RAIN_TOTAL_HIGH_MM = 50.0
RAIN_TOTAL_MED_MM = 15.0
WIND_HIGH_KMH = 50.0
WIND_MED_KMH = 35.0
THUNDER_CODES = {95, 96, 99}
FOG_CODES = {45, 48}
HEAVY_PRECIP_CODES = {65, 67, 75, 82, 86}
MODERATE_PRECIP_CODES = {57, 63, 66, 73, 81, 85}

LEVELS = ("low", "medium", "high")
DELAY_BUFFER_PCT = {"low": 0, "medium": 10, "high": 25}
_CACHE_TTL_S = 1800.0
_cache: dict[tuple, tuple[float, dict]] = {}

# Seasonal outlook per region, one letter per month Jan..Dec (L/M/H).
_SEASONAL: dict[str, tuple[str, dict[str, str]]] = {
    "west_coast": ("LLLLLHHHMLLL", {"monsoon": "southwest monsoon: heavy rain and waterlogging"}),
    "kerala": ("LLLLMHHMMMML", {"monsoon": "monsoon rain (SW Jun-Sep, NE Oct-Nov)"}),
    "ghats": ("LLLLLMHHMLLL", {"monsoon": "monsoon rain, landslide risk on ghat sections"}),
    "se_coast": ("LLLLLLLLLHHM", {"monsoon": "northeast monsoon and Bay of Bengal cyclone season"}),
    "east_coast": ("LLLLMMMMMHML", {"monsoon": "monsoon rain and Bay of Bengal cyclone season"}),
    "east": ("LLLMMHHHHMLL", {"pre": "Nor'wester thunderstorms", "monsoon": "southwest monsoon: heavy rain, flooding"}),
    "northeast": ("LLMMHHHHMLLL", {"pre": "pre-monsoon thunderstorms", "monsoon": "monsoon floods and landslides"}),
    "north": ("MLLLLLMMLLLM", {"winter": "dense winter fog, low visibility", "monsoon": "monsoon downpours"}),
    "northwest": ("LLLLLLMMLLLL", {"monsoon": "monsoon downpours"}),
    "deccan": ("LLLLLLMMMMLL", {"monsoon": "monsoon showers and thunderstorms"}),
    "deccan_south": ("LLLLLLLLLMML", {"monsoon": "northeast monsoon showers"}),
    "central": ("LLLLLMHMLLLL", {"monsoon": "monsoon rain"}),
}
_CITY_REGION = {
    "Mumbai": "west_coast", "Surat": "west_coast", "Kochi": "kerala", "Pune": "ghats",
    "Chennai": "se_coast", "Visakhapatnam": "east_coast", "Bhubaneswar": "east_coast",
    "Kolkata": "east", "Guwahati": "northeast", "Delhi": "north", "Gurugram": "north",
    "Noida": "north", "Lucknow": "north", "Jaipur": "northwest", "Ahmedabad": "northwest",
    "Bengaluru": "deccan", "Hyderabad": "deccan", "Coimbatore": "deccan_south",
    "Nagpur": "central", "Indore": "central",
}


def _reset_cache() -> None:
    _cache.clear()


def _current_month() -> int:
    """Month used by the seasonal heuristic (patched in tests)."""
    return dt.date.today().month


def _region_for(name: str, lat: float, lon: float) -> str:
    if name in _CITY_REGION:
        return _CITY_REGION[name]
    if lat < 15 and lon > 79:
        return "se_coast"
    if lon < 74.5 and lat < 22:
        return "west_coast"
    if lon > 89:
        return "northeast"
    if lon > 84:
        return "east"
    if lat > 25:
        return "north"
    return "deccan"


def seasonal_risk(name: str, lat: float, lon: float, month: int | None = None) -> dict[str, Any]:
    """Deterministic offline risk from Indian climatology for the given month."""
    month = month or _current_month()
    region = _region_for(name, lat, lon)
    pattern, reasons = _SEASONAL[region]
    level = {"L": "low", "M": "medium", "H": "high"}[pattern[month - 1]]
    if month in (12, 1, 2):
        season = "winter"
    elif month in (3, 4, 5):
        season = "pre"
    else:
        season = "monsoon"
    hazard = reasons.get(season) or reasons.get("monsoon", "seasonal weather")
    month_name = dt.date(2000, month, 1).strftime("%B")
    if level == "low":
        summary = f"Low weather risk expected in {name} for {month_name} (seasonal outlook, no live forecast)."
        hazards: list[str] = []
    else:
        summary = (
            f"{level.capitalize()} weather risk typical for {name} in {month_name}: {hazard} "
            "(seasonal outlook, no live forecast)."
        )
        hazards = [hazard]
    return {"risk_level": level, "summary": summary, "hazards": hazards, "region": region}


def _classify_hours(hourly: dict[str, list]) -> dict[str, Any]:
    times = hourly.get("time") or []
    precip = [float(v or 0) for v in hourly.get("precipitation") or []]
    wind = [float(v or 0) for v in hourly.get("wind_speed_10m") or []]
    codes = [int(v or 0) for v in hourly.get("weather_code") or []]
    n = min(len(times), len(precip), len(wind), len(codes))
    worst, peak_idx, hazards = 0, None, set()
    peak_score = -1.0
    for i in range(n):
        lvl = 0
        if precip[i] >= RAIN_HIGH_MM or codes[i] in HEAVY_PRECIP_CODES:
            lvl = 2
            hazards.add("heavy rain")
        elif precip[i] >= RAIN_MED_MM or codes[i] in MODERATE_PRECIP_CODES:
            lvl = max(lvl, 1)
            hazards.add("moderate rain")
        if codes[i] in THUNDER_CODES:
            lvl = 2
            hazards.add("thunderstorm")
        if codes[i] in FOG_CODES:
            lvl = max(lvl, 1)
            hazards.add("fog")
        if wind[i] >= WIND_HIGH_KMH:
            lvl = 2
            hazards.add("strong wind")
        elif wind[i] >= WIND_MED_KMH:
            lvl = max(lvl, 1)
            hazards.add("gusty wind")
        score = lvl * 1000 + precip[i] * 10 + wind[i]
        if score > peak_score:
            peak_score, peak_idx = score, i
        worst = max(worst, lvl)
    total = sum(precip[:n])
    if total >= RAIN_TOTAL_HIGH_MM:
        worst = 2
        hazards.add("heavy cumulative rain")
    elif total >= RAIN_TOTAL_MED_MM:
        worst = max(worst, 1)
        hazards.add("persistent rain")
    fog_hours = sum(1 for c in codes[:n] if c in FOG_CODES)
    if fog_hours >= 6:
        worst = 2
    return {
        "risk_level": LEVELS[worst],
        "peak_time": times[peak_idx] if peak_idx is not None and n else None,
        "max_precip_mm_h": round(max(precip[:n], default=0.0), 1),
        "total_precip_mm": round(total, 1),
        "max_wind_kmh": round(max(wind[:n], default=0.0), 1),
        "fog_hours": fog_hours,
        "hazards": sorted(hazards),
        "hours_sampled": n,
    }


async def risk_at(name: str, lat: float, lon: float, hours_ahead: int = 24) -> dict[str, Any]:
    """Internal, chaos-free weather risk for a coordinate (cached 30 min)."""
    hours = max(1, min(int(hours_ahead or 24), 168))
    key = (round(lat, 2), round(lon, 2), hours, _current_month())
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < _CACHE_TTL_S:
        return {**cached[1], "city": name}

    chain: list[str] = []
    result: dict[str, Any] | None = None
    skip = geo._skip_reason("open_meteo")
    if skip:
        chain.append(f"open_meteo: {skip}")
    else:
        try:
            data = await http_json(
                "GET",
                OPEN_METEO_URL,
                params={
                    "latitude": round(lat, 4),
                    "longitude": round(lon, 4),
                    "hourly": "precipitation,wind_speed_10m,weather_code",
                    "forecast_hours": hours,
                    "timezone": "auto",
                },
            )
            stats = _classify_hours(data.get("hourly") or {})
            if not stats["hours_sampled"]:
                raise ValueError("Open-Meteo returned no hourly data")
            chain.append("open_meteo: ok")
            lvl = stats["risk_level"]
            hz = ", ".join(stats["hazards"]) or "no significant hazards"
            summary = (
                f"{lvl.capitalize()} weather risk in {name} over the next {hours} h: {hz}; "
                f"peak rain {stats['max_precip_mm_h']} mm/h"
                + (f" around {stats['peak_time'].replace('T', ' ')}" if stats["peak_time"] and lvl != "low" else "")
                + f", max wind {stats['max_wind_kmh']} km/h."
            )
            result = {**stats, "summary": summary, "source": "open_meteo", "degraded": False}
        except Exception as exc:  # noqa: BLE001 - fall back to the seasonal heuristic
            geo._provider_failed("open_meteo", exc, chain, component="weather.open_meteo")
    if result is None:
        seasonal = seasonal_risk(name, lat, lon)
        chain.append("seasonal_heuristic: ok")
        result = {
            **seasonal,
            "peak_time": None,
            "source": "seasonal_heuristic",
            "degraded": True,
        }
    level = result["risk_level"]
    result.update(
        {
            "status": "ok",
            "city": name,
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "hours_ahead": hours,
            "delay_buffer_pct": DELAY_BUFFER_PCT[level],
            "provider_chain_tried": chain,
        }
    )
    _cache[key] = (time.monotonic(), result)
    return dict(result)


async def city_risk(city: str, hours_ahead: int = 24) -> dict[str, Any]:
    """Internal, chaos-free risk lookup by place name (used by planning)."""
    chain: list[str] = []
    loc = await geo._locate(city, chain)
    if loc is None:
        return {
            "status": "error",
            "error": f"Unknown place '{city}'.",
            "suggestions": geo._suggest(city),
        }
    return await risk_at(loc["name"], loc["lat"], loc["lon"], hours_ahead)


async def get_weather_risk(city: str, hours_ahead: int = 24) -> dict:
    """Weather risk (rain, thunderstorms, wind, fog) for a city over the coming hours.

    Use this before dispatching to, or promising an ETA for, a city, and whenever the user asks
    about weather. For a whole origin->destination trip use `check_route_disruptions`.

    Args:
        city: City or place, e.g. "Chennai", "Bombay", "WH-BLR".
        hours_ahead: Forecast window in hours (1-168, default 24).

    Returns:
        dict with risk_level (low | medium | high), summary sentence, peak_time (local time of
        the worst hour), max_precip_mm_h, total_precip_mm, max_wind_kmh, hazards[],
        delay_buffer_pct (suggested ETA padding: 0/10/25), source (open_meteo |
        seasonal_heuristic) and degraded (True when no live forecast was available).
    """
    maybe_fail("tool:get_weather_risk")
    return await city_risk(city, hours_ahead)


def _nearest_city(lat: float, lon: float) -> tuple[str, tuple[float, float]]:
    from cargomind.data import gazetteer as gz

    return min(gz.CITIES.items(), key=lambda kv: geo.haversine((lat, lon), kv[1]))


async def check_route_disruptions(origin: str, destination: str) -> dict:
    """Weather disruption check along a trip: origin, en-route midpoint and destination.

    Use this before dispatching a long-haul shipment or when explaining a delay. It samples the
    origin, the nearest major city to the geographic midpoint, and the destination.

    Args:
        origin: Start city, e.g. "Mumbai".
        destination: End city, e.g. "Chennai".

    Returns:
        dict with overall_risk (low | medium | high), checkpoints[] (role, city, risk_level,
        summary, peak_time), recommendation, delay_buffer_pct (suggested ETA padding), sources
        and degraded (True if any checkpoint used the seasonal heuristic).
    """
    maybe_fail("tool:check_route_disruptions")
    return await _route_risk(origin, destination)


async def _route_risk(origin: str, destination: str) -> dict:
    """Internal, chaos-free implementation of `check_route_disruptions`."""
    chain: list[str] = []
    o, d = await asyncio.gather(geo._locate(origin, chain), geo._locate(destination, chain))
    missing = [p for p, loc in ((origin, o), (destination, d)) if loc is None]
    if missing:
        return {
            "status": "error",
            "error": f"Could not locate: {', '.join(repr(m) for m in missing)}.",
            "suggestions": geo._suggest(missing[0]),
        }
    points: list[tuple[str, str, float, float]] = [("origin", o["name"], o["lat"], o["lon"])]
    mid_name, (mlat, mlon) = _nearest_city((o["lat"] + d["lat"]) / 2, (o["lon"] + d["lon"]) / 2)
    if mid_name not in (o["name"], d["name"]):
        points.append(("en_route", mid_name, mlat, mlon))
    if d["name"] != o["name"]:
        points.append(("destination", d["name"], d["lat"], d["lon"]))

    risks = await asyncio.gather(*(risk_at(name, lat, lon, 36) for _, name, lat, lon in points))
    checkpoints = [
        {
            "role": role,
            "city": name,
            "risk_level": r["risk_level"],
            "summary": r["summary"],
            "peak_time": r.get("peak_time"),
            "source": r["source"],
        }
        for (role, name, _, _), r in zip(points, risks, strict=True)
    ]
    overall = max((r["risk_level"] for r in risks), key=LEVELS.index)
    worst = [c for c in checkpoints if c["risk_level"] == overall]
    where = ", ".join(c["city"] for c in worst)
    if overall == "high":
        rec = (
            f"High risk at {where}: consider holding non-critical loads, re-routing, or dispatching "
            "with weather protocol (waterproofing, driver briefing) and a 25% ETA buffer; pre-alert customers."
        )
    elif overall == "medium":
        rec = f"Medium risk at {where}: proceed with a 10% ETA buffer and monitor conditions."
    else:
        rec = "Low risk along the route: proceed as planned."
    return {
        "status": "ok",
        "origin": o["name"],
        "destination": d["name"],
        "overall_risk": overall,
        "checkpoints": checkpoints,
        "recommendation": rec,
        "delay_buffer_pct": DELAY_BUFFER_PCT[overall],
        "sources": sorted({r["source"] for r in risks}),
        "degraded": any(r["degraded"] for r in risks),
    }
