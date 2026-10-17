"""End-to-end test: spawn the MCP server over stdio and call it with the official mcp client."""
from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import StdioServerParameters
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {
    "track_shipment", "list_shipments", "shipment_history", "update_shipment_status", "check_inventory",
    "create_reorder", "list_vehicles", "assign_vehicle", "list_carriers", "list_warehouses", "get_kpis",
}


def _payload(result) -> dict:
    structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured.get("result", structured) if set(structured) == {"result"} else structured
    return json.loads(result.content[0].text)


async def _exercise(tmp_path) -> None:
    env = {
        "CARGOMIND_DB": str(tmp_path / "mcp-test.db"),
        "CARGOMIND_DATA_DIR": str(tmp_path),
        "CARGOMIND_CHAOS": "0",
        "PYTHONIOENCODING": "utf-8",
    }
    if os.getenv("PYTHONPATH"):
        env["PYTHONPATH"] = os.environ["PYTHONPATH"]
    params = StdioServerParameters(command=sys.executable, args=["-m", "cargomind.mcp_server.server"], env=env)
    with open(tmp_path / "server.log", "w", encoding="utf-8") as errlog:  # noqa: ASYNC230
        async with stdio_client(params, errlog=errlog) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert EXPECTED_TOOLS <= names, EXPECTED_TOOLS - names
            track = next(t for t in tools.tools if t.name == "track_shipment")
            assert "shipment_id" in json.dumps(track.input_schema if hasattr(track, "input_schema")
                                               else track.inputSchema)

            res = await session.call_tool("track_shipment", {"shipment_id": "SHP-1001"})
            data = _payload(res)
            assert data["id"] == "SHP-1001"
            assert data["recent_events"]

            miss = _payload(await session.call_tool("track_shipment", {"shipment_id": "SHP-1001X"}))
            assert miss["status"] == "error" and miss["suggestions"][0] == "SHP-1001"

            kpis = _payload(await session.call_tool("get_kpis", {}))
            assert "on_time_rate" in kpis

            resource = await session.read_resource("logistics://kpis")
            text = resource.contents[0].text
            assert "fleet_utilization" in json.loads(text)


def test_mcp_server_stdio_roundtrip(tmp_path):
    asyncio.run(asyncio.wait_for(_exercise(tmp_path), timeout=60))


def test_tools_are_plain_functions():
    from cargomind.mcp_server import server as srv

    for name in EXPECTED_TOOLS:
        fn = getattr(srv, name)
        assert callable(fn) and fn.__doc__ and fn.__name__ == name
