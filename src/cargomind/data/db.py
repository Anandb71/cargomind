"""SQLite operational store for CargoMind (shipments, fleet, carriers, warehouses, stock, POs).

Design notes
------------
* One shared connection per process (``check_same_thread=False``) guarded by an RLock; WAL
  journal so the MCP server subprocess and the web app can read/write the same file.
* ``get_conn()`` lazily creates the schema and seeds deterministic demo data
  (``random.Random(42)``) on first use. Timestamps are ISO-8601 in IST and relative to
  "now", so the network always looks live. If a seeded DB is reopened hours/days later the
  whole timeline is shifted forward ("clock catch-up") so ETAs/promises still look current.
* Every public function returns JSON-serialisable data. Look-ups that miss never raise:
  they return ``{"status": "error", "error": ..., "suggestions": [closest ids]}``.
  Collection functions return ``{"status": "ok", "count": n, "<plural>": [...]}``;
  ``get_shipment`` returns the flat (enriched) shipment row.
* ``config.DB_PATH`` is read at call time, so tests can monkeypatch it.
"""
from __future__ import annotations

import difflib
import json
import logging
import math
import random
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cargomind import config
from cargomind.data.gazetteer import CITIES, haversine_km, resolve_city

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

SHIPMENT_STATUSES: tuple[str, ...] = (
    "created", "picked_up", "in_transit", "delayed", "out_for_delivery", "delivered", "exception",
)
ACTIVE_STATUSES: tuple[str, ...] = ("created", "picked_up", "in_transit", "delayed", "out_for_delivery")
MOVING_STATUSES: tuple[str, ...] = ("picked_up", "in_transit", "out_for_delivery")
PRIORITIES: tuple[str, ...] = ("standard", "express", "critical")
VEHICLE_STATUSES: tuple[str, ...] = ("available", "on_route", "maintenance")
PO_OPEN_STATUSES: tuple[str, ...] = ("open", "approved", "shipped")
MAX_DRIVER_HOURS = 10.0
ROAD_FACTOR = 1.3  # great-circle -> road km

_STATUS_ALIASES = {
    "new": "created", "booked": "created", "pending": "created",
    "picked": "picked_up", "pickup": "picked_up", "collected": "picked_up",
    "transit": "in_transit", "intransit": "in_transit", "moving": "in_transit", "dispatched": "in_transit",
    "late": "delayed", "delay": "delayed", "held": "delayed",
    "ofd": "out_for_delivery", "out_for_deliver": "out_for_delivery", "outfordelivery": "out_for_delivery",
    "done": "delivered", "complete": "delivered", "completed": "delivered",
    "failed": "exception", "damaged": "exception", "lost": "exception", "error": "exception",
}

_OWN_TABLES = (
    "shipment_events", "outbox", "purchase_orders", "shipments", "inventory",
    "vehicles", "carriers", "warehouses", "cm_meta",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cm_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS warehouses (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL, address TEXT,
    lat REAL, lon REAL, capacity_pallets INTEGER, utilization_pct REAL,
    manager TEXT, phone TEXT, cold_chain INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS inventory (
    sku TEXT NOT NULL, warehouse_id TEXT NOT NULL, name TEXT, category TEXT,
    qty_on_hand INTEGER NOT NULL, reorder_point INTEGER NOT NULL, reorder_qty INTEGER NOT NULL,
    unit_cost_inr REAL, supplier TEXT, updated_at TEXT,
    PRIMARY KEY (sku, warehouse_id)
);
CREATE TABLE IF NOT EXISTS vehicles (
    id TEXT PRIMARY KEY, type TEXT NOT NULL, capacity_kg REAL NOT NULL, status TEXT NOT NULL,
    current_city TEXT, home_warehouse_id TEXT, driver_name TEXT, driver_phone TEXT,
    driver_hours_today REAL, fuel_pct REAL, refrigerated INTEGER DEFAULT 0, registration TEXT
);
CREATE TABLE IF NOT EXISTS carriers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, modes TEXT, base_rate_per_km REAL, per_kg_rate REAL,
    reliability REAL, avg_speed_kmph REAL, max_weight_kg REAL, contact_email TEXT
);
CREATE TABLE IF NOT EXISTS shipments (
    id TEXT PRIMARY KEY, order_id TEXT, customer TEXT, customer_email TEXT, customer_phone TEXT,
    origin_city TEXT, destination_city TEXT, status TEXT NOT NULL, priority TEXT NOT NULL,
    weight_kg REAL, carrier_id TEXT, vehicle_id TEXT, eta TEXT, promised_by TEXT,
    last_update TEXT, last_city TEXT, notes TEXT DEFAULT '', contents TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_shipments_status ON shipments(status);
CREATE TABLE IF NOT EXISTS shipment_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id TEXT NOT NULL, ts TEXT NOT NULL,
    status TEXT NOT NULL, city TEXT, note TEXT DEFAULT '', source TEXT DEFAULT 'system'
);
CREATE INDEX IF NOT EXISTS ix_events_shipment ON shipment_events(shipment_id, ts);
CREATE TABLE IF NOT EXISTS purchase_orders (
    id TEXT PRIMARY KEY, sku TEXT NOT NULL, warehouse_id TEXT NOT NULL, qty INTEGER NOT NULL,
    supplier TEXT, status TEXT NOT NULL, unit_cost_inr REAL, total_cost_inr REAL,
    created_at TEXT, expected_by TEXT, note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, shipment_id TEXT, channel TEXT,
    recipient TEXT, subject TEXT, message TEXT, status TEXT, meta TEXT
);
"""

# Timestamp columns shifted by the clock catch-up (outbox is a real log, left untouched).
_TS_COLUMNS: dict[str, tuple[str, ...]] = {
    "shipments": ("eta", "promised_by", "last_update", "created_at"),
    "shipment_events": ("ts",),
    "purchase_orders": ("created_at", "expected_by"),
    "inventory": ("updated_at",),
}
_CLOCK_SHIFT_THRESHOLD = timedelta(hours=6)

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_conn_path: Path | None = None


# --------------------------------------------------------------------------- helpers
def _now() -> datetime:
    return datetime.now(IST).replace(microsecond=0)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(IST).replace(microsecond=0).isoformat() if dt else None


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)


def _dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


def _err(error: str, suggestions: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "error", "error": error, "suggestions": suggestions or []}
    out.update(extra)
    return out


def _norm_id(raw: Any, prefix: str, width: int = 0) -> str:
    """'shp1001' / '1001' / ' SHP-1001 ' -> 'SHP-1001'; 'cr1' -> 'CR-01' (width=2)."""
    s = re.sub(r"[\s_]+", "-", str(raw or "").strip().upper()).strip("-")
    if not s:
        return s
    if s.isdigit():
        s = f"{prefix}-{s}"
    elif s.startswith(prefix) and not s.startswith(prefix + "-"):
        s = f"{prefix}-{s[len(prefix):]}"
    m = re.fullmatch(rf"{prefix}-([0-9OIL]+)", s)
    if m and any(ch.isdigit() for ch in m.group(1)):  # OCR/typing slips: SHP-1O01 -> SHP-1001
        s = f"{prefix}-{m.group(1).translate(str.maketrans('OIL', '011'))}"
    if width:
        m = re.fullmatch(rf"{prefix}-(\d+)", s)
        if m:
            s = f"{prefix}-{m.group(1).zfill(width)}"
    return s


def _closest(raw: str, ids: list[str], n: int = 3) -> list[str]:
    """Closest IDs by string similarity, ties broken by numeric distance."""
    if not ids:
        return []
    key = str(raw).strip().upper()
    digits = re.findall(r"\d+", key)
    target = int(digits[-1]) if digits else None

    def num_dist(i: str) -> int:
        d = re.findall(r"\d+", i)
        return abs(int(d[-1]) - target) if (d and target is not None) else 10**9

    scored = sorted(ids, key=lambda i: (-round(difflib.SequenceMatcher(None, key, i).ratio(), 3), num_dist(i)))
    close = [i for i in scored if difflib.SequenceMatcher(None, key, i).ratio() >= 0.6][:n]
    if close:
        return close
    return sorted(ids, key=num_dist)[:n] if target is not None else scored[:n]


def _not_found(kind: str, raw: Any, ids: list[str], hint: str) -> dict[str, Any]:
    return _err(f"{kind} {str(raw)!r} not found", _closest(str(raw), ids), hint=hint)


def _city_or_raw(city: str | None) -> str | None:
    if city is None or not str(city).strip():
        return None
    return resolve_city(city) or str(city).strip().title()


def _hours(delta: timedelta) -> float:
    return round(delta.total_seconds() / 3600.0, 1)


# --------------------------------------------------------------------------- connection
def get_conn() -> sqlite3.Connection:
    """Return the process-wide connection for ``config.DB_PATH`` (creating + seeding if needed)."""
    global _conn, _conn_path
    path = Path(config.DB_PATH)
    with _lock:
        if _conn is not None and _conn_path == path:
            return _conn
        if _conn is not None:
            try:
                _conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass
            _conn, _conn_path = None, None
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False, timeout=15, isolation_level=None)
        conn.row_factory = _dict_factory
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            _ensure_ready(conn)
        except Exception:
            conn.close()
            raise
        _conn, _conn_path = conn, path
        return conn


def close_conn() -> None:
    """Close the cached connection (tests / shutdown). The next call reopens lazily."""
    global _conn, _conn_path
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn, _conn_path = None, None


@contextmanager
def _tx() -> Iterator[sqlite3.Connection]:
    with _lock:
        conn = get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def _q(sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    with _lock:
        return get_conn().execute(sql, params).fetchall()


def _q1(sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    rows = _q(sql, params)
    return rows[0] if rows else None


def _ids(table: str) -> list[str]:
    return [r["id"] for r in _q(f"SELECT id FROM {table} ORDER BY id")]


def _ensure_ready(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.execute("BEGIN IMMEDIATE")  # serialises concurrent first-run seeding across processes
    try:
        seeded = conn.execute("SELECT value FROM cm_meta WHERE key='seeded_at'").fetchone()
        if not seeded:
            _seed(conn)
            log.info("CargoMind DB seeded at %s", config.DB_PATH)
        else:
            _clock_catch_up(conn)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _clock_catch_up(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT value FROM cm_meta WHERE key='clock_anchor'").fetchone()
    anchor = _parse(row["value"]) if row else None
    now = _now()
    if anchor is None or now - anchor < _CLOCK_SHIFT_THRESHOLD:
        return
    delta = now - anchor
    for table, cols in _TS_COLUMNS.items():
        rows = conn.execute(f"SELECT rowid AS rid, {', '.join(cols)} FROM {table}").fetchall()
        for r in rows:
            vals = [_iso(_parse(r[c]) + delta) if _parse(r[c]) else r[c] for c in cols]
            conn.execute(
                f"UPDATE {table} SET {', '.join(f'{c}=?' for c in cols)} WHERE rowid=?",
                (*vals, r["rid"]),
            )
    conn.execute("INSERT OR REPLACE INTO cm_meta(key, value) VALUES('clock_anchor', ?)", (_iso(now),))
    log.info("Shifted demo timeline forward by %.1f h", delta.total_seconds() / 3600)


def init_db(reset: bool = False) -> dict[str, Any]:
    """Create the schema and seed demo data if empty. ``reset=True`` drops CargoMind tables and reseeds."""
    with _lock:
        conn = get_conn()
        if reset:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for t in _OWN_TABLES:
                    conn.execute(f"DROP TABLE IF EXISTS {t}")
                for stmt in _SCHEMA.split(";"):
                    if stmt.strip():
                        conn.execute(stmt)
                _seed(conn)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
    return {"status": "ok", "db_path": str(config.DB_PATH), "counts": counts()}


def reset_db() -> dict[str, Any]:
    """Drop and reseed all CargoMind tables (deterministic demo data)."""
    return init_db(reset=True)


def counts() -> dict[str, int]:
    """Row counts per CargoMind table."""
    return {t: _q1(f"SELECT COUNT(*) AS n FROM {t}")["n"] for t in _OWN_TABLES if t != "cm_meta"}


# --------------------------------------------------------------------------- seed data
_WAREHOUSES = [
    ("WH-BOM", "Bhiwandi Mega DC", "Mumbai", "", 4200, "Manager-WH-BOM", 1),
    ("WH-DEL", "Delhi NCR Hub", "Delhi", "", 3800, "Manager-WH-DEL", 0),
    ("WH-BLR", "Bengaluru South DC", "Bengaluru", "", 3000, "Manager-WH-BLR", 1),
    ("WH-MAA", "Chennai Port DC", "Chennai", "", 2600, "Manager-WH-MAA", 0),
    ("WH-HYD", "Hyderabad Central DC", "Hyderabad", "", 2800, "Manager-WH-HYD", 1),
]

# sku, name, category, unit_cost_inr, supplier, base reorder point, reorder qty
_SKUS = [
    ("SKU-101", "Industrial ball bearing 6205-2RS", "Auto parts", 145.0, "Pune Precision Bearings", 400, 1200),
    ("SKU-102", "LED panel light 18W", "Electricals", 389.0, "Lumina Lighting Works", 250, 800),
    ("SKU-103", "Basmati rice 25kg bag", "FMCG", 2150.0, "Karnal Grain Mills", 120, 300),
    ("SKU-104", "Cotton bedsheet (double)", "Home textiles", 649.0, "Tiruppur Weaves", 150, 500),
    ("SKU-105", "Smartphone 128GB", "Electronics", 13999.0, "Noida Mobile Assembly", 80, 250),
    ("SKU-106", "Paracetamol 500mg (strip of 10)", "Pharma", 18.0, "Baddi Pharma Labs", 3000, 10000),
    ("SKU-107", "Insulin pens (cold chain)", "Pharma", 520.0, "Ahmedabad Biologicals", 200, 600),
    ("SKU-108", "OPC cement 50kg bag", "Construction", 395.0, "Satna Cement Co", 300, 1000),
    ("SKU-109", "Copper wire 2.5 sq mm coil", "Electricals", 2350.0, "Silvassa Cables", 100, 300),
    ("SKU-110", "Laptop 14in i5", "Electronics", 52990.0, "Chennai Electronics Mfg", 40, 120),
    ("SKU-111", "Ceiling fan 1200mm", "Appliances", 1890.0, "Hyderabad Fans Ltd", 120, 400),
    ("SKU-112", "Packaged drinking water 1L x12", "FMCG", 180.0, "Nashik Springs", 500, 1500),
    ("SKU-113", "Two-wheeler tyre 90/90-12", "Auto parts", 1450.0, "Kerala Rubber Works", 150, 400),
    ("SKU-114", "Solar panel 540W mono PERC", "Energy", 14200.0, "Gujarat Solar Fab", 60, 200),
    ("SKU-115", "PVC pipe 4in x 6m", "Construction", 690.0, "Jaipur Polymers", 200, 600),
    ("SKU-116", "Corrugated carton 18x12x12", "Packaging", 42.0, "Vapi Packaging", 2000, 8000),
    ("SKU-117", "Stretch wrap roll 500mm", "Packaging", 310.0, "Daman Films", 300, 900),
    ("SKU-118", "Nitrile surgical gloves (box of 100)", "Medical", 420.0, "Chennai Medisupplies", 400, 1500),
    ("SKU-119", "Assam CTC tea 1kg", "FMCG", 410.0, "Dibrugarh Tea Estates", 250, 800),
    ("SKU-120", "Lithium battery pack 48V 30Ah", "Energy", 38500.0, "Bengaluru Cell Tech", 30, 100),
]
_FORCED_LOW = {
    ("SKU-107", "WH-BLR"), ("SKU-118", "WH-DEL"), ("SKU-105", "WH-BOM"),
    ("SKU-114", "WH-HYD"), ("SKU-103", "WH-MAA"), ("SKU-120", "WH-BLR"),
}

# id, type, capacity_kg, status, city, home wh, driver, hours today, fuel %, refrigerated
_VEHICLES = [
    ("TRK-101", "32ft MXL container", 15000, "available", "Mumbai", "WH-BOM", "Driver-TRK-101", 2.5, 88, 0),
    ("TRK-102", "Eicher 14ft", 4000, "available", "Delhi", "WH-DEL", "Driver-TRK-102", 9.5, 64, 0),
    ("TRK-103", "32ft SXL container", 7500, "on_route", "Mumbai", "WH-BOM", "Driver-TRK-103", 6.0, 52, 1),
    ("TRK-104", "Tata Ace", 750, "available", "Bengaluru", "WH-BLR", "Driver-TRK-104", 1.0, 91, 0),
    ("TRK-105", "Reefer 20ft (cold chain)", 5000, "available", "Hyderabad", "WH-HYD", "Driver-TRK-105", 3.0, 76, 1),
    ("TRK-106", "Tata Ace", 750, "on_route", "Bengaluru", "WH-BLR", "Driver-TRK-106", 4.5, 47, 0),
    ("TRK-107", "Tata 407", 2500, "available", "Chennai", "WH-MAA", "Driver-TRK-107", 0.0, 18, 0),
    ("TRK-108", "Eicher 14ft", 4000, "on_route", "Delhi", "WH-DEL", "Driver-TRK-108", 5.5, 58, 0),
    ("TRK-109", "Ashok Leyland Dost", 1250, "maintenance", "Pune", "WH-BOM", "Driver-TRK-109", 0.0, 35, 0),
    ("TRK-110", "Mahindra Bolero Pickup", 1500, "available", "Mumbai", "WH-BOM", "Driver-TRK-110", 7.0, 69, 0),
]
_STATE_CODES = {"Mumbai": "MH", "Delhi": "DL", "Bengaluru": "KA", "Hyderabad": "TS", "Chennai": "TN", "Pune": "MH"}

# id, name, modes, base_rate_per_km, per_kg_rate, reliability, avg_speed_kmph, max_weight_kg
_CARRIERS = [
    ("CR-01", "SwiftHaul Express", "road,air", 42.0, 4.5, 0.94, 52.0, 5000),
    ("CR-02", "Bharat RoadLink", "road", 32.0, 2.2, 0.87, 42.0, 20000),
    ("CR-03", "AeroCargo India", "air", 95.0, 11.0, 0.96, 380.0, 2000),
    ("CR-04", "TranzRail Freight", "rail,road", 22.0, 1.6, 0.83, 38.0, 30000),
    ("CR-05", "Deccan Coastal Logistics", "road", 30.0, 2.6, 0.90, 46.0, 12000),
]

_CUSTOMERS = [f"Customer-{i:03d}" for i in range(1, 26)]

# status, priority, origin, destination, contents, extra(dict)
_SHIPMENT_PLAN: list[tuple[str, str, str, str, str, dict[str, Any]]] = [
    ("in_transit", "critical", "Mumbai", "Delhi", "Insulin pens (cold chain)",
     {"weight": 340, "carrier": "CR-01", "vehicle": "TRK-103", "tight": True,
      "notes": "Cold chain 2-8C; temperature logger attached"}),
    ("delayed", "express", "Bengaluru", "Chennai", "Smartphones (4 pallets)",
     {"notes": "Heavy rain near Krishnagiri on NH-48; running behind schedule"}),
    ("delayed", "critical", "Delhi", "Jaipur", "Surgical gloves & PPE kits",
     {"notes": "Held at Shahjahanpur checkpost: e-way bill mismatch"}),
    ("exception", "express", "Pune", "Hyderabad", "LED panel lights",
     {"notes": "Consignee reported 3 cartons crushed at delivery attempt; POD photo pending review"}),
    ("out_for_delivery", "standard", "Chennai", "Bengaluru", "Cotton bedsheets",
     {"weight": 620, "vehicle": "TRK-106"}),
    ("delivered", "standard", "Mumbai", "Pune", "Packaged drinking water", {"delivered_ago": "today"}),
    ("created", "critical", "Hyderabad", "Visakhapatnam", "Lithium battery packs (UN3480)",
     {"weight": 1800, "notes": "Hazmat class 9: needs DG-certified driver"}),
    ("in_transit", "standard", "Kolkata", "Guwahati", "Assam CTC tea cartons", {}),
    ("picked_up", "express", "Chennai", "Kochi", "Two-wheeler tyres", {}),
    ("delayed", "standard", "Ahmedabad", "Mumbai", "PVC pipes",
     {"notes": "Truck breakdown near Vapi; replacement vehicle dispatched"}),
    ("delivered", "express", "Delhi", "Lucknow", "Laptops", {"delivered_ago": "today", "late": True}),
    ("in_transit", "express", "Hyderabad", "Bengaluru", "Ceiling fans", {}),
    ("out_for_delivery", "express", "Delhi", "Gurugram", "Copper wire coils",
     {"weight": 1450, "vehicle": "TRK-108"}),
    ("created", "standard", "Mumbai", "Surat", "Corrugated cartons", {}),
    ("exception", "standard", "Jaipur", "Indore", "Basmati rice bags",
     {"notes": "Address not found; consignee unreachable on phone (2 attempts)"}),
    ("in_transit", "standard", "Mumbai", "Kolkata", "OPC cement bags", {"weight": 1950}),
    ("delivered", "standard", "Bengaluru", "Coimbatore", "Stretch wrap rolls", {"delivered_ago": 30}),
    ("delayed", "express", "Chennai", "Hyderabad", "Nitrile surgical gloves",
     {"notes": "Port congestion at Chennai; pickup slipped"}),
    ("picked_up", "standard", "Delhi", "Nagpur", "Solar panels", {}),
    ("delivered", "critical", "Hyderabad", "Chennai", "Paracetamol strips", {"delivered_ago": 52}),
    ("in_transit", "standard", "Pune", "Ahmedabad", "Industrial ball bearings", {}),
    ("created", "express", "Bengaluru", "Hyderabad", "Lithium battery packs", {"weight": 1100}),
    ("out_for_delivery", "standard", "Delhi", "Noida", "Smartphones", {}),
    ("delivered", "standard", "Kolkata", "Bhubaneswar", "Assam CTC tea cartons", {"delivered_ago": 26}),
    ("delivered", "express", "Mumbai", "Bengaluru", "LED panel lights", {"delivered_ago": 75}),
]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", ".", name.lower().replace("pvt ltd", "").replace("llp", "")).strip(".")


def _phone(rng: random.Random) -> str:
    return f"+91 9{rng.randint(1000, 9999)}0 {rng.randint(10000, 99999)}"


def _waypoint(origin: str, dest: str, progress: float) -> str:
    total = haversine_km(origin, dest)
    best, best_score = origin, 1e9
    for c in CITIES:
        if c in (origin, dest):
            continue
        d1, d2 = haversine_km(origin, c), haversine_km(c, dest)
        if (d1 + d2) / max(total, 1) > 1.25:
            continue
        score = abs(d1 / max(total, 1) - progress)
        if score < best_score:
            best, best_score = c, score
    return best


def _seed(conn: sqlite3.Connection) -> None:
    rng = random.Random(42)
    now = _now()
    ins = conn.execute

    for wid, name, city, addr, cap, mgr, cold in _WAREHOUSES:
        lat, lon = CITIES[city]
        utilisation = round(rng.uniform(58, 91), 1)
        _phone(rng)  # Preserve the seeded business RNG sequence, without storing contacts.
        ins("INSERT INTO warehouses VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (wid, name, city, "", lat, lon, cap, utilisation, f"Manager-{wid}", "", cold))

    wh_ids = [w[0] for w in _WAREHOUSES]
    for sku, name, cat, cost, supplier, base_rp, rq in _SKUS:
        whs = set(rng.sample(wh_ids, rng.randint(2, 3)))
        whs |= {w for (s, w) in _FORCED_LOW if s == sku}
        for wid in sorted(whs):
            rp = max(10, int(base_rp * rng.uniform(0.8, 1.2)))
            if (sku, wid) in _FORCED_LOW:
                qty = int(rp * rng.uniform(0.1, 0.6))
            elif rng.random() < 0.08:
                qty = int(rp * rng.uniform(0.6, 0.95))
            else:
                qty = int(rp * rng.uniform(1.3, 4.0))
            ins("INSERT INTO inventory VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sku, wid, name, cat, qty, rp, rq, cost, supplier,
                 _iso(now - timedelta(hours=rng.uniform(0.5, 30)))))

    for vid, vtype, cap, status, city, home, driver, hrs, fuel, reefer in _VEHICLES:
        _registration = f"{_STATE_CODES.get(city, 'MH')}-{rng.randint(1, 48):02d}-" \
              f"{chr(65 + rng.randint(0, 25))}{chr(65 + rng.randint(0, 25))}-{rng.randint(1000, 9999)}"
        _phone(rng)
        ins("INSERT INTO vehicles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (vid, vtype, cap, status, city, home, f"Driver-{vid}", "", hrs, fuel, reefer, ""))

    carrier_speed: dict[str, float] = {}
    for cid, name, modes, base, perkg, rel, speed, maxw in _CARRIERS:
        carrier_speed[cid] = speed
        ins("INSERT INTO carriers VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, name, modes, base, perkg, rel, speed, maxw, ""))

    customers = _CUSTOMERS[:]
    rng.shuffle(customers)
    vehicle_city: dict[str, str] = {}
    hours_since_midnight = (now - now.replace(hour=0, minute=0, second=0)).total_seconds() / 3600

    def u(a: float, b: float) -> timedelta:
        return timedelta(hours=rng.uniform(a, b))

    for i, (status, prio, origin, dest, contents, extra) in enumerate(_SHIPMENT_PLAN):
        sid, oid = f"SHP-{1001 + i}", f"ORD-{5001 + i}"
        customer = customers[i % len(customers)]
        email = ""
        weight = float(extra.get("weight") or round(math.exp(rng.uniform(math.log(5), math.log(2000))), 1))
        gc_km = haversine_km(origin, dest)
        if "carrier" in extra:
            carrier = extra["carrier"]
        elif prio == "critical":
            carrier = "CR-03" if gc_km > 800 and weight <= 2000 else "CR-01"
        elif prio == "express":
            carrier = rng.choice(["CR-01", "CR-05"])
        else:
            carrier = rng.choice(["CR-02", "CR-04", "CR-05"])
        speed = carrier_speed[carrier]
        transit = timedelta(hours=(gc_km * (1.0 if carrier == "CR-03" else ROAD_FACTOR)) / speed
                            + (6 if carrier == "CR-03" else 3))
        vehicle = extra.get("vehicle")
        last_city = origin
        events: list[tuple[str, str, str]] = []  # (status, city, note) - timestamps assigned later
        cname = next(c[1] for c in _CARRIERS if c[0] == carrier)

        if status == "created":
            created = now - u(0.5, 5)
            last_upd = created
            eta = now + u(3, 8) + transit
            promised = eta + (u(1, 4) if prio == "critical" else u(4, 24))
        elif status == "picked_up":
            created = now - u(8, 20)
            last_upd = now - u(0.5, 3)
            eta = now + transit + u(1, 3)
            promised = eta + u(3, 18)
        elif status in ("in_transit", "delayed"):
            p = rng.uniform(0.3, 0.75)
            start = now - transit * p
            created = start - u(4, 12)
            last_upd = now - u(0.2, 2.5)
            last_city = _waypoint(origin, dest, p)
            if status == "in_transit":
                eta = now + transit * (1 - p)
                promised = eta + (timedelta(hours=1.5) if extra.get("tight") else u(2, 14))
            else:
                delay = u(5, 14)
                eta = now + transit * (1 - p) + delay
                promised = eta - delay + u(-3, 1)
        elif status == "out_for_delivery":
            created = now - transit - u(8, 24)
            last_upd = now - u(0.3, 2)
            last_city = dest
            eta = now + u(1, 5)
            promised = eta + u(0.5, 6)
        elif status == "delivered":
            ago = extra.get("delivered_ago")
            if ago == "today":
                ago_h = min(rng.uniform(1, 4), max(hours_since_midnight * 0.8, 0.05))
            else:
                ago_h = float(ago)
            delivered_at = now - timedelta(hours=ago_h)
            created = delivered_at - transit - u(6, 20)
            last_upd = eta = delivered_at
            last_city = dest
            promised = delivered_at - u(2, 5) if extra.get("late") else delivered_at + u(1, 12)
        else:  # exception
            created = now - transit - u(12, 30)
            last_upd = now - u(1, 6)
            last_city = dest
            eta = now + u(12, 36)
            promised = now + u(-8, 4)

        # event trail
        events.append(("created", origin, f"Order {oid} booked; manifest generated"))
        if status != "created":
            events.append(("picked_up", origin, f"Picked up from {origin} dock; {rng.randint(1, 40)} packages scanned"))
        if status in ("in_transit", "delayed", "out_for_delivery", "delivered", "exception"):
            events.append(("in_transit", origin, f"Departed {origin} hub with {cname}"))
            if status in ("in_transit", "delayed") and last_city != origin:
                events.append(("in_transit", last_city, f"Crossed {last_city} checkpoint"))
        if status == "delayed":
            events.append(("delayed", last_city, extra.get("notes", "Running behind schedule")))
        if status in ("out_for_delivery", "delivered", "exception"):
            events.append(("in_transit", dest, f"Arrived at {dest} delivery hub"))
            who = f"vehicle {vehicle}" if vehicle else f"{cname} last-mile partner"
            events.append(("out_for_delivery", dest, f"Out for delivery with {who}"))
        if status == "delivered":
            events.append(("delivered", dest, "Delivered; POD signed by receiver"))
        if status == "exception":
            events.append(("exception", dest, extra.get("notes", "Delivery exception")))

        span = (last_upd - created) / max(len(events) - 1, 1)
        for k, (est, ecity, note) in enumerate(events):
            ins("INSERT INTO shipment_events(shipment_id, ts, status, city, note, source) VALUES (?,?,?,?,?,?)",
                (sid, _iso(created + span * k), est, ecity, note, "seed"))

        if vehicle:
            vehicle_city[vehicle] = last_city
        _phone(rng)
        ins("INSERT INTO shipments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, oid, customer, email, "", origin, dest, status, prio, weight, carrier,
             vehicle, _iso(eta), _iso(promised), _iso(last_upd), last_city, extra.get("notes", ""),
             contents, _iso(created)))

    for vid, city in vehicle_city.items():
        ins("UPDATE vehicles SET current_city=?, status='on_route' WHERE id=?", (city, vid))

    # Purchase orders: one already open for a low-stock line (exercises idempotent reorder).
    pos = [
        ("PO-7001", "SKU-103", "WH-MAA", 300, "open", -20, 52, "Auto-raised: below reorder point"),
        ("PO-7002", "SKU-112", "WH-BOM", 1500, "approved", -40, 30, "Monsoon demand buffer"),
        ("PO-7003", "SKU-116", "WH-DEL", 8000, "shipped", -70, 10, "Festive season packaging"),
        ("PO-7004", "SKU-101", "WH-BLR", 1200, "received", -200, -50, "Quarterly replenishment"),
    ]
    sku_meta = {s[0]: s for s in _SKUS}
    for pid, sku, wid, qty, st, created_h, expected_h, note in pos:
        cost = sku_meta[sku][3]
        ins("INSERT INTO purchase_orders VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pid, sku, wid, qty, sku_meta[sku][4], st, cost, round(cost * qty, 2),
             _iso(now + timedelta(hours=created_h)), _iso(now + timedelta(hours=expected_h)), note))

    ins("INSERT OR REPLACE INTO cm_meta(key, value) VALUES('seeded_at', ?)", (_iso(now),))
    ins("INSERT OR REPLACE INTO cm_meta(key, value) VALUES('clock_anchor', ?)", (_iso(now),))


# --------------------------------------------------------------------------- shipments
def _carrier_names() -> dict[str, str]:
    return {r["id"]: r["name"] for r in _q("SELECT id, name FROM carriers")}


def _enrich_shipment(s: dict[str, Any], carriers: dict[str, str] | None = None) -> dict[str, Any]:
    carriers = carriers if carriers is not None else _carrier_names()
    now = _now()
    eta, promised = _parse(s.get("eta")), _parse(s.get("promised_by"))
    late = bool(eta and promised and eta > promised) or s.get("status") == "delayed"
    s["carrier_name"] = carriers.get(s.get("carrier_id") or "", None)
    s["is_late"] = late
    s["delay_hours"] = max(0.0, _hours(eta - promised)) if eta and promised else 0.0
    s["hours_to_promise"] = _hours(promised - now) if promised and s.get("status") in ACTIVE_STATUSES else None
    try:
        s["distance_km"] = round(haversine_km(s["origin_city"], s["destination_city"]) * ROAD_FACTOR, 0)
    except (ValueError, KeyError, TypeError):
        s["distance_km"] = None
    return s


def get_shipment(shipment_id: str) -> dict[str, Any]:
    """Return one shipment (flat row + carrier_name, is_late, delay_hours, hours_to_promise, distance_km)."""
    sid = _norm_id(shipment_id, "SHP")
    row = _q1("SELECT * FROM shipments WHERE id=?", (sid,))
    if row is None:
        return _not_found("Shipment", shipment_id, _ids("shipments"),
                          "Shipment IDs look like SHP-1001; use list_shipments to browse.")
    return _enrich_shipment(row)


def list_shipments(
    status: str | None = None,
    city: str | None = None,
    priority: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List shipments filtered by status ('active' = not delivered/exception), city (origin,
    destination or current) and priority. Critical first, then earliest promise."""
    where, params = [], []
    if status:
        st = _normalise_status(status)
        if st == "active":
            where.append(f"status IN ({','.join('?' * len(ACTIVE_STATUSES))})")
            params.extend(ACTIVE_STATUSES)
        elif st in SHIPMENT_STATUSES:
            where.append("status=?")
            params.append(st)
        else:
            return _err(f"Unknown status {status!r}", ["active", *SHIPMENT_STATUSES])
    if city:
        c = _city_or_raw(city)
        where.append("(origin_city=? OR destination_city=? OR last_city=?)")
        params.extend([c, c, c])
    if priority:
        pr = str(priority).strip().lower()
        if pr not in PRIORITIES:
            return _err(f"Unknown priority {priority!r}", list(PRIORITIES))
        where.append("priority=?")
        params.append(pr)
    try:
        lim = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        lim = 50
    sql = "SELECT * FROM shipments"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += (" ORDER BY CASE status WHEN 'delivered' THEN 1 ELSE 0 END,"
            " CASE priority WHEN 'critical' THEN 0 WHEN 'express' THEN 1 ELSE 2 END,"
            " promised_by LIMIT ?")
    rows = _q(sql, [*params, lim])
    carriers = _carrier_names()
    return {"status": "ok", "count": len(rows), "shipments": [_enrich_shipment(r, carriers) for r in rows]}


def _normalise_status(status: str) -> str:
    s = re.sub(r"[\s\-]+", "_", str(status or "").strip().lower())
    return _STATUS_ALIASES.get(s.replace("_", ""), _STATUS_ALIASES.get(s, s))


def update_shipment_status(
    shipment_id: str,
    status: str,
    city: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    """Set a shipment's status (validated), update last_city/last_update and append a history event."""
    new = _normalise_status(status)
    if new not in SHIPMENT_STATUSES:
        ranked = difflib.get_close_matches(new, SHIPMENT_STATUSES, n=7, cutoff=0.0)
        return _err(f"Invalid status {status!r}. Valid: {', '.join(SHIPMENT_STATUSES)}", ranked)
    sid = _norm_id(shipment_id, "SHP")
    with _tx() as conn:
        row = conn.execute("SELECT * FROM shipments WHERE id=?", (sid,)).fetchone()
        if row is None:
            ids = [r["id"] for r in conn.execute("SELECT id FROM shipments ORDER BY id").fetchall()]
            return _not_found("Shipment", shipment_id, ids, "Shipment IDs look like SHP-1001.")
        old = row["status"]
        if old == "delivered" and new not in ("delivered", "exception"):
            return _err(f"{sid} is already delivered; only 'exception' (post-delivery issue) is allowed.",
                        ["exception"], shipment_id=sid)
        ts = _now()
        loc = _city_or_raw(city) or (row["destination_city"] if new in ("out_for_delivery", "delivered")
                                     else row["last_city"])
        eta = _iso(ts) if new == "delivered" else row["eta"]
        notes = note.strip() if note and note.strip() else row["notes"]
        conn.execute(
            "UPDATE shipments SET status=?, last_city=?, last_update=?, eta=?, notes=? WHERE id=?",
            (new, loc, _iso(ts), eta, notes, sid),
        )
        ev_note = note.strip() or f"Status changed {old} -> {new}"
        cur = conn.execute(
            "INSERT INTO shipment_events(shipment_id, ts, status, city, note, source) VALUES (?,?,?,?,?,?)",
            (sid, _iso(ts), new, loc, ev_note, "api"),
        )
        event = {"id": cur.lastrowid, "shipment_id": sid, "ts": _iso(ts), "status": new, "city": loc,
                 "note": ev_note, "source": "api"}
        if new == "delivered" and row["vehicle_id"]:
            _release_vehicle_if_idle(conn, row["vehicle_id"], exclude=sid)
    return {"status": "ok", "previous_status": old, "shipment": get_shipment(sid), "event": event}


def shipment_history(shipment_id: str) -> dict[str, Any]:
    """Chronological event trail for a shipment."""
    sid = _norm_id(shipment_id, "SHP")
    if _q1("SELECT id FROM shipments WHERE id=?", (sid,)) is None:
        return _not_found("Shipment", shipment_id, _ids("shipments"), "Shipment IDs look like SHP-1001.")
    events = _q("SELECT * FROM shipment_events WHERE shipment_id=? ORDER BY ts, id", (sid,))
    return {"status": "ok", "shipment_id": sid, "count": len(events), "events": events}


# --------------------------------------------------------------------------- inventory
def _resolve_warehouse(raw: Any) -> str | None:
    s = str(raw or "").strip()
    if not s:
        return None
    ids = _ids("warehouses")
    cand = _norm_id(s, "WH")
    if cand in ids:
        return cand
    if f"WH-{cand}" in ids:
        return f"WH-{cand}"
    city = resolve_city(s)
    if city:
        row = _q1("SELECT id FROM warehouses WHERE city=?", (city,))
        if row:
            return row["id"]
    return None


def _resolve_sku(raw: Any) -> list[str]:
    """SKU id ('SKU-107', '107') or product-name fragment ('insulin') -> matching SKU ids."""
    s = str(raw or "").strip()
    if not s:
        return []
    cand = _norm_id(s, "SKU")
    if _q1("SELECT sku FROM inventory WHERE sku=? LIMIT 1", (cand,)):
        return [cand]
    rows = _q("SELECT DISTINCT sku FROM inventory WHERE lower(name) LIKE ? OR lower(category)=? ORDER BY sku",
              (f"%{s.lower()}%", s.lower()))
    return [r["sku"] for r in rows]


def _sku_suggestions(raw: str) -> list[str]:
    rows = _q("SELECT DISTINCT sku, name FROM inventory ORDER BY sku")
    by_name = difflib.get_close_matches(raw.lower(), [r["name"].lower() for r in rows], n=3, cutoff=0.4)
    out = [r["sku"] for r in rows if r["name"].lower() in by_name]
    return out or _closest(raw, [r["sku"] for r in rows])


def _inventory_row(r: dict[str, Any], open_pos: dict[tuple[str, str], str], wh_city: dict[str, str]) -> dict:
    r["warehouse_city"] = wh_city.get(r["warehouse_id"])
    r["low_stock"] = r["qty_on_hand"] <= r["reorder_point"]
    r["shortfall"] = max(0, r["reorder_point"] - r["qty_on_hand"])
    r["open_po"] = open_pos.get((r["sku"], r["warehouse_id"]))
    return r


def _open_po_map() -> dict[tuple[str, str], str]:
    rows = _q(f"SELECT id, sku, warehouse_id FROM purchase_orders WHERE status IN "
              f"({','.join('?' * len(PO_OPEN_STATUSES))}) ORDER BY id", PO_OPEN_STATUSES)
    return {(r["sku"], r["warehouse_id"]): r["id"] for r in rows}


def get_inventory(
    sku: str | None = None,
    warehouse_id: str | None = None,
    low_stock_only: bool = False,
) -> dict[str, Any]:
    """Stock lines (sku x warehouse) with low_stock flag, shortfall and any open PO id.

    ``sku`` accepts an id ('SKU-107', '107') or product-name fragment ('insulin');
    ``warehouse_id`` accepts 'WH-BLR', 'BLR' or a city ('Bangalore')."""
    where, params = [], []
    if sku:
        skus = _resolve_sku(sku)
        if not skus:
            return _err(f"SKU {sku!r} not found", _sku_suggestions(str(sku)),
                        hint="SKU ids look like SKU-101; product-name fragments also work.")
        where.append(f"sku IN ({','.join('?' * len(skus))})")
        params.extend(skus)
    if warehouse_id:
        wid = _resolve_warehouse(warehouse_id)
        if not wid:
            return _not_found("Warehouse", warehouse_id, _ids("warehouses"),
                              "Warehouse ids: WH-BOM, WH-DEL, WH-BLR, WH-MAA, WH-HYD (or a city name).")
        where.append("warehouse_id=?")
        params.append(wid)
    if low_stock_only:
        where.append("qty_on_hand <= reorder_point")
    sql = "SELECT * FROM inventory" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY (qty_on_hand * 1.0 / reorder_point), sku, warehouse_id"
    rows = _q(sql, params)
    open_pos = _open_po_map()
    wh_city = {w["id"]: w["city"] for w in _q("SELECT id, city FROM warehouses")}
    items = [_inventory_row(r, open_pos, wh_city) for r in rows]
    return {"status": "ok", "count": len(items), "low_stock_count": sum(i["low_stock"] for i in items),
            "items": items}


def create_reorder(sku: str, warehouse_id: str, qty: int | None = None) -> dict[str, Any]:
    """Raise a purchase order for a stock line. Idempotent: an existing open PO is returned
    (``duplicate=True``) instead of creating a second one. Default qty tops stock up to
    2x the reorder point (at least the line's standard reorder quantity)."""
    skus = _resolve_sku(sku)
    if not skus:
        return _err(f"SKU {sku!r} not found", _sku_suggestions(str(sku)))
    if len(skus) > 1:
        return _err(f"SKU {sku!r} is ambiguous; pick one", skus)
    sku_id = skus[0]
    wid = _resolve_warehouse(warehouse_id)
    if not wid:
        return _not_found("Warehouse", warehouse_id, _ids("warehouses"),
                          "Warehouse ids: WH-BOM, WH-DEL, WH-BLR, WH-MAA, WH-HYD (or a city name).")
    if qty is not None:
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            return _err(f"qty must be a positive integer, got {qty!r}")
        if qty <= 0 or qty > 1_000_000:
            return _err(f"qty must be between 1 and 1,000,000, got {qty}")
    with _tx() as conn:
        line = conn.execute("SELECT * FROM inventory WHERE sku=? AND warehouse_id=?", (sku_id, wid)).fetchone()
        if line is None:
            stocked = [r["warehouse_id"] for r in
                       conn.execute("SELECT warehouse_id FROM inventory WHERE sku=?", (sku_id,)).fetchall()]
            return _err(f"{sku_id} is not stocked at {wid}", stocked,
                        hint=f"{sku_id} is stocked at: {', '.join(stocked)}")
        existing = conn.execute(
            f"SELECT * FROM purchase_orders WHERE sku=? AND warehouse_id=? AND status IN "
            f"({','.join('?' * len(PO_OPEN_STATUSES))}) ORDER BY id LIMIT 1",
            (sku_id, wid, *PO_OPEN_STATUSES),
        ).fetchone()
        if existing:
            return {"status": "ok", "duplicate": True, "purchase_order": existing,
                    "message": f"{existing['id']} is already {existing['status']} for {sku_id} at {wid}; "
                               "no new PO raised."}
        order_qty = qty or max(line["reorder_qty"], 2 * line["reorder_point"] - line["qty_on_hand"])
        last = conn.execute("SELECT id FROM purchase_orders ORDER BY id DESC LIMIT 1").fetchone()
        next_num = int(last["id"].split("-")[1]) + 1 if last else 7001
        pid = f"PO-{next_num}"
        now = _now()
        lead_days = 2 + (sum(map(ord, sku_id + wid)) % 4)
        po = {
            "id": pid, "sku": sku_id, "warehouse_id": wid, "qty": order_qty, "supplier": line["supplier"],
            "status": "open", "unit_cost_inr": line["unit_cost_inr"],
            "total_cost_inr": round((line["unit_cost_inr"] or 0) * order_qty, 2),
            "created_at": _iso(now), "expected_by": _iso(now + timedelta(days=lead_days)),
            "note": "Raised by CargoMind",
        }
        conn.execute("INSERT INTO purchase_orders VALUES (:id,:sku,:warehouse_id,:qty,:supplier,:status,"
                     ":unit_cost_inr,:total_cost_inr,:created_at,:expected_by,:note)", po)
    return {"status": "ok", "duplicate": False, "purchase_order": po,
            "message": f"Raised {pid}: {order_qty} x {line['name']} for {wid}, expected in {lead_days} days."}


def list_purchase_orders(status: str | None = None) -> dict[str, Any]:
    """Purchase orders, newest first. ``status='open'`` returns all not-yet-received POs."""
    if status and status.lower() == "open":
        rows = _q(f"SELECT * FROM purchase_orders WHERE status IN ({','.join('?' * len(PO_OPEN_STATUSES))}) "
                  "ORDER BY id DESC", PO_OPEN_STATUSES)
    elif status:
        rows = _q("SELECT * FROM purchase_orders WHERE status=? ORDER BY id DESC", (status.lower(),))
    else:
        rows = _q("SELECT * FROM purchase_orders ORDER BY id DESC")
    return {"status": "ok", "count": len(rows), "purchase_orders": rows}


# --------------------------------------------------------------------------- fleet
def _vehicle_loads() -> dict[str, dict[str, Any]]:
    rows = _q(f"SELECT vehicle_id, id, weight_kg FROM shipments WHERE vehicle_id IS NOT NULL AND status IN "
              f"({','.join('?' * len(ACTIVE_STATUSES))})", ACTIVE_STATUSES)
    loads: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = loads.setdefault(r["vehicle_id"], {"load": 0.0, "shipments": []})
        d["load"] += r["weight_kg"] or 0
        d["shipments"].append(r["id"])
    return loads


def _enrich_vehicle(v: dict[str, Any], loads: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ld = loads.get(v["id"], {"load": 0.0, "shipments": []})
    v["current_load_kg"] = round(ld["load"], 1)
    v["remaining_capacity_kg"] = round(v["capacity_kg"] - ld["load"], 1)
    v["assigned_shipments"] = ld["shipments"]
    v["refrigerated"] = bool(v.get("refrigerated"))
    v["driver_hours_remaining"] = round(max(0.0, MAX_DRIVER_HOURS - (v["driver_hours_today"] or 0)), 1)
    return v


def get_vehicle(vehicle_id: str) -> dict[str, Any]:
    """One vehicle with load, remaining capacity, assigned shipments and driver hours remaining."""
    vid = _norm_id(vehicle_id, "TRK")
    row = _q1("SELECT * FROM vehicles WHERE id=?", (vid,))
    if row is None:
        return _not_found("Vehicle", vehicle_id, _ids("vehicles"), "Vehicle IDs look like TRK-101.")
    return _enrich_vehicle(row, _vehicle_loads())


def list_vehicles(status: str | None = None, city: str | None = None) -> dict[str, Any]:
    """Fleet list filtered by status (available|on_route|maintenance) and current city."""
    where, params = [], []
    if status:
        st = re.sub(r"[\s\-]+", "_", str(status).strip().lower())
        if st not in VEHICLE_STATUSES:
            return _err(f"Unknown vehicle status {status!r}", list(VEHICLE_STATUSES))
        where.append("status=?")
        params.append(st)
    if city:
        where.append("current_city=?")
        params.append(_city_or_raw(city))
    rows = _q("SELECT * FROM vehicles" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id",
              params)
    loads = _vehicle_loads()
    return {"status": "ok", "count": len(rows), "vehicles": [_enrich_vehicle(r, loads) for r in rows]}


def _release_vehicle_if_idle(conn: sqlite3.Connection, vehicle_id: str, exclude: str) -> None:
    busy = conn.execute(
        f"SELECT COUNT(*) AS n FROM shipments WHERE vehicle_id=? AND id<>? AND status IN "
        f"({','.join('?' * len(ACTIVE_STATUSES))})", (vehicle_id, exclude, *ACTIVE_STATUSES),
    ).fetchone()["n"]
    if not busy:
        conn.execute("UPDATE vehicles SET status='available' WHERE id=? AND status='on_route'", (vehicle_id,))


def assign_vehicle(shipment_id: str, vehicle_id: str) -> dict[str, Any]:
    """Assign an own-fleet vehicle to a shipment (capacity-checked, idempotent).

    Rejects vehicles in maintenance, delivered/exception shipments and overloads. Returns
    ``warnings`` for soft issues (vehicle in another city, driver near hours limit, low fuel)."""
    sid = _norm_id(shipment_id, "SHP")
    vid = _norm_id(vehicle_id, "TRK")
    with _tx() as conn:
        s = conn.execute("SELECT * FROM shipments WHERE id=?", (sid,)).fetchone()
        if s is None:
            ids = [r["id"] for r in conn.execute("SELECT id FROM shipments ORDER BY id").fetchall()]
            return _not_found("Shipment", shipment_id, ids, "Shipment IDs look like SHP-1001.")
        v = conn.execute("SELECT * FROM vehicles WHERE id=?", (vid,)).fetchone()
        if v is None:
            ids = [r["id"] for r in conn.execute("SELECT id FROM vehicles ORDER BY id").fetchall()]
            return _not_found("Vehicle", vehicle_id, ids, "Vehicle IDs look like TRK-101.")
        if s["vehicle_id"] == vid:
            return {"status": "ok", "already_assigned": True, "shipment_id": sid, "vehicle_id": vid,
                    "warnings": [], "message": f"{sid} is already assigned to {vid}."}
        if s["status"] not in ACTIVE_STATUSES:
            return _err(f"{sid} is {s['status']}; only active shipments can be assigned a vehicle.")
        if v["status"] == "maintenance":
            alts = conn.execute("SELECT id FROM vehicles WHERE status='available' AND current_city=? "
                                "AND capacity_kg>=? ORDER BY capacity_kg", (v["current_city"], s["weight_kg"])
                                ).fetchall()
            return _err(f"{vid} is under maintenance.", [a["id"] for a in alts])
        load = conn.execute(
            f"SELECT COALESCE(SUM(weight_kg),0) AS w FROM shipments WHERE vehicle_id=? AND status IN "
            f"({','.join('?' * len(ACTIVE_STATUSES))})", (vid, *ACTIVE_STATUSES),
        ).fetchone()["w"]
        remaining = v["capacity_kg"] - load
        if (s["weight_kg"] or 0) > remaining:
            alts = conn.execute(
                "SELECT id FROM vehicles WHERE status='available' AND capacity_kg>=? "
                "ORDER BY (current_city=?) DESC, capacity_kg LIMIT 3", (s["weight_kg"], s["origin_city"]),
            ).fetchall()
            return _err(f"{vid} ({v['type']}) has {remaining:.0f} kg free; {sid} weighs {s['weight_kg']:.0f} kg.",
                        [a["id"] for a in alts], remaining_capacity_kg=round(remaining, 1))
        warnings = []
        here = s["last_city"] or s["origin_city"]
        if v["current_city"] != here:
            warnings.append(f"{vid} is in {v['current_city']}, shipment is at {here}.")
        if (v["driver_hours_today"] or 0) >= MAX_DRIVER_HOURS - 1.5:
            warnings.append(f"Driver {v['driver_name']} has {v['driver_hours_today']}h on duty today "
                            f"(limit {MAX_DRIVER_HOURS:.0f}h).")
        if (v["fuel_pct"] or 0) < 25:
            warnings.append(f"{vid} fuel at {v['fuel_pct']:.0f}%; refuel before departure.")
        if "cold chain" in (s["contents"] or "").lower() and not v["refrigerated"]:
            warnings.append(f"{sid} needs cold chain but {vid} is not refrigerated.")
        prev = s["vehicle_id"]
        ts = _iso(_now())
        conn.execute("UPDATE shipments SET vehicle_id=?, last_update=? WHERE id=?", (vid, ts, sid))
        conn.execute("UPDATE vehicles SET status='on_route' WHERE id=?", (vid,))
        conn.execute(
            "INSERT INTO shipment_events(shipment_id, ts, status, city, note, source) VALUES (?,?,?,?,?,?)",
            (sid, ts, s["status"], here, f"Assigned to {vid} ({v['type']}, driver {v['driver_name']})", "api"),
        )
        if prev:
            _release_vehicle_if_idle(conn, prev, exclude=sid)
    return {"status": "ok", "already_assigned": False, "shipment_id": sid, "vehicle_id": vid,
            "previous_vehicle_id": prev, "warnings": warnings, "vehicle": get_vehicle(vid),
            "message": f"Assigned {sid} ({s['weight_kg']:.0f} kg) to {vid}."}


# --------------------------------------------------------------------------- carriers / warehouses
def _carrier_row(r: dict[str, Any]) -> dict[str, Any]:
    r["modes"] = [m for m in (r.get("modes") or "").split(",") if m]
    return r


def list_carriers() -> dict[str, Any]:
    """Carrier partners with modes, INR rate card (base_rate_per_km, per_kg_rate), reliability, speed."""
    rows = [_carrier_row(r) for r in _q("SELECT * FROM carriers ORDER BY id")]
    return {"status": "ok", "count": len(rows), "carriers": rows}


def get_carrier(carrier_id: str) -> dict[str, Any]:
    """One carrier by id (CR-01) or name."""
    cid = _norm_id(carrier_id, "CR", width=2)
    row = _q1("SELECT * FROM carriers WHERE id=? OR lower(name)=lower(?)", (cid, str(carrier_id).strip()))
    if row is None:
        return _not_found("Carrier", carrier_id, _ids("carriers"), "Carrier IDs look like CR-01.")
    return _carrier_row(row)


def list_warehouses() -> dict[str, Any]:
    """Warehouses with location, utilisation, SKU-line count and low-stock count."""
    rows = _q(
        "SELECT w.*, COUNT(i.sku) AS sku_lines, "
        "COALESCE(SUM(CASE WHEN i.qty_on_hand <= i.reorder_point THEN 1 ELSE 0 END),0) AS low_stock_lines "
        "FROM warehouses w LEFT JOIN inventory i ON i.warehouse_id = w.id GROUP BY w.id ORDER BY w.id"
    )
    for r in rows:
        r["cold_chain"] = bool(r["cold_chain"])
    return {"status": "ok", "count": len(rows), "warehouses": rows}


def get_warehouse(warehouse_id: str) -> dict[str, Any]:
    """One warehouse by id ('WH-BLR'), code ('BLR') or city ('Bangalore')."""
    wid = _resolve_warehouse(warehouse_id)
    if not wid:
        return _not_found("Warehouse", warehouse_id, _ids("warehouses"), "Warehouse ids look like WH-BOM.")
    return next(w for w in list_warehouses()["warehouses"] if w["id"] == wid)


# --------------------------------------------------------------------------- KPIs
def kpis() -> dict[str, Any]:
    """Network KPIs: on_time_rate (% of non-exception shipments on/ahead of promise), in_transit,
    delayed, exceptions, delivered_today, low_stock_skus, fleet_utilization (% vehicles on route),
    open_pos, plus by_status counts and critical_active."""
    now = _now()
    ships = _q("SELECT status, priority, eta, promised_by FROM shipments")
    by_status = {s: 0 for s in SHIPMENT_STATUSES}
    on_time = considered = delayed = delivered_today = critical_active = 0
    for s in ships:
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1
        eta, promised = _parse(s["eta"]), _parse(s["promised_by"])
        late = s["status"] == "delayed" or bool(eta and promised and eta > promised)
        if s["status"] != "exception":
            considered += 1
            on_time += 0 if late else 1
        if s["status"] in ACTIVE_STATUSES and late:
            delayed += 1
        if s["status"] == "delivered" and eta and eta.astimezone(IST).date() == now.date():
            delivered_today += 1
        if s["status"] in ACTIVE_STATUSES and s["priority"] == "critical":
            critical_active += 1
    vehicles = _q("SELECT status FROM vehicles")
    on_route = sum(1 for v in vehicles if v["status"] == "on_route")
    low = _q1("SELECT COUNT(*) AS n FROM inventory WHERE qty_on_hand <= reorder_point")["n"]
    open_pos = _q1(f"SELECT COUNT(*) AS n FROM purchase_orders WHERE status IN "
                   f"({','.join('?' * len(PO_OPEN_STATUSES))})", PO_OPEN_STATUSES)["n"]
    return {
        "status": "ok",
        "generated_at": _iso(now),
        "on_time_rate": round(100.0 * on_time / considered, 1) if considered else 100.0,
        "in_transit": sum(by_status[s] for s in MOVING_STATUSES),
        "delayed": delayed,
        "exceptions": by_status["exception"],
        "delivered_today": delivered_today,
        "low_stock_skus": low,
        "fleet_utilization": round(100.0 * on_route / len(vehicles), 1) if vehicles else 0.0,
        "open_pos": open_pos,
        "critical_active": critical_active,
        "total_shipments": len(ships),
        "fleet": {st: sum(1 for v in vehicles if v["status"] == st) for st in VEHICLE_STATUSES},
        "by_status": by_status,
    }


# --------------------------------------------------------------------------- outbox
def add_outbox(
    shipment_id: str = "",
    channel: str = "email",
    message: str = "",
    recipient: str = "",
    subject: str = "",
    status: str = "sent",
    **meta: Any,
) -> dict[str, Any]:
    """Record a (simulated) outbound customer notification. If ``recipient`` is empty it is
    looked up from the shipment (email for channel=email, phone otherwise)."""
    sid = _norm_id(shipment_id, "SHP") if shipment_id else ""
    ch = (channel or "email").strip().lower()
    if not recipient and sid:
        s = _q1("SELECT customer_email, customer_phone FROM shipments WHERE id=?", (sid,))
        if s:
            recipient = s["customer_email"] if ch == "email" else s["customer_phone"]
    ts = _iso(_now())
    with _tx() as conn:
        cur = conn.execute(
            "INSERT INTO outbox(ts, shipment_id, channel, recipient, subject, message, status, meta) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ts, sid, ch, recipient, subject, message, status, json.dumps(meta, default=str) if meta else None),
        )
        oid = cur.lastrowid
    entry = {"id": oid, "ts": ts, "shipment_id": sid, "channel": ch, "recipient": recipient,
             "subject": subject, "message": message, "status": status, "meta": meta or None}
    return {"status": "ok", "outbox_id": oid, "entry": entry}


def list_outbox(limit: int = 20) -> dict[str, Any]:
    """Most recent simulated notifications, newest first."""
    try:
        lim = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        lim = 20
    rows = _q("SELECT * FROM outbox ORDER BY id DESC LIMIT ?", (lim,))
    for r in rows:
        if r.get("meta"):
            try:
                r["meta"] = json.loads(r["meta"])
            except ValueError:
                pass
    return {"status": "ok", "count": len(rows), "messages": rows}
