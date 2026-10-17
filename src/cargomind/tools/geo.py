"""Geocoding and road routing with a self-healing provider chain.

Chain (each hop that fails is written to the healing journal and the next one is tried):

1. Google Maps Platform (Routes API v2 / Geocoding API) when ``GOOGLE_MAPS_API_KEY`` is set;
2. public OpenStreetMap services (OSRM routing, Nominatim geocoding);
3. offline estimate from the built-in gazetteer: great-circle distance x 1.25 road factor at a
   45 km/h truck average.

Every result carries ``source``, ``degraded`` and ``provider_chain_tried`` so agents (and the
dashboard) can say how much to trust the numbers.
"""
from __future__ import annotations

import asyncio
import difflib
import itertools
import math
import time
from typing import Any

import httpx

from cargomind import config
from cargomind.tools import http_json, journal, maybe_fail, offline, short_error

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
OSRM_URL = "https://router.project-osrm.org"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

ROUTES_FIELD_MASK = (
    "routes.distanceMeters,routes.duration,routes.polyline.encodedPolyline,"
    "routes.optimizedIntermediateWaypointIndex,routes.legs.distanceMeters,routes.legs.duration"
)

ROAD_FACTOR = 1.25  # road km per great-circle km (Indian highway network)
AVG_TRUCK_KMH = 45.0  # door-to-door average for a loaded truck incl. stops
TRUCK_FACTOR = 1.3  # car-profile provider duration -> loaded-truck duration
MAX_STOPS = 20

_PROVIDER_COOLDOWN_S = 60.0
_AUTH_COOLDOWN_S = 3600.0
_CACHE_TTL_S = 600.0
_GEOCODE_TTL_S = 86_400.0

_down_until: dict[str, float] = {}
_route_cache: dict[tuple, tuple[float, dict]] = {}
_geocode_cache: dict[str, tuple[float, dict]] = {}


def _reset_caches() -> None:
    """Clears result caches and provider cool-downs (used by tests)."""
    _down_until.clear()
    _route_cache.clear()
    _geocode_cache.clear()


# ----------------------------------------------------------------------------- helpers
def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in km between two (lat, lon) pairs."""
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(min(1.0, h)))


def estimate_road(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
    """Offline road estimate between two points -> (km, minutes) at truck speed."""
    km = haversine(a, b) * ROAD_FACTOR
    return km, km / AVG_TRUCK_KMH * 60.0


def city_coords(name: str | None) -> tuple[str, tuple[float, float]] | None:
    """Resolves a city/alias via the gazetteer -> (canonical name, (lat, lon)) or None."""
    from cargomind.data import gazetteer as gz

    city = gz.resolve_city(name) if name else None
    return (city, gz.CITIES[city]) if city else None


def fmt_duration(minutes: float) -> str:
    """Human duration, e.g. 1895 -> '1 d 7 h 35 min'."""
    m = round(max(0.0, minutes))
    d, rem = divmod(m, 1440)
    h, mins = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d} d")
    if h:
        parts.append(f"{h} h")
    if mins or not parts:
        parts.append(f"{mins} min")
    return " ".join(parts)


def encode_polyline(points: list[tuple[float, float]]) -> str:
    """Encodes (lat, lon) points with Google's polyline algorithm (precision 5)."""
    out: list[str] = []
    prev_lat = prev_lon = 0
    for lat, lon in points:
        ilat, ilon = round(lat * 1e5), round(lon * 1e5)
        for delta in (ilat - prev_lat, ilon - prev_lon):
            v = ~(delta << 1) if delta < 0 else delta << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)


def _suggest(place: str) -> list[str]:
    from cargomind.data import gazetteer as gz

    names = list(gz.CITIES)
    close = difflib.get_close_matches((place or "").strip().title(), names, n=3, cutoff=0.5)
    return close or names[:6]


def _skip_reason(provider: str, needs_key: bool = False) -> str | None:
    if offline():
        return "skipped (offline mode)"
    if needs_key and not config.GOOGLE_MAPS_API_KEY:
        return "skipped (no GOOGLE_MAPS_API_KEY)"
    until = _down_until.get(provider, 0.0)
    if until > time.monotonic():
        return f"skipped (cooling down {int(until - time.monotonic())}s after failure)"
    return None


def _provider_failed(provider: str, exc: BaseException, chain: list[str], component: str | None = None) -> None:
    """Records a failed provider hop (chain + journal) and cools it down if transient."""
    err = short_error(exc)
    chain.append(f"{provider}: failed ({err})")
    transient = isinstance(exc, (httpx.TransportError, asyncio.TimeoutError)) or (
        isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 500, 502, 503, 504)
    )
    misconfigured = isinstance(exc, PermissionError) or (
        isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403)
    )
    if misconfigured:  # key/API not enabled: no point retrying for a while
        _down_until[provider] = time.monotonic() + _AUTH_COOLDOWN_S
    elif transient:
        _down_until[provider] = time.monotonic() + _PROVIDER_COOLDOWN_S
    journal("fallback", component or f"geo.{provider}", err, action="next provider")


def _degraded(chain: list[str], source: str) -> bool:
    return source.endswith("estimate") or any(": failed" in c for c in chain)


def _loc_out(loc: dict) -> dict:
    return {k: loc[k] for k in ("query", "name", "lat", "lon", "precision") if k in loc}


# ----------------------------------------------------------------------------- geocoding
async def _google_geocode(place: str) -> dict | None:
    data = await http_json(
        "GET", GEOCODE_URL, params={"address": place, "region": "in", "key": config.GOOGLE_MAPS_API_KEY}
    )
    status = data.get("status")
    if status == "ZERO_RESULTS":
        return None
    if status in ("REQUEST_DENIED", "OVER_DAILY_LIMIT"):
        raise PermissionError(f"Geocoding status {status}")
    if status != "OK":
        raise ValueError(f"Geocoding status {status}")
    top = data["results"][0]
    loc = top["geometry"]["location"]
    return {
        "name": top.get("formatted_address", place),
        "lat": float(loc["lat"]),
        "lon": float(loc["lng"]),
        "precision": top.get("geometry", {}).get("location_type", "unknown").lower(),
    }


async def _nominatim(place: str) -> dict | None:
    data = await http_json(
        "GET",
        NOMINATIM_URL,
        params={"q": place, "format": "jsonv2", "limit": 1, "countrycodes": "in"},
        headers={"User-Agent": "CargoMind/0.1 hackathon"},
    )
    if not data:
        return None
    top = data[0]
    return {
        "name": top.get("display_name", place),
        "lat": float(top["lat"]),
        "lon": float(top["lon"]),
        "precision": top.get("addresstype") or top.get("type") or "unknown",
    }


async def _geocode_online(place: str, chain: list[str]) -> dict | None:
    """Google Geocoding -> Nominatim. Returns a location dict (with 'source') or None."""
    for provider, fn, needs_key in (
        ("google_geocoding", _google_geocode, True),
        ("nominatim", _nominatim, False),
    ):
        skip = _skip_reason(provider, needs_key)
        if skip:
            chain.append(f"{provider}: {skip}")
            continue
        try:
            hit = await fn(place)
        except Exception as exc:  # noqa: BLE001 - any provider failure -> next provider
            _provider_failed(provider, exc, chain)
            continue
        if hit is None:
            chain.append(f"{provider}: no match")
            continue
        chain.append(f"{provider}: ok")
        return {"query": place, **hit, "source": provider}
    return None


async def _locate(place: str, chain: list[str]) -> dict | None:
    """Gazetteer city -> online geocoders -> city mentioned in text. None if nothing found."""
    from cargomind.data import gazetteer as gz

    q = (place or "").strip()
    if not q:
        return None
    hit = city_coords(q)
    if hit:
        return {"query": q, "name": hit[0], "lat": hit[1][0], "lon": hit[1][1], "precision": "city", "source": "gazetteer"}
    online = await _geocode_online(q, chain)
    if online:
        return online
    found = gz.find_cities_in_text(q)
    if found:
        lat, lon = gz.CITIES[found[0]]
        chain.append("gazetteer_text_match: ok")
        return {"query": q, "name": found[0], "lat": lat, "lon": lon, "precision": "city (approximate)", "source": "gazetteer"}
    return None


async def geocode(place: str) -> dict:
    """Converts a place name or address into coordinates.

    Use this when you need latitude/longitude for a city, warehouse address or landmark (for
    example to place it on the map). For distances or travel times use `get_route` instead.

    Args:
        place: Free-text place, e.g. "Bhiwandi warehouse, Mumbai", "Bangalore", "WH-DEL".

    Returns:
        dict with status "ok", name, lat, lon, precision, source (google_geocoding | nominatim |
        gazetteer), degraded (True when only an offline city-centre estimate was available) and
        provider_chain_tried. On failure: status "error" with suggestions of known cities.
    """
    maybe_fail("tool:geocode")
    q = (place or "").strip()
    if not q:
        return {"status": "error", "error": "Empty place name.", "suggestions": _suggest("")}
    cached = _geocode_cache.get(q.lower())
    if cached and time.monotonic() - cached[0] < _GEOCODE_TTL_S:
        return {**cached[1], "cached": True}

    chain: list[str] = []
    hit = await _geocode_online(q, chain)
    if hit is None:
        loc = await _locate_offline(q, chain)
        hit = loc
    if hit is None:
        return {
            "status": "error",
            "error": f"Could not locate '{q}'.",
            "suggestions": _suggest(q),
            "provider_chain_tried": chain,
        }
    source = hit.pop("source")
    result = {
        "status": "ok",
        **_loc_out(hit),
        "source": source,
        "degraded": source == "gazetteer" or _degraded(chain, source),
        "provider_chain_tried": chain,
    }
    if source == "gazetteer":
        result["note"] = "Offline gazetteer: city-centre coordinates only."
    _geocode_cache[q.lower()] = (time.monotonic(), result)
    return result


async def _locate_offline(q: str, chain: list[str]) -> dict | None:
    from cargomind.data import gazetteer as gz

    hit = city_coords(q)
    name = hit[0] if hit else next(iter(gz.find_cities_in_text(q)), None)
    if not name:
        chain.append("gazetteer: no match")
        return None
    chain.append("gazetteer: ok")
    lat, lon = gz.CITIES[name]
    return {"query": q, "name": name, "lat": lat, "lon": lon, "precision": "city", "source": "gazetteer"}


# ----------------------------------------------------------------------------- routing providers
def _waypoint(loc: dict) -> dict:
    return {"location": {"latLng": {"latitude": loc["lat"], "longitude": loc["lon"]}}}


def _seconds(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value).rstrip("s") or 0)


async def _google_routes(
    origin: dict, destination: dict, intermediates: list[dict], avoid_tolls: bool, optimize: bool
) -> dict:
    body: dict[str, Any] = {
        "origin": _waypoint(origin),
        "destination": _waypoint(destination),
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "routeModifiers": {"avoidTolls": bool(avoid_tolls)},
        "units": "METRIC",
        "regionCode": "IN",
    }
    if intermediates:
        body["intermediates"] = [_waypoint(s) for s in intermediates]
        body["optimizeWaypointOrder"] = bool(optimize and len(intermediates) > 1)
    data = await http_json(
        "POST",
        ROUTES_URL,
        json_body=body,
        headers={
            "X-Goog-Api-Key": config.GOOGLE_MAPS_API_KEY or "",
            "X-Goog-FieldMask": ROUTES_FIELD_MASK,
            "Content-Type": "application/json",
        },
    )
    routes = data.get("routes") or []
    if not routes:
        raise ValueError("Routes API returned no route")
    r = routes[0]
    order = r.get("optimizedIntermediateWaypointIndex")
    if not order or any(i < 0 for i in order):
        order = list(range(len(intermediates)))
    return {
        "distance_m": float(r.get("distanceMeters", 0)),
        "duration_s": _seconds(r.get("duration", 0)),
        "polyline": (r.get("polyline") or {}).get("encodedPolyline"),
        "order": order,
        "legs": [
            {"distance_m": float(leg.get("distanceMeters", 0)), "duration_s": _seconds(leg.get("duration", 0))}
            for leg in r.get("legs") or []
        ],
    }


def _osrm_coords(points: list[dict]) -> str:
    return ";".join(f"{p['lon']:.6f},{p['lat']:.6f}" for p in points)


async def _osrm_route(origin: dict, destination: dict) -> dict:
    data = await http_json(
        "GET", f"{OSRM_URL}/route/v1/driving/{_osrm_coords([origin, destination])}", params={"overview": "simplified"}
    )
    if data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError(f"OSRM code {data.get('code')}")
    r = data["routes"][0]
    return {"distance_m": float(r["distance"]), "duration_s": float(r["duration"]), "polyline": r.get("geometry")}


async def _osrm_trip(depot: dict, stops: list[dict], return_to_depot: bool) -> dict:
    """OSRM /trip. Open trips must end at a fixed stop, so the last input stop is the end."""
    params = {"overview": "simplified", "source": "first"}
    if return_to_depot:
        params.update(roundtrip="true", destination="any")
    else:
        params.update(roundtrip="false", destination="last")
    data = await http_json("GET", f"{OSRM_URL}/trip/v1/driving/{_osrm_coords([depot, *stops])}", params=params)
    if data.get("code") != "Ok" or not data.get("trips"):
        raise ValueError(f"OSRM code {data.get('code')}")
    trip = data["trips"][0]
    positions = [wp["waypoint_index"] for wp in data["waypoints"]]
    order = sorted(range(len(stops)), key=lambda i: positions[i + 1])
    return {
        "distance_m": float(trip["distance"]),
        "duration_s": float(trip["duration"]),
        "polyline": trip.get("geometry"),
        "order": order,
        "legs": [{"distance_m": float(leg["distance"]), "duration_s": float(leg["duration"])} for leg in trip.get("legs", [])],
    }


# ----------------------------------------------------------------------------- get_route
async def route(origin: str, destination: str, avoid_tolls: bool = False) -> dict:
    """Internal (chaos-free) implementation of `get_route`, reused by planning/weather."""
    geo_chain: list[str] = []
    o, d = await asyncio.gather(_locate(origin, geo_chain), _locate(destination, geo_chain))
    missing = [p for p, loc in ((origin, o), (destination, d)) if loc is None]
    if missing:
        return {
            "status": "error",
            "error": f"Could not locate: {', '.join(repr(m) for m in missing)}.",
            "suggestions": _suggest(missing[0]),
            "geocoding_chain": geo_chain,
        }
    key = (round(o["lat"], 4), round(o["lon"], 4), round(d["lat"], 4), round(d["lon"], 4), bool(avoid_tolls))
    cached = _route_cache.get(key)
    if cached and time.monotonic() - cached[0] < _CACHE_TTL_S:
        res = dict(cached[1])
        res["origin"], res["destination"] = _loc_out(o), _loc_out(d)
        return {**res, "cached": True}

    chain: list[str] = []
    raw: dict | None = None
    source = ""
    if haversine((o["lat"], o["lon"]), (d["lat"], d["lon"])) < 0.05:
        raw, source = {"distance_m": 0.0, "duration_s": 0.0, "polyline": None}, "same_location"
    if raw is None:
        skip = _skip_reason("google_routes", needs_key=True)
        if skip:
            chain.append(f"google_routes: {skip}")
        else:
            try:
                raw = await _google_routes(o, d, [], avoid_tolls, optimize=False)
                source = "google_routes"
                chain.append("google_routes: ok")
            except Exception as exc:  # noqa: BLE001
                _provider_failed("google_routes", exc, chain)
    if raw is None:
        skip = _skip_reason("osrm")
        if skip:
            chain.append(f"osrm: {skip}")
        else:
            try:
                raw = await _osrm_route(o, d)
                source = "osrm"
                chain.append("osrm: ok")
            except Exception as exc:  # noqa: BLE001
                _provider_failed("osrm", exc, chain)
    if raw is None:
        km, minutes = estimate_road((o["lat"], o["lon"]), (d["lat"], d["lon"]))
        raw = {
            "distance_m": km * 1000,
            "duration_s": minutes * 60,
            "polyline": encode_polyline([(o["lat"], o["lon"]), (d["lat"], d["lon"])]),
        }
        source = "haversine_estimate"
        chain.append("haversine_estimate: ok")

    minutes = raw["duration_s"] / 60
    is_estimate = source == "haversine_estimate"
    truck_min = minutes if is_estimate else minutes * TRUCK_FACTOR
    result: dict[str, Any] = {
        "status": "ok",
        "origin": _loc_out(o),
        "destination": _loc_out(d),
        "distance_km": round(raw["distance_m"] / 1000, 1),
        "duration_min": round(minutes),
        "duration_human": fmt_duration(minutes),
        "truck_duration_min": round(truck_min),
        "truck_duration_human": fmt_duration(truck_min),
        "avoid_tolls": bool(avoid_tolls),
        "traffic_aware": source == "google_routes",
        "source": source,
        "degraded": _degraded(chain, source) or "approximate" in o.get("precision", "") + d.get("precision", ""),
        "provider_chain_tried": chain,
    }
    if raw.get("polyline"):
        result["polyline"] = raw["polyline"]
    if is_estimate:
        result["note"] = (
            f"Offline estimate: great-circle distance x {ROAD_FACTOR} road factor at "
            f"{AVG_TRUCK_KMH:.0f} km/h average; no traffic or road closures considered."
        )
        result["polyline_is_straight_line"] = True
    elif source == "osrm" and avoid_tolls:
        result["note"] = "OSRM fallback cannot avoid tolls; route may include toll roads."
    if geo_chain:
        result["geocoding_chain"] = geo_chain
    if source not in ("same_location",):
        _route_cache[key] = (time.monotonic(), result)
    return result


async def get_route(origin: str, destination: str, avoid_tolls: bool = False) -> dict:
    """Road distance and drive time between two places (traffic-aware when Google is available).

    Use this for "how far / how long from X to Y" questions, before quoting carriers, or to
    draw a route on the map. For several delivery stops use `optimize_multi_stop_route`.

    Args:
        origin: Start city or address, e.g. "Mumbai", "Bhiwandi, Maharashtra", "WH-BOM".
        destination: End city or address, e.g. "Delhi".
        avoid_tolls: Prefer toll-free roads (honoured by Google Routes only).

    Returns:
        dict with distance_km, duration_min / duration_human (car-profile drive time),
        truck_duration_min / truck_duration_human (loaded-truck estimate), origin/destination
        coordinates, optional encoded polyline, source (google_routes | osrm |
        haversine_estimate), degraded (True when a fallback or offline estimate was used) and
        provider_chain_tried. On unknown places: status "error" with suggestions.
    """
    maybe_fail("tool:get_route")
    return await route(origin, destination, avoid_tolls)


# ----------------------------------------------------------------------------- multi-stop
def _tour_km(order: list[int], dist: list[list[float]], ret: bool) -> float:
    seq = [0, *order, *([0] if ret else [])]
    return sum(dist[seq[i]][seq[i + 1]] for i in range(len(seq) - 1))


def _nn_two_opt(points: list[tuple[float, float]], ret: bool) -> list[int]:
    """Nearest-neighbour tour from points[0] improved with 2-opt. Returns stop indices (1-based)."""
    n = len(points)
    dist = [[haversine(points[i], points[j]) for j in range(n)] for i in range(n)]
    unvisited, cur, order = set(range(1, n)), 0, []
    while unvisited:
        nxt = min(unvisited, key=lambda j: (dist[cur][j], j))
        order.append(nxt)
        unvisited.remove(nxt)
        cur = nxt
    improved = True
    while improved:
        improved = False
        for i in range(len(order) - 1):
            for k in range(i + 1, len(order)):
                cand = order[:i] + order[i : k + 1][::-1] + order[k + 1 :]
                if _tour_km(cand, dist, ret) < _tour_km(order, dist, ret) - 1e-6:
                    order, improved = cand, True
    return order


async def optimize_multi_stop_route(
    depot: str, stops: list[str], return_to_depot: bool = True, avoid_tolls: bool = False
) -> dict:
    """Finds the best visiting order for a multi-drop delivery run starting at a depot.

    Use this when one vehicle must serve several cities/addresses (milk-run, consolidation
    trip) and you need the stop sequence, total distance and drive time.

    Args:
        depot: Start point (warehouse city or address), e.g. "Pune" or "WH-BOM".
        stops: Delivery stops to visit, e.g. ["Nashik", "Surat", "Ahmedabad"] (max 20).
        return_to_depot: True if the vehicle returns to the depot at the end (round trip).
        avoid_tolls: Prefer toll-free roads (Google Routes only).

    Returns:
        dict with optimized_order (stop names in visit order), optimized_stop_indices (indices
        into the input `stops`), waypoints (name/lat/lon in visit order, for the map), legs,
        total_distance_km, total_duration_min, duration_human, truck_duration_human, optional
        polyline, unresolved_stops, source (google_routes | osrm_trip | heuristic_estimate),
        degraded and provider_chain_tried.
    """
    maybe_fail("tool:optimize_multi_stop_route")
    if not stops:
        return {"status": "error", "error": "No stops given.", "suggestions": ["Pass at least one stop."]}
    geo_chain: list[str] = []
    depot_loc = await _locate(depot, geo_chain)
    if depot_loc is None:
        return {"status": "error", "error": f"Could not locate depot '{depot}'.", "suggestions": _suggest(depot)}
    raw_locs = await asyncio.gather(*(_locate(s, geo_chain) for s in list(stops)[:MAX_STOPS]))
    resolved: list[tuple[int, dict]] = []
    unresolved: list[str] = []
    seen: set[tuple[float, float]] = set()
    for idx, (query, loc) in enumerate(zip(stops, raw_locs, strict=False)):
        if loc is None:
            unresolved.append(query)
            continue
        key = (round(loc["lat"], 4), round(loc["lon"], 4))
        if key in seen:
            continue
        seen.add(key)
        resolved.append((idx, loc))
    if len(stops) > MAX_STOPS:
        unresolved.extend(stops[MAX_STOPS:])
    if not resolved:
        return {
            "status": "error",
            "error": "None of the stops could be located.",
            "suggestions": _suggest(stops[0]),
            "unresolved_stops": unresolved,
        }

    locs = [loc for _, loc in resolved]
    dpt = (depot_loc["lat"], depot_loc["lon"])
    # Open trips need a fixed end for Google/OSRM: use the stop farthest from the depot.
    if not return_to_depot and len(locs) > 1:
        far = max(range(len(locs)), key=lambda i: haversine(dpt, (locs[i]["lat"], locs[i]["lon"])))
        locs.append(locs.pop(far))
        resolved.append(resolved.pop(far))

    chain: list[str] = []
    raw: dict | None = None
    source = ""
    skip = _skip_reason("google_routes", needs_key=True)
    if skip:
        chain.append(f"google_routes: {skip}")
    else:
        try:
            if return_to_depot:
                g = await _google_routes(depot_loc, depot_loc, locs, avoid_tolls, optimize=True)
                order = g["order"]
            else:
                g = await _google_routes(depot_loc, locs[-1], locs[:-1], avoid_tolls, optimize=True)
                order = [*g["order"], len(locs) - 1]
            raw, source = {**g, "order": order}, "google_routes"
            chain.append("google_routes: ok")
        except Exception as exc:  # noqa: BLE001
            _provider_failed("google_routes", exc, chain)
    if raw is None:
        skip = _skip_reason("osrm_trip")
        if skip:
            chain.append(f"osrm_trip: {skip}")
        else:
            try:
                raw, source = await _osrm_trip(depot_loc, locs, return_to_depot), "osrm_trip"
                chain.append("osrm_trip: ok")
            except Exception as exc:  # noqa: BLE001
                _provider_failed("osrm_trip", exc, chain)
    if raw is None:
        pts = [dpt, *((loc["lat"], loc["lon"]) for loc in locs)]
        order = [i - 1 for i in _nn_two_opt(pts, return_to_depot)]
        seq = [dpt, *(pts[i + 1] for i in order), *([dpt] if return_to_depot else [])]
        legs = []
        for a, b in itertools.pairwise(seq):
            km, mins = estimate_road(a, b)
            legs.append({"distance_m": km * 1000, "duration_s": mins * 60})
        raw = {
            "distance_m": sum(leg["distance_m"] for leg in legs),
            "duration_s": sum(leg["duration_s"] for leg in legs),
            "polyline": encode_polyline(seq),
            "order": order,
            "legs": legs,
        }
        source = "heuristic_estimate"
        chain.append("heuristic_estimate: ok (nearest-neighbour + 2-opt)")

    order = raw["order"]
    visit = [locs[i] for i in order]
    names = [depot_loc["name"], *(v["name"] for v in visit), *([depot_loc["name"]] if return_to_depot else [])]
    legs_out = [
        {
            "from": names[i],
            "to": names[i + 1],
            "distance_km": round(leg["distance_m"] / 1000, 1),
            "duration_min": round(leg["duration_s"] / 60),
        }
        for i, leg in enumerate(raw.get("legs") or [])
        if i + 1 < len(names)
    ]
    minutes = raw["duration_s"] / 60
    truck_min = minutes if source == "heuristic_estimate" else minutes * TRUCK_FACTOR
    result: dict[str, Any] = {
        "status": "ok",
        "depot": _loc_out(depot_loc),
        "optimized_order": [v["name"] for v in visit],
        "optimized_stop_indices": [resolved[i][0] for i in order],
        "waypoints": [
            {"name": p["name"], "lat": p["lat"], "lon": p["lon"]}
            for p in [depot_loc, *visit, *([depot_loc] if return_to_depot else [])]
        ],
        "legs": legs_out,
        "total_distance_km": round(raw["distance_m"] / 1000, 1),
        "total_duration_min": round(minutes),
        "duration_human": fmt_duration(minutes),
        "truck_duration_human": fmt_duration(truck_min),
        "return_to_depot": bool(return_to_depot),
        "unresolved_stops": unresolved,
        "source": source,
        "degraded": _degraded(chain, source) or bool(unresolved),
        "provider_chain_tried": chain,
    }
    if raw.get("polyline"):
        result["polyline"] = raw["polyline"]
    if source == "heuristic_estimate":
        result["note"] = "Offline estimate: stop order by nearest-neighbour + 2-opt on straight-line distances."
    if geo_chain:
        result["geocoding_chain"] = geo_chain
    return result
