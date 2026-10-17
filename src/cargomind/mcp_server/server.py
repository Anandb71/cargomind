"""CargoMind logistics MCP server.

Exposes the operational SQLite store (shipments, fleet, carriers, warehouses, inventory, KPIs)
as MCP tools + resources over stdio, so the ADK agents (via ``McpToolset``), Claude Desktop or
any other MCP client can use it::

    python -m cargomind.mcp_server.server          # or: uv run python -m cargomind.mcp_server.server

The database path comes from ``CARGOMIND_DB`` (see ``cargomind.config``). Every tool is also a
plain module-level function so callers can use them in-process when the MCP transport is down.
stdout carries the JSON-RPC protocol: all logging goes to stderr.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer  # mcp>=2: FastMCP was renamed MCPServer

from cargomind.data import db

try:  # fault injection for the self-healing demo (no-op when the module or rate is absent)
    from cargomind.healing.chaos import maybe_fail
except Exception:  # noqa: BLE001  # pragma: no cover - healing package optional at import time

    def maybe_fail(component: str) -> None:  # type: ignore[misc]
        return None


log = logging.getLogger("cargomind.mcp_server")

SERVER_NAME = "cargomind-logistics"

mcp = MCPServer(
    name=SERVER_NAME,
    title="CargoMind Logistics",
    instructions=(
        "Operational data for an India-wide logistics network (warehouses WH-BOM/WH-DEL/WH-BLR/"
        "WH-MAA/WH-HYD, shipments SHP-1001.., vehicles TRK-101.., carriers CR-01..). Look-ups that "
        "miss return {'status': 'error', 'suggestions': [...]} with the closest valid IDs - retry "
        "with a suggestion instead of guessing. Times are ISO-8601 IST."
    ),
    version="0.1.0",
    log_level="WARNING",
)


def _opt(value: str | None) -> str | None:
    """Treat '', 'none', 'any', 'all' as 'no filter' (LLMs often send these)."""
    if value is None:
        return None
    v = str(value).strip()
    return None if v.lower() in ("", "none", "null", "any", "all", "*") else v


# --------------------------------------------------------------------------- tools
def track_shipment(shipment_id: str) -> dict[str, Any]:
    """Track a shipment: current status, location, ETA vs promised delivery, carrier, vehicle and recent events.

    Use this first whenever the user mentions a specific shipment or order.

    Args:
        shipment_id: Shipment ID such as "SHP-1001" (also accepts "shp1001" or "1001").

    Returns:
        The shipment (status, priority, origin_city, destination_city, last_city, eta, promised_by,
        is_late, delay_hours, hours_to_promise, carrier_name, vehicle, customer, weight_kg, notes)
        plus ``recent_events`` (latest 5, newest last). On a miss: status="error" with suggestions.
    """
    maybe_fail("tool:track_shipment")
    s = db.get_shipment(shipment_id)
    if s.get("status") == "error":
        return s
    hist = db.shipment_history(s["id"])
    s["recent_events"] = hist.get("events", [])[-5:]
    if s.get("vehicle_id"):
        v = db.get_vehicle(s["vehicle_id"])
        if v.get("status") != "error":
            s["vehicle"] = {k: v[k] for k in ("id", "type", "driver_name", "driver_phone", "current_city",
                                               "refrigerated")}
    return s


def list_shipments(status: str = "", city: str = "", priority: str = "", limit: int = 25) -> dict[str, Any]:
    """List shipments, filtered and sorted (active first, critical first, then earliest promised delivery).

    Args:
        status: One of created, picked_up, in_transit, delayed, out_for_delivery, delivered, exception,
            or "active" (anything not yet delivered/exception). Empty = all.
        city: City name or alias (e.g. "Bangalore", "Bombay"); matches origin, destination or current city.
        priority: standard, express or critical. Empty = all.
        limit: Maximum rows to return (default 25).

    Returns:
        {"status": "ok", "count": n, "shipments": [...]} each with is_late / delay_hours.
    """
    maybe_fail("tool:list_shipments")
    return db.list_shipments(status=_opt(status), city=_opt(city), priority=_opt(priority), limit=limit)


def shipment_history(shipment_id: str) -> dict[str, Any]:
    """Full chronological event trail (scans, checkpoints, delays, assignments) for one shipment.

    Args:
        shipment_id: Shipment ID such as "SHP-1003".

    Returns:
        {"status": "ok", "shipment_id": ..., "events": [{ts, status, city, note, source}, ...]}.
    """
    maybe_fail("tool:shipment_history")
    return db.shipment_history(shipment_id)


def update_shipment_status(shipment_id: str, status: str, city: str = "", note: str = "") -> dict[str, Any]:
    """Change a shipment's status and log a history event (e.g. mark delayed, delivered, exception).

    Args:
        shipment_id: Shipment ID such as "SHP-1002".
        status: New status: created, picked_up, in_transit, delayed, out_for_delivery, delivered or exception.
        city: Optional city where the update happened (defaults to the current/destination city).
        note: Short human-readable reason, e.g. "Rain on NH-48, 6h delay". Stored as the shipment note.

    Returns:
        {"status": "ok", "previous_status": ..., "shipment": {...}, "event": {...}} or an error with valid values.
    """
    maybe_fail("tool:update_shipment_status")
    return db.update_shipment_status(shipment_id, status, city=_opt(city), note=note or "")


def check_inventory(sku: str = "", warehouse_id: str = "", low_stock_only: bool = False) -> dict[str, Any]:
    """Check stock levels per SKU and warehouse, including low-stock flags and any open purchase order.

    Args:
        sku: SKU ID ("SKU-107") or product name fragment ("insulin", "gloves"). Empty = all SKUs.
        warehouse_id: Warehouse ID ("WH-BLR"), code ("BLR") or city ("Bangalore"). Empty = all warehouses.
        low_stock_only: If true, only lines at or below their reorder point.

    Returns:
        {"status": "ok", "count", "low_stock_count", "items": [{sku, name, warehouse_id, qty_on_hand,
        reorder_point, shortfall, low_stock, open_po, ...}]}.
    """
    maybe_fail("tool:check_inventory")
    return db.get_inventory(sku=_opt(sku), warehouse_id=_opt(warehouse_id), low_stock_only=bool(low_stock_only))


def create_reorder(sku: str, warehouse_id: str, qty: int = 0) -> dict[str, Any]:
    """Raise a purchase order to replenish a SKU at a warehouse. Safe to retry: if an open PO already
    exists for the line it is returned with duplicate=true instead of creating another.

    Args:
        sku: SKU ID such as "SKU-107".
        warehouse_id: Warehouse ID ("WH-BLR"), code or city.
        qty: Units to order; 0 = automatic (tops stock up to 2x reorder point, min standard reorder qty).

    Returns:
        {"status": "ok", "duplicate": bool, "purchase_order": {id, qty, supplier, expected_by, ...}, "message"}.
    """
    maybe_fail("tool:create_reorder")
    return db.create_reorder(sku, warehouse_id, qty=qty or None)


def list_vehicles(status: str = "", city: str = "") -> dict[str, Any]:
    """List own-fleet vehicles with capacity, current load, driver hours remaining and fuel.

    Args:
        status: available, on_route or maintenance. Empty = all.
        city: Current city of the vehicle (name or alias). Empty = all.

    Returns:
        {"status": "ok", "count", "vehicles": [{id, type, capacity_kg, remaining_capacity_kg, status,
        current_city, driver_name, driver_hours_today, driver_hours_remaining, fuel_pct, refrigerated}]}.
    """
    maybe_fail("tool:list_vehicles")
    return db.list_vehicles(status=_opt(status), city=_opt(city))


def assign_vehicle(shipment_id: str, vehicle_id: str) -> dict[str, Any]:
    """Assign an own-fleet vehicle to a shipment. Checks capacity and maintenance status; idempotent.

    Args:
        shipment_id: Shipment ID such as "SHP-1007".
        vehicle_id: Vehicle ID such as "TRK-105".

    Returns:
        {"status": "ok", "warnings": [...], "vehicle": {...}} - warnings flag soft issues (vehicle in another
        city, driver near the 10h limit, low fuel, cold-chain mismatch). Errors suggest alternative vehicles.
    """
    maybe_fail("tool:assign_vehicle")
    return db.assign_vehicle(shipment_id, vehicle_id)


def list_carriers() -> dict[str, Any]:
    """List carrier partners with modes, INR rate card (base_rate_per_km, per_kg_rate), reliability (0-1)
    and average speed - use for quoting and choosing a carrier.

    Returns:
        {"status": "ok", "count", "carriers": [...]}.
    """
    maybe_fail("tool:list_carriers")
    return db.list_carriers()


def list_warehouses() -> dict[str, Any]:
    """List warehouses with city, coordinates, utilisation %, cold-chain capability and low-stock line count.

    Returns:
        {"status": "ok", "count", "warehouses": [...]}.
    """
    maybe_fail("tool:list_warehouses")
    return db.list_warehouses()


def get_kpis() -> dict[str, Any]:
    """Network health snapshot: on_time_rate (%), in_transit, delayed, exceptions, delivered_today,
    low_stock_skus, fleet_utilization (%), open_pos, critical_active and counts by status.

    Returns:
        KPI dict with status="ok".
    """
    maybe_fail("tool:get_kpis")
    return db.kpis()


TOOLS = (
    track_shipment, list_shipments, shipment_history, update_shipment_status, check_inventory,
    create_reorder, list_vehicles, assign_vehicle, list_carriers, list_warehouses, get_kpis,
)
for _fn in TOOLS:
    mcp.tool()(_fn)


# --------------------------------------------------------------------------- resources
@mcp.resource("logistics://kpis", name="kpis", description="Live network KPIs (JSON).",
              mime_type="application/json")
def kpis_resource() -> str:
    return json.dumps(db.kpis(), indent=2)


@mcp.resource("logistics://warehouses", name="warehouses", description="Warehouse directory (JSON).",
              mime_type="application/json")
def warehouses_resource() -> str:
    return json.dumps(db.list_warehouses(), indent=2)


# --------------------------------------------------------------------------- entrypoint
def main() -> None:
    """Run the server over stdio (stdout = protocol, stderr = logs)."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    db.get_conn()  # create/seed before the first request so tool calls stay fast
    log.info("%s serving %d tools over stdio (db=%s)", SERVER_NAME, len(TOOLS), db.config.DB_PATH)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
