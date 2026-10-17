"""SelfHealingPlugin: runner-wide tool/model healing for every CargoMind agent.

* before_tool  - per-tool circuit breaker short-circuit + argument repair
                 (ID canonicalisation, city alias resolution).
* on_tool_error - transient errors: retry with backoff (chaos suppressed);
                 everything else: reflect-and-retry hint for the LLM.
* after_tool   - tool-reported ``{"status": "error"}`` gets a healing hint.
* on_model_error - graceful assistant message instead of a crashed turn.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.genai import types

from cargomind import config
from cargomind.healing import chaos, journal
from cargomind.healing.breaker import get_breaker, peek_breaker

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

HEALING_HINT = (
    "Re-check arguments (IDs look like SHP-1001, ORD-5001, SKU-101, TRK-101, WH-BLR; "
    "cities like Mumbai/Bengaluru); try an alternative tool or ask the user."
)

# --------------------------------------------------------------- arg repair
# canonical prefix -> accepted spellings
_PREFIX_ALIASES: dict[str, tuple[str, ...]] = {
    "SHP": ("SHP", "SHIP", "SHIPMENT", "SH"),
    "ORD": ("ORD", "ORDER", "OR"),
    "SKU": ("SKU",),
    "TRK": ("TRK", "TRUCK", "VEH", "VEHICLE", "TK"),
    "WH": ("WH", "WAREHOUSE", "WHS"),
    "CR": ("CR", "CARRIER"),
    "PO": ("PO",),
}
_ALIAS_TO_PREFIX = {a: p for p, aliases in _PREFIX_ALIASES.items() for a in aliases}

# argument name (or stem) -> canonical prefix
_ARG_PREFIX: dict[str, str] = {
    "shipment": "SHP",
    "order": "ORD",
    "sku": "SKU",
    "vehicle": "TRK",
    "truck": "TRK",
    "warehouse": "WH",
    "carrier": "CR",
    "po": "PO",
    "purchase_order": "PO",
}

_ID_RE = re.compile(r"^([A-Z]+)[\s\-_#:.]*([A-Z0-9][A-Z0-9\-]*)$")
_CITY_KEYS = ("city", "origin", "destination", "depot", "stop", "place")
_SKIP_SUFFIXES = ("session_id", "invocation_id", "user_id", "image_ref")


def _arg_prefix(arg: str) -> str | None:
    """Canonical ID prefix implied by an argument name, if any."""
    stem = arg.lower()
    for suffix in ("_ids", "_id"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if stem in _ARG_PREFIX:
        return _ARG_PREFIX[stem]
    tail = stem.rsplit("_", 1)[-1]  # e.g. source_warehouse_id -> warehouse
    return _ARG_PREFIX.get(tail)


def _warehouse_from_city(value: str) -> str | None:
    """'bangalore' -> 'WH-BLR' via the gazetteer's IATA-style codes."""
    try:
        from cargomind.data import gazetteer
    except Exception:  # noqa: BLE001 - optional at import time
        return None
    city = gazetteer.resolve_city(value)
    if not city:
        return None
    for code, canon in getattr(gazetteer, "CODE_ALIASES", {}).items():
        if canon == city:
            return f"WH-{code.upper()}"
    return None


def normalize_id(value: Any, prefix: str | None) -> Any:
    """Canonicalises an entity ID; returns `value` unchanged when unsure.

    ``normalize_id("shp1003", "SHP") == "SHP-1003"``,
    ``normalize_id("1003", "SHP") == "SHP-1003"``,
    ``normalize_id("wh blr", "WH") == "WH-BLR"``,
    ``normalize_id("trk101", None) == "TRK-101"`` (prefix inferred from value).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and prefix and prefix != "WH":
        return f"{prefix}-{value}"
    if not isinstance(value, str):
        return value
    raw = value.strip()
    if not raw:
        return value
    up = re.sub(r"\s+", " ", raw.upper())
    m = _ID_RE.match(up)
    if m:
        head, rest = m.group(1), m.group(2).strip("-")
        canon = _ALIAS_TO_PREFIX.get(head)
        if canon and (prefix is None or canon == prefix):
            # Unknown arg names: only touch obvious IDs (prefix + digits).
            if prefix is None and not rest.isdigit():
                return value
            if canon == "WH" and not rest.isalpha():
                return value
            return f"{canon}-{rest}"
        if prefix and head == prefix and rest:
            return f"{prefix}-{rest}"
    if prefix is None:
        return value
    compact = re.sub(r"[\s_]+", "-", up).strip("-")
    if prefix == "WH":
        if re.fullmatch(r"[A-Z]{3}", compact):
            return f"WH-{compact}"
        return _warehouse_from_city(raw) or value
    if compact.isdigit():
        return f"{prefix}-{compact}"
    return value


def _resolve_city(value: str) -> str | None:
    try:
        from cargomind.data.gazetteer import resolve_city
    except Exception:  # noqa: BLE001 - gazetteer optional
        return None
    try:
        return resolve_city(value)
    except Exception:  # noqa: BLE001
        return None


def _is_city_arg(arg: str) -> bool:
    low = arg.lower()
    return any(k in low for k in _CITY_KEYS) and not low.endswith("_id")


def repair_args(tool_args: dict[str, Any]) -> list[str]:
    """Normalises IDs and city names in `tool_args` IN PLACE.

    Returns:
      Human-readable descriptions of each change (empty when nothing changed).
    """
    changes: list[str] = []
    for arg in list(tool_args):
        value = tool_args[arg]
        low = arg.lower()
        if low.endswith(_SKIP_SUFFIXES):
            continue
        new: Any = value
        if low.endswith(("_id", "_ids")) or low in ("sku", "skus"):
            prefix = _arg_prefix(low)
            if isinstance(value, list):
                new = [normalize_id(v, prefix) for v in value]
            else:
                new = normalize_id(value, prefix)
        elif _is_city_arg(low):
            if isinstance(value, str) and value.strip():
                new = _resolve_city(value) or value
            elif isinstance(value, list):
                new = [
                    (_resolve_city(v) or v) if isinstance(v, str) and v.strip() else v
                    for v in value
                ]
        if new != value:
            tool_args[arg] = new
            changes.append(f"{arg}: {value!r} → {new!r}")
    return changes


# ------------------------------------------------------------ plugin
def _short(error: BaseException) -> str:
    text = str(error).strip().replace("\n", " ")
    return (f"{type(error).__name__}: {text}" if text else type(error).__name__)[:300]


def _is_lookup_error(tool: BaseTool, error: Exception) -> bool:
    """The flow reports an unknown (hallucinated) tool name via on_tool_error."""
    return isinstance(error, ValueError) and "not found" in str(error) and (
        f"'{tool.name}'" in str(error) or "Available tools" in str(error)
    )


class SelfHealingPlugin(BasePlugin):
    """Retries, repairs, reflects and degrades gracefully. Journals everything."""

    def __init__(
        self,
        name: str = "self_healing",
        max_retries: int | None = None,
        backoff_base: float = 0.3,
    ) -> None:
        super().__init__(name=name)
        self.max_retries = config.TOOL_MAX_RETRIES if max_retries is None else max(0, max_retries)
        self.backoff_base = max(0.0, backoff_base)

    def _backoff(self, attempt: int) -> float:
        return self.backoff_base * (2**attempt)

    def _error_payload(self, tool_name: str, error: str, **extra: Any) -> dict[str, Any]:
        return {
            "status": "error",
            "error": error,
            "healing_hint": HEALING_HINT,
            "tool": tool_name,
            **extra,
        }

    # -- tools -----------------------------------------------------------------
    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict[str, Any] | None:
        component = f"tool:{tool.name}"
        breaker = peek_breaker(component)
        if breaker is not None and not breaker.allow():
            retry_in = breaker.retry_in()
            journal.record(
                "reflect",
                component,
                detail=f"circuit open ({breaker.failures} failures)",
                action="short-circuited with degraded response",
                outcome="degraded",
            )
            return self._error_payload(
                tool.name,
                f"{tool.name} is temporarily unavailable (circuit open after repeated failures).",
                degraded=True,
                retry_in_s=round(retry_in, 1),
            )
        try:
            changes = repair_args(tool_args)
        except Exception:
            logger.warning("arg repair failed for %s", tool.name, exc_info=True)
            changes = []
        if changes:
            journal.record(
                "arg_repair",
                component,
                detail="; ".join(changes),
                action="normalised arguments before call",
                outcome="repaired",
            )
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not isinstance(result, dict):
            self._note_success(tool.name)
            return None
        is_error = result.get("status") == "error" or (
            "status" not in result and bool(result.get("error"))
        )
        if not is_error:
            self._note_success(tool.name)
            return None
        if "healing_hint" in result:  # already handled (journaled) by this plugin
            return None
        journal.record(
            "reflect",
            f"tool:{tool.name}",
            detail=str(result.get("error", "tool reported an error"))[:300],
            action="added healing hint for LLM self-correction",
            outcome="handed_to_llm",
        )
        return {**result, "status": "error", "healing_hint": HEALING_HINT}

    def _note_success(self, tool_name: str) -> None:
        breaker = peek_breaker(f"tool:{tool_name}")
        if breaker is not None:
            breaker.success()

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> dict[str, Any] | None:
        component = f"tool:{tool.name}"
        if _is_lookup_error(tool, error):
            journal.record(
                "reflect",
                component,
                detail=_short(error),
                action="unknown tool: asked LLM to pick a listed tool",
                outcome="handed_to_llm",
            )
            message = str(error).split("Possible causes", 1)[0].strip()
            return self._error_payload(tool.name, message[:1500])

        last_error: Exception = error
        attempts = 0
        if chaos.is_transient(error):
            for attempt in range(self.max_retries):
                attempts = attempt + 1
                delay = self._backoff(attempt)
                journal.record(
                    "retry",
                    component,
                    detail=_short(last_error),
                    action=f"retry {attempts}/{self.max_retries} after {delay:.2g}s",
                    outcome="retrying",
                )
                if delay:
                    await asyncio.sleep(delay)
                try:
                    with chaos.no_chaos():
                        result = await tool.run_async(args=tool_args, tool_context=tool_context)
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if not chaos.is_transient(exc):
                        break
                    continue
                get_breaker(component).success()
                journal.record(
                    "recovered",
                    component,
                    detail=_short(error),
                    action=f"succeeded on retry {attempts}",
                    outcome="recovered",
                )
                return result if isinstance(result, dict) else {"result": result}

        get_breaker(component).failure()
        transient = chaos.is_transient(last_error)
        journal.record(
            "reflect",
            component,
            detail=_short(last_error),
            action=(
                f"retries exhausted ({attempts}); reflect-and-retry hint to LLM"
                if transient
                else "non-transient; reflect-and-retry hint to LLM"
            ),
            outcome="failed",
        )
        return self._error_payload(
            tool.name,
            _short(last_error),
            transient=transient,
            retries=attempts,
        )

    # -- models ----------------------------------------------------------------
    async def on_model_error_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
        error: Exception,
    ) -> LlmResponse | None:
        model = getattr(llm_request, "model", None) or "unknown"
        agent = getattr(callback_context, "agent_name", "") or ""
        journal.record(
            "fallback",
            f"model:{model}",
            detail=_short(error),
            action=f"graceful degradation message{f' ({agent})' if agent else ''}",
            outcome="failed",
        )
        text = (
            "I hit a temporary problem reaching my reasoning models "
            f"({type(error).__name__}). The incident is logged and the self-healing "
            "layer is routing around it - please retry in a moment, or ask me to "
            "continue in offline mode."
        )
        return LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            custom_metadata={"served_by": "self_healing", "degraded": True},
        )
