"""Tests for cargomind.tools (geo, weather, planning, vision, comms).

All tests are network-free: CARGOMIND_OFFLINE=1 and httpx is patched to fail, so the
fallback chains (Google -> OSM -> offline estimate) are exercised deterministically.
"""
from __future__ import annotations

import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="cm_tools_")
if "cargomind.config" not in sys.modules:  # must happen before cargomind is imported
    os.environ["CARGOMIND_OFFLINE"] = "1"
    os.environ["CARGOMIND_CHAOS"] = "0"
    os.environ["CARGOMIND_DATA_DIR"] = _TMP
    os.environ["CARGOMIND_DB"] = os.path.join(_TMP, "tools_test.db")

from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from cargomind import config
from cargomind import tools as T
from cargomind.tools import comms, geo, planning, vision, weather


class FakeToolContext:
    def __init__(self) -> None:
        self.state: dict = {}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch, tmp_path):
    async def _fail(self, request, **kwargs):
        raise httpx.ConnectError("network disabled in tests", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", _fail)
    monkeypatch.setattr(config, "FORCE_OFFLINE", True)
    monkeypatch.setattr(config, "GOOGLE_MAPS_API_KEY", None)
    upload = tmp_path / "uploads"
    upload.mkdir()
    monkeypatch.setattr(config, "UPLOAD_DIR", upload)
    monkeypatch.setattr(weather, "_current_month", lambda: 10)  # NE monsoon: Chennai/Vizag high
    geo._reset_caches()
    weather._reset_cache()
    yield
    geo._reset_caches()
    weather._reset_cache()


@pytest.fixture
def journal_events(monkeypatch):
    events: list[dict] = []

    def fake_record(kind, component, detail, action="", outcome=""):
        ev = {"kind": kind, "component": component, "detail": detail, "action": action, "outcome": outcome}
        events.append(ev)
        return ev

    from cargomind.healing import journal

    monkeypatch.setattr(journal, "record", fake_record)
    return events


@pytest.fixture
def online_but_broken(monkeypatch):
    """Network providers enabled (with a Maps key) but every HTTP call fails."""
    monkeypatch.setattr(config, "FORCE_OFFLINE", False)
    monkeypatch.setattr(config, "GOOGLE_MAPS_API_KEY", "test-key")


# ------------------------------------------------------------------------------ geo
@pytest.mark.asyncio
async def test_route_offline_uses_haversine_estimate():
    r = await T.get_route("Bombay", "Bangalore")
    assert r["status"] == "ok"
    assert r["source"] == "haversine_estimate" and r["degraded"] is True
    assert r["origin"]["name"] == "Mumbai" and r["destination"]["name"] == "Bengaluru"
    gc = geo.haversine((r["origin"]["lat"], r["origin"]["lon"]), (r["destination"]["lat"], r["destination"]["lon"]))
    assert r["distance_km"] == pytest.approx(gc * geo.ROAD_FACTOR, rel=0.01)
    assert r["duration_min"] == pytest.approx(r["distance_km"] / geo.AVG_TRUCK_KMH * 60, abs=2)
    assert r["polyline"] and r["polyline_is_straight_line"]
    assert any("skipped (offline mode)" in c for c in r["provider_chain_tried"])


@pytest.mark.asyncio
async def test_route_fallback_chain_records_each_failed_hop(online_but_broken, journal_events):
    r = await T.get_route("Mumbai", "Delhi")
    assert r["source"] == "haversine_estimate" and r["degraded"] is True
    chain = r["provider_chain_tried"]
    assert chain[0].startswith("google_routes: failed")
    assert chain[1].startswith("osrm: failed")
    assert chain[-1] == "haversine_estimate: ok"
    comps = [e["component"] for e in journal_events if e["kind"] == "fallback"]
    assert comps == ["geo.google_routes", "geo.osrm"]
    assert all(e["action"] == "next provider" for e in journal_events)
    # transient failures put providers on cool-down: the next call skips them instantly
    r2 = await T.get_route("Pune", "Hyderabad")
    assert any("cooling down" in c for c in r2["provider_chain_tried"])
    assert len(journal_events) == 2


@pytest.mark.asyncio
async def test_route_unknown_place_returns_error_with_suggestions():
    r = await T.get_route("Atlantis", "Delhi")
    assert r["status"] == "error" and r["suggestions"]


@pytest.mark.asyncio
async def test_geocode_offline_gazetteer():
    r = await T.geocode("Madras")
    assert r["status"] == "ok" and r["name"] == "Chennai"
    assert r["source"] == "gazetteer" and r["degraded"] is True
    bad = await T.geocode("Xyzzyville")
    assert bad["status"] == "error" and bad["suggestions"]


@pytest.mark.asyncio
async def test_multi_stop_heuristic_order():
    r = await T.optimize_multi_stop_route("Mumbai", ["Ahmedabad", "Pune", "Surat", "Atlantis"])
    assert r["status"] == "ok" and r["source"] == "heuristic_estimate" and r["degraded"]
    assert r["unresolved_stops"] == ["Atlantis"]
    assert sorted(r["optimized_stop_indices"]) == [0, 1, 2]
    assert sorted(r["optimized_order"]) == ["Ahmedabad", "Pune", "Surat"]
    assert len(r["waypoints"]) == 5 and r["waypoints"][0]["name"] == r["waypoints"][-1]["name"] == "Mumbai"
    assert r["total_distance_km"] == pytest.approx(sum(leg["distance_km"] for leg in r["legs"]), abs=1)
    # Surat sits between Mumbai and Ahmedabad: a sensible tour visits them adjacently
    order = r["optimized_order"]
    assert abs(order.index("Surat") - order.index("Ahmedabad")) == 1


def test_polyline_encoding_matches_reference():
    pts = [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
    assert geo.encode_polyline(pts) == "_p~iF~ps|U_ulLnnqC_mqNvxq`@"


def test_chaos_hook_is_called(monkeypatch):
    from cargomind.healing import chaos

    chaos.force_fail("tool:get_route", 1)
    import asyncio

    with pytest.raises(chaos.ChaosError):
        asyncio.run(T.get_route("Mumbai", "Pune"))
    chaos.force_fail("tool:get_route", 0)


# ------------------------------------------------------------------------------ weather
@pytest.mark.asyncio
async def test_weather_offline_seasonal_heuristic(monkeypatch):
    r = await T.get_weather_risk("Chennai")
    assert r["risk_level"] == "high" and r["degraded"] is True
    assert r["source"] == "seasonal_heuristic" and r["delay_buffer_pct"] == 25
    assert "October" in r["summary"]
    assert (await T.get_weather_risk("Jaipur"))["risk_level"] == "low"
    monkeypatch.setattr(weather, "_current_month", lambda: 7)
    assert (await T.get_weather_risk("Mumbai"))["risk_level"] == "high"
    assert (await T.get_weather_risk("Chennai"))["risk_level"] == "low"
    monkeypatch.setattr(weather, "_current_month", lambda: 1)
    delhi = await T.get_weather_risk("New Delhi")
    assert delhi["risk_level"] == "medium" and "fog" in delhi["summary"]


@pytest.mark.asyncio
async def test_weather_online_failure_falls_back(online_but_broken, journal_events):
    r = await T.get_weather_risk("Kolkata", hours_ahead=12)
    assert r["source"] == "seasonal_heuristic" and r["degraded"] is True
    assert r["provider_chain_tried"][0].startswith("open_meteo: failed")
    assert any(e["component"] == "weather.open_meteo" for e in journal_events)


def test_classify_hours_thresholds():
    calm = weather._classify_hours(
        {"time": ["t0", "t1"], "precipitation": [0.0, 0.4], "wind_speed_10m": [8, 12], "weather_code": [1, 61]}
    )
    assert calm["risk_level"] == "low"
    storm = weather._classify_hours(
        {"time": ["t0", "t1", "t2"], "precipitation": [0.5, 9.0, 3.0], "wind_speed_10m": [10, 42, 20], "weather_code": [3, 95, 63]}
    )
    assert storm["risk_level"] == "high" and storm["peak_time"] == "t1"
    assert "thunderstorm" in storm["hazards"] and storm["max_precip_mm_h"] == 9.0
    fog = weather._classify_hours(
        {"time": ["t0"], "precipitation": [0.0], "wind_speed_10m": [5], "weather_code": [45]}
    )
    assert fog["risk_level"] == "medium" and "fog" in fog["hazards"]


@pytest.mark.asyncio
async def test_route_disruptions_samples_three_points():
    r = await T.check_route_disruptions("Delhi", "Chennai")
    roles = [c["role"] for c in r["checkpoints"]]
    assert roles == ["origin", "en_route", "destination"]
    assert r["overall_risk"] == "high" and r["delay_buffer_pct"] == 25 and r["degraded"]
    assert "Chennai" in r["recommendation"]


# ------------------------------------------------------------------------------ planning
@pytest.mark.asyncio
async def test_quote_carriers_ranking_and_priority_pricing():
    std = await T.quote_carriers("Mumbai", "Delhi", 500, "standard")
    crit = await T.quote_carriers("Mumbai", "Delhi", 500, "critical")
    assert std["status"] == crit["status"] == "ok"
    assert 1 <= len(std["quotes"]) <= 3
    scores = [q["score"] for q in std["quotes"]]
    assert scores == sorted(scores, reverse=True)
    assert all(q["reasons"] for q in std["quotes"])
    by_id = {q["carrier_id"]: q for q in crit["quotes"]}
    for q in std["quotes"]:
        if q["carrier_id"] in by_id:
            assert by_id[q["carrier_id"]]["cost_inr"] == pytest.approx(q["cost_inr"] * 1.7, rel=0.01)
    # critical weights speed: its winner is at least as fast as the standard winner
    assert crit["quotes"][0]["eta_hours"] <= std["quotes"][0]["eta_hours"]


@pytest.mark.asyncio
async def test_quote_carriers_exclusions_and_validation():
    short = await T.quote_carriers("Mumbai", "Pune", 100)
    assert any("air not economical" in e["reason"] for e in short["excluded"])
    assert all(q["mode"] != "air" for q in short["quotes"])
    heavy = await T.quote_carriers("Mumbai", "Delhi", 25000)
    assert heavy["status"] == "ok" and all(q["carrier_id"] == "CR-04" for q in heavy["quotes"])
    assert (await T.quote_carriers("Mumbai", "Delhi", 100, "urgent!!"))["status"] == "error"
    assert (await T.quote_carriers("Mumbai", "Delhi", 0))["status"] == "error"


@pytest.mark.asyncio
async def test_estimate_eta():
    r = await T.estimate_eta("SHP-1002")
    assert r["status"] == "ok" and r["shipment_id"] == "SHP-1002"
    assert r["sla_risk"] in ("low", "medium", "high")
    assert isinstance(r["slack_hours"], float)
    assert r["weather_risk"] == "high"  # Chennai in October
    assert r["breakdown"]["weather_buffer_hours"] > 0
    assert (await T.estimate_eta("SHP-0000"))["status"] == "error"


def test_calendar_hours_duty_limits():
    assert planning.calendar_hours(8) == 8
    assert planning.calendar_hours(32) == pytest.approx(32 + 3 * 14)
    assert planning.calendar_hours(32, team=True) == pytest.approx(32 + 6)
    assert planning.calendar_hours(5, hours_today=8) == pytest.approx(5 + 14)


def _fake_network(now: datetime):
    iso = lambda h: (now + timedelta(hours=h)).isoformat()
    shipments = [
        # critical long-haul: greedy puts it on a tired driver and misses the promise
        {"id": "SHP-9001", "status": "created", "priority": "critical", "weight_kg": 800, "origin_city": "Mumbai",
         "last_city": "Mumbai", "destination_city": "Delhi", "promised_by": iso(45), "contents": "Auto parts"},
        # standard, same corridor (Ahmedabad is on the way to Delhi)
        {"id": "SHP-9002", "status": "created", "priority": "standard", "weight_kg": 1000, "origin_city": "Mumbai",
         "last_city": "Mumbai", "destination_city": "Ahmedabad", "promised_by": iso(120), "contents": "Cement"},
        # standard to Chennai: diverging corridor + HIGH weather risk in October
        {"id": "SHP-9003", "status": "created", "priority": "standard", "weight_kg": 200, "origin_city": "Mumbai",
         "last_city": "Mumbai", "destination_city": "Chennai", "promised_by": iso(120), "contents": "Textiles"},
        # critical with no vehicle in Pune
        {"id": "SHP-9004", "status": "created", "priority": "critical", "weight_kg": 300, "origin_city": "Pune",
         "last_city": "Pune", "destination_city": "Hyderabad", "promised_by": iso(30), "contents": "Medicines"},
    ]
    vehicles = [
        {"id": "TRK-901", "type": "Eicher 14ft", "capacity_kg": 4000, "remaining_capacity_kg": 4000, "status": "available",
         "current_city": "Mumbai", "driver_name": "Tired Driver", "driver_hours_today": 8.5, "refrigerated": False, "fuel_pct": 80},
        {"id": "TRK-902", "type": "32ft SXL", "capacity_kg": 6000, "remaining_capacity_kg": 6000, "status": "available",
         "current_city": "Mumbai", "driver_name": "Fresh Driver", "driver_hours_today": 1.0, "refrigerated": False, "fuel_pct": 90},
    ]

    def list_shipments(status=None, city=None, priority=None, limit=50):
        rows = [dict(s) for s in shipments if status is None or s["status"] == status]
        return {"status": "ok", "count": len(rows), "shipments": rows}

    def list_vehicles(status=None, city=None):
        rows = [dict(v) for v in vehicles if status is None or v["status"] == status]
        return {"status": "ok", "count": len(rows), "vehicles": rows}

    return list_shipments, list_vehicles


@pytest.mark.asyncio
async def test_dispatch_loop_propose_validate_revise_converges(monkeypatch):
    from cargomind.data import db

    list_shipments, list_vehicles = _fake_network(planning._now())
    monkeypatch.setattr(db, "list_shipments", list_shipments)
    monkeypatch.setattr(db, "list_vehicles", list_vehicles)
    assigned: list[tuple[str, str]] = []
    monkeypatch.setattr(db, "assign_vehicle", lambda s, v: assigned.append((s, v)) or {"status": "ok", "warnings": []})
    tc = FakeToolContext()

    # committing before any validation is refused
    p1 = await T.propose_dispatch_plan(tc)
    assert p1["revision"] == 1 and p1["strategy"] == "greedy first-fit"
    assert T.commit_dispatch_plan(tc)["status"] == "error"

    v1 = await T.validate_dispatch_plan(tc)
    assert v1["valid"] is False and v1["score"] < 100
    types = {i["type"] for i in v1["issues"] if i["severity"] == "blocker"}
    assert {"driver_hours", "sla", "weather", "critical_unassigned"} <= types
    assert tc.state["plan_issues"]

    p2 = await T.propose_dispatch_plan(tc)
    assert p2["revision"] == 2 and tc.state["plan_revision"] == 2
    assert p2["plan_id"] == p1["plan_id"] and "critic" in p2["strategy"]
    assert any(n.startswith("Fixed") for n in p2["notes"])
    v2 = await T.validate_dispatch_plan(tc)
    assert v2["valid"] is True, v2["issues"]
    assert v2["score"] > v1["score"] and tc.state["plan_issues"] == []

    plan = tc.state["current_plan"]
    on = {s["shipment_id"]: a for a in plan["assignments"] for s in a["shipments"]}
    assert on["SHP-9001"]["vehicle_id"] == "TRK-902" and on["SHP-9001"]["team_drivers"] is True
    assert on["SHP-9002"]["vehicle_id"] == "TRK-902"  # consolidated on the same corridor
    assert {u["shipment_id"]: u["reason_code"] for u in plan["unassigned"]} == {"SHP-9003": "weather_deferred"}
    assert [h["shipment_id"] for h in plan["carrier_handoffs"]] == ["SHP-9004"]

    c = T.commit_dispatch_plan(tc)
    assert c["status"] == "ok" and sorted(assigned) == [("SHP-9001", "TRK-902"), ("SHP-9002", "TRK-902")]
    assert c["carrier_bookings_needed"][0]["shipment_id"] == "SHP-9004"
    assert T.commit_dispatch_plan(tc)["already_committed"] is True


@pytest.mark.asyncio
async def test_dispatch_loop_on_seeded_db_converges_and_commits():
    from cargomind.data import db

    tc = FakeToolContext()
    valid_at = None
    for iteration in range(1, 4):
        p = await T.propose_dispatch_plan(tc)
        assert p["status"] == "ok"
        v = await T.validate_dispatch_plan(tc)
        if v["valid"]:
            valid_at = iteration
            break
    assert valid_at is not None and valid_at <= 2
    res = T.commit_dispatch_plan(tc)
    assert res["status"] == "ok" and not res["failed"]
    for row in res["applied"]:
        assert db.get_shipment(row["shipment_id"])["vehicle_id"] == row["vehicle_id"]


@pytest.mark.asyncio
async def test_validate_without_plan_is_error_and_city_filter():
    tc = FakeToolContext()
    assert (await T.validate_dispatch_plan(tc))["status"] == "error"
    assert (await T.propose_dispatch_plan(tc, city="Atlantis"))["status"] == "error"
    p = await T.propose_dispatch_plan(tc, city="Hyderabad")
    assert p["city"] == "Hyderabad"
    for a in p["assignments"]:
        assert a["city"] == "Hyderabad"


# ------------------------------------------------------------------------------ vision
def _make_images(upload: Path) -> None:
    from PIL import Image, ImageDraw, ImageFilter

    img = Image.new("RGB", (640, 480), (225, 225, 220))
    d = ImageDraw.Draw(img)
    d.rectangle([140, 120, 500, 400], fill=(176, 128, 82), outline=(60, 40, 20), width=4)
    for x in range(150, 500, 18):
        d.line([(x, 130), (x + 10, 390)], fill=(120, 85, 50), width=2)
    d.rectangle([300, 200, 460, 260], fill=(250, 250, 250))
    d.text((310, 220), "AWB SHP-1004", fill=(0, 0, 0))
    img.save(upload / "pod_ok.jpg", quality=92)
    dark = Image.new("RGB", (400, 300), (12, 10, 9)).filter(ImageFilter.GaussianBlur(4))
    dark.save(upload / "pod_dark.png")
    (upload / "notes.txt").write_text("not an image")


@pytest.mark.asyncio
async def test_inspect_photo_offline_heuristic():
    _make_images(config.UPLOAD_DIR)
    ok = await T.inspect_delivery_photo("pod_ok.jpg", "SHP-1004")
    assert ok["status"] == "ok" and ok["source"] == "pillow_heuristic" and ok["degraded"] is True
    assert ok["damage_detected"] is None and ok["recommended_action"] == "manual_review"
    assert "NOT assessed" in ok["note"]
    assert ok["image"]["dominant_colors"]
    dark = await T.inspect_delivery_photo("pod_dark.png")
    assert dark["recommended_action"] == "retake_photo"
    assert any("dark" in f for f in dark["findings"])


@pytest.mark.asyncio
async def test_inspect_photo_guards():
    _make_images(config.UPLOAD_DIR)
    assert (await T.inspect_delivery_photo("../../etc/passwd"))["status"] == "error"
    assert "traversal" in (await T.inspect_delivery_photo("..\\secret.jpg"))["error"]
    missing = await T.inspect_delivery_photo("nope.jpg")
    assert missing["status"] == "error" and "pod_ok.jpg" in missing["suggestions"]
    assert (await T.inspect_delivery_photo("notes.txt"))["status"] == "error"
    # a full path that points inside the upload dir is accepted
    full = await T.inspect_delivery_photo(str(config.UPLOAD_DIR / "pod_ok.jpg"))
    assert full["status"] == "ok"


@pytest.mark.asyncio
async def test_inspect_photo_gemini_paths(monkeypatch, journal_events):
    _make_images(config.UPLOAD_DIR)
    monkeypatch.setattr(config, "online_gemini", lambda: True)

    async def good(data, mime, sid):
        assert mime == "image/jpeg" and data[:2] == b"\xff\xd8" and sid == "SHP-1004"
        return {"damage_detected": True, "severity": "major", "findings": ["Crushed corner"], "package_count_visible": 1,
                "label_readable": True, "recommended_action": "open_damage_claim", "confidence": 0.9}

    monkeypatch.setattr(vision, "_gemini_inspect", good)
    r = await T.inspect_delivery_photo("pod_ok.jpg", "SHP-1004")
    assert r["source"] == "gemini" and r["degraded"] is False and r["severity"] == "major"

    async def boom(*_a):
        raise RuntimeError("503 model overloaded")

    monkeypatch.setattr(vision, "_gemini_inspect", boom)
    r = await T.inspect_delivery_photo("pod_ok.jpg", "SHP-1004")
    assert r["source"] == "pillow_heuristic" and r["degraded"] is True
    assert any(e["component"] == "vision.gemini" for e in journal_events)


# ------------------------------------------------------------------------------ comms
def test_draft_notifications_per_channel():
    sms = T.draft_customer_notification("SHP-1002", "heavy rain near Krishnagiri on NH-48 slowing all traffic", "sms")
    assert sms["status"] == "ok" and sms["category"] == "delay"
    assert sms["char_count"] <= 160 and sms["sms_segments"] == 1
    assert "https://track.cargomind.dev/SHP-1002" in sms["message"] and "Sorry" in sms["message"]
    email = T.draft_customer_notification("shp1002", "heavy rain near Krishnagiri", "email")
    assert email["subject"].startswith("Your shipment SHP-1002") and "apologise" in email["message"]
    assert email["recipient"] and "@" in email["recipient"]
    wa = T.draft_customer_notification("SHP-1002", "out for delivery", "WhatsApp")
    assert wa["channel"] == "whatsapp" and wa["category"] == "out_for_delivery" and "*" in wa["message"]
    assert T.draft_customer_notification("SHP-1002", "x", "pigeon")["status"] == "error"
    assert T.draft_customer_notification("SHP-0000", "x")["status"] == "error"


def test_send_notification_queues_in_outbox():
    from cargomind.data import db

    r = comms.send_notification("SHP-1002", "email", "Subject: Delay on SHP-1002\n\nHello, your parcel is late.")
    assert r["status"] == "queued (simulated)" and r["outbox_id"]
    assert r["subject"] == "Delay on SHP-1002"
    latest = db.list_outbox(1)["messages"][0]
    assert latest["id"] == r["outbox_id"] and latest["status"] == "queued (simulated)"
    assert latest["message"] == "Hello, your parcel is late."
    assert T.send_notification("SHP-1002", "sms", "x" * 500)["status"] == "error"
    assert T.send_notification("SHP-1002", "sms", "  ")["status"] == "error"


def test_tool_registry_is_complete():
    names = {f.__name__ for f in T.ALL_TOOLS}
    assert len(names) == 13
    for f in T.ALL_TOOLS:
        assert f.__doc__ and "Returns:" in f.__doc__
