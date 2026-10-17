"""Tests for the gazetteer and the SQLite operational store (temp DB per test)."""
from __future__ import annotations

import json

import pytest

from cargomind import config
from cargomind.data import db
from cargomind.data.gazetteer import CITIES, find_cities_in_text, haversine_km, resolve_city


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "cargomind-test.db")
    db.close_conn()
    yield db
    db.close_conn()


# --------------------------------------------------------------------------- gazetteer
def test_gazetteer_aliases_and_codes():
    assert len(CITIES) >= 20
    assert resolve_city("bangalore") == "Bengaluru"
    assert resolve_city("BOMBAY") == "Mumbai"
    assert resolve_city("Madras") == "Chennai"
    assert resolve_city("calcutta") == "Kolkata"
    assert resolve_city("Gurgaon") == "Gurugram"
    assert resolve_city("New Delhi, India") == "Delhi"
    assert resolve_city("vizag") == "Visakhapatnam"
    assert resolve_city("cochin") == "Kochi"
    assert resolve_city("blr") == "Bengaluru"
    assert resolve_city("WH-HYD") == "Hyderabad"
    assert resolve_city("Hyderbad") == "Hyderabad"  # typo tolerance
    assert resolve_city("Atlantis") is None
    assert resolve_city("") is None


def test_find_cities_in_text_order_dedup_boundaries():
    text = "Move stock from Bombay to Bangalore, then BLR -> New Delhi; mumbai again. Delhiwala is not a city."
    assert find_cities_in_text(text) == ["Mumbai", "Bengaluru", "Delhi"]
    assert find_cities_in_text("the del key and maa") == []  # lower-case codes are not cities in prose
    assert find_cities_in_text("") == []


def test_haversine():
    d = haversine_km("Mumbai", "Delhi")
    assert 1100 < d < 1200
    assert haversine_km((19.076, 72.8777), "bombay") == 0.0
    assert haversine_km("Chennai", "Bengaluru") == haversine_km("madras", "bangalore")
    with pytest.raises(ValueError):
        haversine_km("Atlantis", "Delhi")


# --------------------------------------------------------------------------- seed
def test_seed_counts_and_determinism(fresh_db):
    c = db.counts()
    assert c["shipments"] == 25
    assert c["warehouses"] == 5
    assert c["vehicles"] == 10
    assert c["carriers"] == 5
    assert c["shipment_events"] > 25
    skus = {r["sku"] for r in db.get_inventory()["items"]}
    assert len(skus) == 20
    first = db.get_shipment("SHP-1001")
    db.reset_db()
    again = db.get_shipment("SHP-1001")
    for key in ("customer", "origin_city", "destination_city", "status", "priority", "weight_kg", "carrier_id"):
        assert first[key] == again[key]


def test_seed_looks_live(fresh_db):
    by_status = db.kpis()["by_status"]
    for st in ("in_transit", "delayed", "out_for_delivery", "delivered", "exception"):
        assert by_status[st] >= 1, st
    late = [s for s in db.list_shipments(status="delayed")["shipments"] if s["is_late"]]
    assert late, "delayed shipments should be past their promise"
    assert db.list_shipments(priority="critical")["count"] >= 3
    for s in db.list_shipments(limit=100)["shipments"]:
        assert 5 <= s["weight_kg"] <= 2000
        assert s["id"].startswith("SHP-") and s["order_id"].startswith("ORD-")
    json.dumps(db.list_shipments(limit=100))  # JSON-serialisable


# --------------------------------------------------------------------------- lookups
def test_not_found_returns_suggestions(fresh_db):
    r = db.get_shipment("SHP-1001X")
    assert r["status"] == "error"
    assert r["suggestions"][0] == "SHP-1001"
    assert db.get_shipment("SHP-1O01")["id"] == "SHP-1001"  # letter O typo is repaired
    assert db.shipment_history("SHP-9999")["status"] == "error"
    assert db.get_vehicle("trk-1O1")["id"] == "TRK-101"
    v = db.get_vehicle("TRK-901")
    assert v["status"] == "error" and v["suggestions"]
    w = db.get_inventory(warehouse_id="WH-XYZ")
    assert w["status"] == "error" and w["suggestions"]


def test_id_and_filter_normalisation(fresh_db):
    assert db.get_shipment("shp1001")["id"] == "SHP-1001"
    assert db.get_shipment("1001")["id"] == "SHP-1001"
    blr = db.list_shipments(city="bangalore")
    assert blr["count"] > 0
    assert all("Bengaluru" in (s["origin_city"], s["destination_city"], s["last_city"]) for s in blr["shipments"])
    assert db.list_shipments(status="bogus")["status"] == "error"
    active = db.list_shipments(status="active", limit=100)["shipments"]
    assert active and all(s["status"] in db.ACTIVE_STATUSES for s in active)


# --------------------------------------------------------------------------- mutations
def test_update_status_appends_event(fresh_db):
    before = db.shipment_history("SHP-1002")["count"]
    r = db.update_shipment_status("SHP-1002", "out for delivery", city="madras", note="Rain cleared")
    assert r["status"] == "ok"
    assert r["shipment"]["status"] == "out_for_delivery"
    assert r["shipment"]["last_city"] == "Chennai"
    hist = db.shipment_history("SHP-1002")
    assert hist["count"] == before + 1
    assert hist["events"][-1]["note"] == "Rain cleared"

    bad = db.update_shipment_status("SHP-1002", "teleported")
    assert bad["status"] == "error" and "in_transit" in bad["suggestions"]
    assert db.update_shipment_status("SHP-0000", "delivered")["status"] == "error"

    done = db.update_shipment_status("SHP-1002", "delivered")
    assert done["shipment"]["status"] == "delivered"
    assert db.update_shipment_status("SHP-1002", "in_transit")["status"] == "error"  # terminal


def test_low_stock_and_reorder(fresh_db):
    low = db.get_inventory(low_stock_only=True)
    assert low["count"] >= 5
    assert all(i["qty_on_hand"] <= i["reorder_point"] and i["low_stock"] for i in low["items"])
    line = next(i for i in low["items"] if not i["open_po"])
    po = db.create_reorder(line["sku"], line["warehouse_id"])
    assert po["status"] == "ok" and not po["duplicate"]
    assert po["purchase_order"]["id"].startswith("PO-")
    again = db.create_reorder(line["sku"], line["warehouse_id"])
    assert again["duplicate"] and again["purchase_order"]["id"] == po["purchase_order"]["id"]
    assert db.create_reorder("SKU-999", "WH-BOM")["status"] == "error"
    assert db.get_inventory(sku="insulin")["count"] >= 1


def test_assign_vehicle_capacity_and_idempotency(fresh_db):
    over = db.assign_vehicle("SHP-1022", "TRK-104")  # 1100 kg on a 750 kg Tata Ace
    assert over["status"] == "error" and over["suggestions"]
    ok = db.assign_vehicle("SHP-1014", "TRK-110")
    assert ok["status"] == "ok"
    assert db.get_shipment("SHP-1014")["vehicle_id"] == "TRK-110"
    assert db.get_vehicle("TRK-110")["status"] == "on_route"
    assert db.assign_vehicle("SHP-1014", "TRK-110")["already_assigned"] is True
    assert db.assign_vehicle("SHP-1014", "TRK-109")["status"] == "error"  # maintenance


def test_kpis_keys(fresh_db):
    k = db.kpis()
    for key in ("on_time_rate", "in_transit", "delayed", "exceptions", "delivered_today",
                "low_stock_skus", "fleet_utilization", "open_pos"):
        assert key in k, key
    assert 0 <= k["on_time_rate"] <= 100
    assert k["delivered_today"] >= 1
    assert k["exceptions"] == 2
    assert k["open_pos"] >= 1


def test_outbox_and_directory(fresh_db):
    r = db.add_outbox(shipment_id="SHP-1003", channel="email", subject="Delay", message="Sorry for the delay")
    assert r["status"] == "ok" and r["entry"]["recipient"] == ""
    assert db.list_outbox(5)["messages"][0]["id"] == r["outbox_id"]
    assert db.list_carriers()["count"] == 5
    assert isinstance(db.list_carriers()["carriers"][0]["modes"], list)
    assert {w["id"] for w in db.list_warehouses()["warehouses"]} == {"WH-BOM", "WH-DEL", "WH-BLR", "WH-MAA", "WH-HYD"}
    assert db.get_warehouse("Bangalore")["id"] == "WH-BLR"
