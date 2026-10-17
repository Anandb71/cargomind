"""Disruption Autopilot + Watchtower (hermetic: offline rule-engine, local MCP)."""
from __future__ import annotations

import asyncio

import pytest

from cargomind import autopilot, watchtower
from cargomind.healing.offline_llm import _extract


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


def _fake_weather(high: set[str]):
    async def get_weather_risk(city: str, hours_ahead: int = 24) -> dict:
        return {"city": city, "risk_level": "high" if city in high else "low"}

    return get_weather_risk


def test_reroute_skips_staging_hub_at_high_weather_risk(monkeypatch):
    monkeypatch.setattr(autopilot.weather, "get_weather_risk", _fake_weather({"Bengaluru"}))
    r = asyncio.run(autopilot.propose_disruption_reroutes("Chennai", "cyclone", "high"))
    assert r["alternate_hub"] not in ("Chennai", "Bengaluru")
    assert r["alternate_hub_weather"] == "low"
    assert r["hubs_skipped_for_weather"] == [{"hub": "Bengaluru", "weather_risk": "high"}]
    assert all("Bengaluru hub" not in a["action"] for a in r["shipment_actions"])
    assert "warning" not in r


def test_reroute_warns_when_every_nearby_hub_is_stormy(monkeypatch):
    monkeypatch.setattr(autopilot.weather, "get_weather_risk", _fake_weather(set(autopilot.HUBS)))
    r = asyncio.run(autopilot.propose_disruption_reroutes("Chennai", "cyclone", "high"))
    assert r["alternate_hub"] == "Bengaluru"  # nearest, but flagged for the operator
    assert r["alternate_hub_weather"] == "high" and "operator review" in r["warning"]


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
