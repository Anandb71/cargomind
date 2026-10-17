"""Disruption Autopilot + Watchtower (hermetic: offline rule-engine, local MCP)."""
from __future__ import annotations

import asyncio

import pytest

from cargomind import autopilot, watchtower
from cargomind.healing.offline_llm import _extract


@pytest.fixture(autouse=True)
def isolate_watchtower_alerts(monkeypatch):
    monkeypatch.setattr(watchtower, "_active", {})


def test_autopilot_end_to_end_offline():
    r = asyncio.run(autopilot.run_autopilot("madras", "Cyclone", "severe"))
    assert r["status"] == "ok"
    impact = r["impact"]
    assert impact["city"] == "Chennai" and impact["event"] == "cyclone" and impact["severity"] == "high"
    assert impact["affected_shipments"] == len(impact["shipments"]) > 0
    ids = {s["id"] for s in impact["shipments"]}
    assert {a["shipment_id"] for a in r["reroutes"]["shipment_actions"]} == ids
    assert r["reroutes"]["alternate_hub"] != "Chennai" and r["reroutes"]["requires_approval"]
    assert r["notices"]["sent"] is False
    assert all(d["priority"] in ("critical", "express") for d in r["notices"]["drafts"])


def test_autopilot_unknown_city_is_soft_error():
    r = asyncio.run(autopilot.assess_disruption_impact("Atlantis"))
    assert r["status"] == "error" and "Chennai" in r["suggestions"]


def test_offline_extracts_event_and_severity():
    text = "Cyclone alert in Chennai, severity high - run disruption autopilot"
    assert _extract("event", "str", text, ["Chennai"]) == "cyclone"
    assert _extract("severity", "str", text, ["Chennai"]) == "high"
    assert _extract("severity", "str", "critical shipment in Pune", ["Pune"]) is None


def test_concierge_routes_to_autopilot_offline():
    from cargomind.runtime import ask

    out = asyncio.run(ask("Cyclone alert in Chennai, severity high - run disruption autopilot", session_id="t-ap"))
    assert "chennai" in out.lower()


def test_watchtower_scan_is_stable_and_well_formed():
    first = asyncio.run(watchtower.scan_once())
    assert first["active"], "seed data has delayed shipments and low stock"
    for a in first["active"]:
        assert {"id", "kind", "severity", "title", "detail", "suggested_prompt"} <= a.keys()
        assert a["severity"] in ("critical", "warning", "info")
    second = asyncio.run(watchtower.scan_once())
    assert second["new"] == []  # nothing changed -> nothing new
    assert [a["id"] for a in watchtower.latest_alerts()] == [a["id"] for a in second["active"]]


@pytest.mark.parametrize("kind", ["sla_risk", "low_stock"])
def test_watchtower_covers_core_checks(kind):
    asyncio.run(watchtower.scan_once())
    assert any(a["kind"] == kind for a in watchtower.latest_alerts())


def test_reroutes_use_existing_warehouse_hubs(monkeypatch):
    monkeypatch.setattr(autopilot.db, "list_warehouses", lambda: {
        "warehouses": [{"city": "Mumbai"}, {"city": "Bengaluru"}],
    })
    monkeypatch.setattr(autopilot.db, "list_shipments", lambda **_: {"shipments": []})
    monkeypatch.setattr(autopilot.db, "list_vehicles", lambda: {"vehicles": []})

    result = asyncio.run(autopilot.propose_disruption_reroutes("Mumbai", "cyclone", "high"))

    assert result["status"] == "ok"
    assert result["alternate_hub"] == "Bengaluru"  # nearby Pune has no warehouse in this network


def test_autopilot_without_alternate_hub_reports_incomplete_plan(monkeypatch):
    monkeypatch.setattr(autopilot.db, "list_warehouses", lambda: {"warehouses": [{"city": "Chennai", "id": "WH-MAA"}]})

    result = asyncio.run(autopilot.run_autopilot("Chennai", "cyclone", "high"))

    assert result["status"] == "error"
    assert "No alternate warehouse hub" in result["error"]
    assert result["reroutes"]["shipment_actions"] == []
    assert result["reroutes"]["vehicle_moves"] == []
    assert result["reroutes"]["suggestions"]
    assert result["notices"]["sent"] is False


@pytest.mark.parametrize("status", ["created", "picked_up"])
@pytest.mark.parametrize("severity, expected_action", [("high", "HOLD at origin"), ("medium", "Depart early")])
def test_outbound_load_at_origin_obeys_severity(monkeypatch, status, severity, expected_action):
    shipment = {
        "id": "SHP-1999", "status": status, "priority": "standard",
        "origin_city": "Chennai", "last_city": "Chennai", "destination_city": "Delhi",
    }
    monkeypatch.setattr(autopilot.db, "list_shipments", lambda **_: {"shipments": [shipment]})
    monkeypatch.setattr(autopilot.db, "list_vehicles", lambda: {"vehicles": []})
    monkeypatch.setattr(autopilot.db, "list_warehouses", lambda: {"warehouses": [{"city": "Bengaluru"}]})

    result = asyncio.run(autopilot.propose_disruption_reroutes("Chennai", "cyclone", severity))

    assert result["shipment_actions"][0]["action"].startswith(expected_action)


@pytest.mark.parametrize("fuel, expected", [(0, True), (10, True), (20, False), (None, False)])
def test_watchtower_zero_fuel_is_not_missing_data(monkeypatch, fuel, expected):
    monkeypatch.setattr(watchtower.db, "list_vehicles", lambda: {"vehicles": [
        {"id": "TRK-199", "fuel_pct": fuel, "driver_hours_remaining": 5},
    ]})

    assert bool(watchtower._fleet_alerts()) is expected


@pytest.mark.parametrize("failed_check", ["_shipment_alerts", "_stock_alerts", "_fleet_alerts"])
def test_watchtower_retains_only_failed_source_until_healthy_scan(monkeypatch, failed_check):
    alerts = {
        "_shipment_alerts": watchtower._alert("exc:SHP-1999", "exception", "critical", "Exception", "", ""),
        "_stock_alerts": watchtower._alert("stock:SKU-199:WH-MAA", "low_stock", "critical", "Empty", "", ""),
        "_fleet_alerts": watchtower._alert("fuel:TRK-199", "fleet", "warning", "Empty tank", "", ""),
    }
    for name, alert in alerts.items():
        monkeypatch.setattr(watchtower, name, lambda alert=alert: [alert])

    async def no_weather():
        return [], set()

    def failed_source():
        raise RuntimeError("temporary DB outage")

    monkeypatch.setattr(watchtower, "_weather_alerts", no_weather)
    first = asyncio.run(watchtower.scan_once())
    assert len(first["active"]) == 3
    failed_id = alerts[failed_check]["id"]
    raised_at = alerts[failed_check]["raised_at"]

    monkeypatch.setattr(watchtower, failed_check, failed_source)
    healthy_check = next(name for name in alerts if name != failed_check)
    monkeypatch.setattr(watchtower, healthy_check, lambda: [])
    second = asyncio.run(watchtower.scan_once())

    active = {a["id"]: a for a in second["active"]}
    assert failed_id in active
    assert active[failed_id]["raised_at"] == raised_at
    assert active[failed_id]["stale"] is True
    assert alerts[healthy_check]["id"] not in active
    assert second["new"] == []

    monkeypatch.setattr(watchtower, failed_check, lambda: [alerts[failed_check]])
    healthy = asyncio.run(watchtower.scan_once())
    assert not next(a for a in healthy["active"] if a["id"] == failed_id).get("stale", False)

    monkeypatch.setattr(watchtower, failed_check, lambda: [])
    recovered = asyncio.run(watchtower.scan_once())
    assert failed_id not in {a["id"] for a in recovered["active"]}


@pytest.mark.parametrize("failure", [TimeoutError("weather timed out"), {"status": "error", "error": "provider down"}])
def test_watchtower_retains_failed_weather_city_and_resolves_healthy_cities(monkeypatch, failure):
    monkeypatch.setattr(watchtower, "HUBS", ["Chennai", "Mumbai"])
    for name in ("_shipment_alerts", "_stock_alerts", "_fleet_alerts"):
        monkeypatch.setattr(watchtower, name, lambda: [])
    responses = {city: {"status": "ok", "risk_level": "high"} for city in watchtower.HUBS}

    async def city_weather(city, hours_ahead):
        result = responses[city]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(watchtower.weather, "get_weather_risk", city_weather)
    first = asyncio.run(watchtower.scan_once())
    assert {a["id"] for a in first["active"]} == {"wx:Chennai", "wx:Mumbai"}
    raised_at = next(a["raised_at"] for a in first["active"] if a["id"] == "wx:Chennai")

    responses["Chennai"] = failure
    responses["Mumbai"] = {"status": "ok", "risk_level": "low"}
    second = asyncio.run(watchtower.scan_once())

    assert [a["id"] for a in second["active"]] == ["wx:Chennai"]
    assert second["active"][0]["raised_at"] == raised_at
    assert second["active"][0]["stale"] is True
    assert second["new"] == []

    responses["Chennai"] = {"status": "ok", "risk_level": "medium"}
    healthy = asyncio.run(watchtower.scan_once())
    assert not healthy["active"][0].get("stale", False)
    assert healthy["active"][0]["raised_at"] == raised_at

    responses["Chennai"] = {"status": "ok", "risk_level": "low"}
    assert asyncio.run(watchtower.scan_once())["active"] == []
