"""MCP integration with a self-healing twist.

Each specialist talks to the CargoMind logistics MCP server
(`python -m cargomind.mcp_server.server`, stdio) through ADK's McpToolset.
If the MCP server can't start or crashes, ResilientMcpToolset journals the
fault, opens a breaker, and serves the *same* tool names from in-process
FunctionTools so the conversation never breaks. When the breaker cools down
it reconnects to MCP automatically.
"""
from __future__ import annotations

import sys
from typing import Optional

from google.adk.tools import FunctionTool
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from mcp import StdioServerParameters

from cargomind import config


def _journal(kind: str, component: str, detail: str, action: str = "", outcome: str = "") -> None:
    try:
        from cargomind.healing.journal import record

        record(kind, component, detail, action=action, outcome=outcome)
    except Exception:
        pass


def _breaker(name: str):
    try:
        from cargomind.healing.breaker import get_breaker

        return get_breaker(name)
    except Exception:
        return None


def _local_tools(names: list[str]) -> list[BaseTool]:
    from cargomind.mcp_server import server as srv

    tools = []
    for n in names:
        fn = getattr(srv, n, None)
        fn = getattr(fn, "fn", fn)  # unwrap if a decorator wrapped it
        if callable(fn):
            tools.append(FunctionTool(fn))
    return tools


class ResilientMcpToolset(BaseToolset):
    def __init__(self, tool_names: list[str]):
        super().__init__(tool_filter=tool_names)
        self.tool_names = tool_names
        self._mcp = McpToolset(
            connection_params=StdioConnectionParams(
                server_params=StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "cargomind.mcp_server.server"],
                    env={"CARGOMIND_DB": str(config.DB_PATH), "PYTHONIOENCODING": "utf-8"},
                ),
                timeout=20.0,
            ),
            tool_filter=tool_names,
        )
        self._degraded = False

    async def get_tools(self, readonly_context=None) -> list[BaseTool]:
        breaker = _breaker("mcp:cargomind-logistics")
        if config.FORCE_MCP_LOCAL or (breaker and not breaker.allow()):
            return _local_tools(self.tool_names)
        try:
            tools = await self._mcp.get_tools(readonly_context)
            if not tools:
                raise RuntimeError("MCP server returned no tools")
            if breaker:
                breaker.success()
            if self._degraded:
                _journal("recovered", "mcp:cargomind-logistics", "MCP server reachable again", outcome="reconnected")
                self._degraded = False
            return tools
        except Exception as exc:  # noqa: BLE001 - any failure -> heal
            if breaker:
                breaker.failure()
            self._degraded = True
            _journal(
                "fallback",
                "mcp:cargomind-logistics",
                f"{type(exc).__name__}: {str(exc)[:160]}",
                action="serve same tools in-process",
                outcome="degraded",
            )
            return _local_tools(self.tool_names)

    async def close(self) -> None:
        try:
            await self._mcp.close()
        except Exception:
            pass


def mcp(*tool_names: str) -> ResilientMcpToolset:
    return ResilientMcpToolset(list(tool_names))


class GoogleMapsMcpToolset(BaseToolset):
    """Google's hosted Maps MCP server (Grounding Lite, Streamable HTTP).

    Adds Google-grounded `search_places`, `lookup_weather` and `compute_routes`
    tools. Optional: if the key lacks access or the endpoint is down, the
    breaker opens and the agent silently keeps its own Routes/OSRM/Open-Meteo tools.
    """

    def __init__(self):
        super().__init__()
        from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

        self._mcp = McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url=config.GOOGLE_MAPS_MCP_URL,
                headers={"X-Goog-Api-Key": config.GOOGLE_MAPS_API_KEY or ""},
                timeout=10.0,
            ),
            tool_name_prefix="gmaps",
        )

    async def get_tools(self, readonly_context=None) -> list[BaseTool]:
        breaker = _breaker("mcp:google-maps")
        if breaker and not breaker.allow():
            return []
        try:
            tools = await self._mcp.get_tools(readonly_context)
            if breaker:
                breaker.success()
            return tools
        except Exception as exc:  # noqa: BLE001
            if breaker:
                breaker.failure()
            _journal("fallback", "mcp:google-maps", f"{type(exc).__name__}: {str(exc)[:160]}",
                     action="continue with built-in Routes/OSRM + Open-Meteo tools", outcome="degraded")
            return []

    async def close(self) -> None:
        try:
            await self._mcp.close()
        except Exception:
            pass


def google_maps_mcp() -> list[BaseToolset]:
    if config.GOOGLE_MAPS_API_KEY and config.USE_GOOGLE_MAPS_MCP and not config.FORCE_OFFLINE:
        return [GoogleMapsMcpToolset()]
    return []
