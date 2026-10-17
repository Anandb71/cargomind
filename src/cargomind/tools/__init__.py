"""External-API tools for CargoMind agents (maps, weather, planning, vision, comms).

Every public tool is a plain (mostly async) function with an LLM-oriented docstring and a
JSON-serialisable ``dict`` return, so it can be handed straight to an ADK ``Agent(tools=[...])``.

Shared plumbing (chaos hook, healing journal, HTTP helpers) lives here and is defined *before*
the submodules are imported, so ``from cargomind.tools import maybe_fail`` works from inside
them. The healing modules are imported lazily so the tools keep working (as no-ops) if the
self-healing layer is absent.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from cargomind import config

log = logging.getLogger("cargomind.tools")

USER_AGENT = "CargoMind/0.1 hackathon"


def maybe_fail(component: str) -> None:
    """Chaos hook: may raise an injected transient error (no-op if healing layer missing)."""
    try:
        from cargomind.healing.chaos import maybe_fail as _maybe_fail
    except ImportError:
        return
    _maybe_fail(component)


def journal(kind: str, component: str, detail: str, action: str = "", outcome: str = "") -> None:
    """Records a healing-journal event. Never raises."""
    try:
        from cargomind.healing.journal import record
    except ImportError:
        log.info("[%s] %s: %s (%s)", kind, component, detail, action)
        return
    try:
        record(kind, component, detail, action=action, outcome=outcome)
    except Exception:  # journaling must never break a tool
        log.debug("journal.record failed", exc_info=True)


def offline() -> bool:
    """True when CARGOMIND_OFFLINE forces network-free code paths."""
    return bool(config.FORCE_OFFLINE)


async def http_json(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
) -> Any:
    """Performs one HTTP request and returns the decoded JSON body.

    Raises ``httpx.HTTPError`` (network, timeout, non-2xx) or ``ValueError`` (bad JSON) so
    callers can fall back to the next provider.
    """
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    async with httpx.AsyncClient(timeout=timeout or config.HTTP_TIMEOUT_S) as client:
        resp = await client.request(method, url, params=params, json=json_body, headers=hdrs)
        resp.raise_for_status()
        return resp.json()


def short_error(exc: BaseException) -> str:
    """Compact, key-free description of an exception for journals and tool results."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    msg = str(exc).split("\n", 1)[0][:160]
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


# --- public tool surface (imported after the helpers above; see module docstring) ---------
from cargomind.tools.comms import draft_customer_notification, send_notification
from cargomind.tools.geo import geocode, get_route, optimize_multi_stop_route
from cargomind.tools.planning import (
    commit_dispatch_plan,
    estimate_eta,
    propose_dispatch_plan,
    quote_carriers,
    validate_dispatch_plan,
)
from cargomind.tools.vision import inspect_delivery_photo
from cargomind.tools.weather import check_route_disruptions, get_weather_risk

GEO_TOOLS = [geocode, get_route, optimize_multi_stop_route]
WEATHER_TOOLS = [get_weather_risk, check_route_disruptions]
PLANNING_TOOLS = [quote_carriers, estimate_eta]
DISPATCH_TOOLS = [propose_dispatch_plan, validate_dispatch_plan, commit_dispatch_plan]
VISION_TOOLS = [inspect_delivery_photo]
COMMS_TOOLS = [draft_customer_notification, send_notification]
ALL_TOOLS = GEO_TOOLS + WEATHER_TOOLS + PLANNING_TOOLS + DISPATCH_TOOLS + VISION_TOOLS + COMMS_TOOLS

__all__ = [
    "ALL_TOOLS",
    "COMMS_TOOLS",
    "DISPATCH_TOOLS",
    "GEO_TOOLS",
    "PLANNING_TOOLS",
    "VISION_TOOLS",
    "WEATHER_TOOLS",
    "check_route_disruptions",
    "commit_dispatch_plan",
    "draft_customer_notification",
    "estimate_eta",
    "geocode",
    "get_route",
    "get_weather_risk",
    "inspect_delivery_photo",
    "optimize_multi_stop_route",
    "propose_dispatch_plan",
    "quote_carriers",
    "send_notification",
    "validate_dispatch_plan",
]
