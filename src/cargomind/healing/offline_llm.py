"""OfflineLlm - a deterministic rule-engine that speaks the ADK model protocol.

It is the last link of every ResilientLlm chain, so CargoMind keeps working when
Gemini / Claude / OpenAI are unreachable (or when no key is configured at all).
It does real work: it picks tools by intent keywords, extracts arguments
(IDs, cities, weights, priorities, ...) straight from the request text using the
tool's own declared schema, and turns tool results into readable markdown.
"""
from __future__ import annotations

import json
import re
from typing import Any, AsyncGenerator

from google.genai import types
from pydantic import BaseModel, Field

from google.adk.models import BaseLlm, LlmRequest, LlmResponse

OFFLINE_MODEL = "offline/rule-engine"


class OfflineRule(BaseModel):
    tool: str
    keywords: list[str] = Field(default_factory=list)
    then: list[str] = Field(default_factory=list)  # follow-up tools after this one
    min_cities: int = Field(default=0, ge=0)  # route tools need both endpoints


class OfflinePolicy(BaseModel):
    agent: str = "agent"
    title: str = ""
    rules: list[OfflineRule] = Field(default_factory=list)
    default_tool: str | None = None
    multi: bool = False  # router mode: fan out to every matching tool
    finish_tool: str | None = None  # e.g. exit_loop once a result says valid
    finish_when_key: str = "valid"
    echo_context: bool = False  # summarise sibling agents' outputs (writer agents)
    help_text: str = ""


# ---------------------------------------------------------------- extraction
_ID_PATTERNS = {
    "shipment": (r"\bSHP[-_\s]?(\d{3,5})\b", "SHP-{}"),
    "order": (r"\bORD[-_\s]?(\d{3,5})\b", "ORD-{}"),
    "vehicle": (r"\bTRK[-_\s]?(\d{2,4})\b", "TRK-{}"),
    # "WHBLR"/"WH-BLR"/"wh blr", but never the "Wh" of "Which" (no-separator form must be uppercase)
    "warehouse": (r"\b(?:(?-i:WH)[-_\s]?|WH[-_\s])([A-Za-z]{3})\b", "WH-{}"),
    "sku": (r"\bSKU[-_\s]?(\d[A-Za-z0-9]*(?:-[A-Za-z0-9]+)*)\b", "SKU-{}"),  # digit first: "SKUs" is not an id
    "carrier": (r"\bCR[-_\s]?(\d{1,3})\b", "CR-{}"),
}
_STATUSES = ["out_for_delivery", "in_transit", "picked_up", "delivered", "delayed", "exception", "created"]
_NEGATIVE = re.compile(r"\b(wrong|bad|incorrect|never|don'?t|stop|avoid|hate|terrible|not)\b", re.I)


def _find_cities(text: str) -> list[str]:
    try:
        from cargomind.data.gazetteer import find_cities_in_text

        return find_cities_in_text(text)
    except Exception:  # gazetteer missing -> naive capitalised words
        return []


def _extract(name: str, ptype: str, text: str, cities: list[str]) -> Any:
    low = name.lower()
    for key, (pattern, fmt) in _ID_PATTERNS.items():
        if key in low and ("id" in low or low == key or key == "sku"):
            m = re.search(pattern, text, re.I)
            if m:
                return fmt.format(m.group(1).upper())
            if key == "shipment":
                m = re.search(r"\b(1\d{3})\b", text)
                if m:
                    return f"SHP-{m.group(1)}"
            return None
    if low in ("request", "query", "question", "feedback", "instructions"):
        return text
    if low in ("origin", "from_city", "source", "depot", "start"):
        return cities[0] if cities else None
    if low in ("destination", "to_city", "dest", "end"):
        return cities[1] if len(cities) > 1 else None
    if low in ("city", "place", "location", "address", "hub"):
        return cities[0] if cities else None
    if low in ("stops", "waypoints", "cities", "destinations"):
        return cities[1:] if len(cities) > 1 else None
    if "weight" in low:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:kg|kgs|kilo)", text, re.I)
        if m:
            return float(m.group(1))
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:t|ton|tons|tonnes)\b", text, re.I)
        return float(m.group(1)) * 1000 if m else None
    if low in ("qty", "quantity", "units"):
        m = re.search(r"(\d+)\s*(?:units|pcs|qty|pieces)?", text, re.I)
        return int(m.group(1)) if m else None
    if low == "priority":
        for p in ("critical", "express", "standard"):
            if p in text.lower():
                return p
        return None
    if low == "channel":
        for c in ("whatsapp", "sms", "email"):
            if c in text.lower():
                return c
        return None
    if low == "status":
        t = text.lower().replace(" ", "_")
        return next((s for s in _STATUSES if s in t), None)
    if low == "event":
        m = re.search(r"\b(cyclone|flood\w*|strike|bandh|closure|landslide|heat ?wave|fog|storm|earthquake|fire)\b", text, re.I)
        return m.group(1).lower() if m else None
    if low == "severity":
        m = re.search(r"\b(?:severity\W+(low|medium|high)|(low|medium|high)\W+severity)\b", text, re.I)
        return (m.group(1) or m.group(2)).lower() if m else None
    if low == "reason":
        m = re.search(r"(?:for|about|because of|due to)\s+(.+)$", text, re.I)
        return m.group(1).strip(" .?") if m else "delay"
    if low in ("image_ref", "image", "photo"):
        m = re.search(r"([\w-]+\.(?:jpe?g|png|webp))", text, re.I)
        return m.group(1) if m else None
    if low == "rating":
        return -1 if _NEGATIVE.search(text) else 1
    if low in ("low_stock_only",):
        return bool(re.search(r"low|reorder|short|running out|stock ?out", text, re.I))
    if low == "avoid_tolls":
        return bool(re.search(r"avoid(?:ing)? toll|no toll|toll[- ]free", text, re.I))
    return None


def _coerce(value: Any, ptype: str) -> Any:
    try:
        if ptype in ("INTEGER", "integer"):
            return int(value)
        if ptype in ("NUMBER", "number"):
            return float(value)
        if ptype in ("BOOLEAN", "boolean"):
            return bool(value)
        if ptype in ("ARRAY", "array") and not isinstance(value, list):
            return [value]
        if ptype in ("STRING", "string") and isinstance(value, list):
            return ", ".join(map(str, value))
    except (TypeError, ValueError):
        return None
    return value


def _tool_params(tool: Any) -> tuple[dict[str, str], list[str]]:
    """Return ({param: type}, required) from an ADK tool declaration."""
    try:
        decl = tool._get_declaration()
    except Exception:
        decl = None
    if decl is None:
        return {}, []
    if getattr(decl, "parameters", None) is not None and decl.parameters.properties:
        props = {
            k: (v.type.value if hasattr(v.type, "value") else str(v.type or "STRING")).upper()
            for k, v in decl.parameters.properties.items()
        }
        return props, list(decl.parameters.required or [])
    schema = getattr(decl, "parameters_json_schema", None) or {}
    props = {}
    for k, v in (schema.get("properties") or {}).items():
        t = v.get("type")
        if t is None and v.get("anyOf"):
            t = next((a.get("type") for a in v["anyOf"] if a.get("type") != "null"), "string")
        props[k] = str(t or "string").upper()
    return props, list(schema.get("required") or [])


# ---------------------------------------------------------------- rendering
def _fmt_value(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.1f}"
    if isinstance(v, (dict, list)):
        s = json.dumps(v, default=str)
        return s if len(s) < 140 else s[:137] + "..."
    return str(v)


def _unwrap_mcp(obj: Any) -> Any:
    """MCP tool results arrive as {content:[{type:text,text:json}], structuredContent, isError}."""
    if isinstance(obj, dict) and isinstance(obj.get("content"), list) and ("isError" in obj or "structuredContent" in obj):
        sc = obj.get("structuredContent")
        if isinstance(sc, dict):
            return sc.get("result", sc) if set(sc) == {"result"} else sc
        texts = [c.get("text", "") for c in obj["content"] if isinstance(c, dict)]
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except (ValueError, TypeError):
            return joined
    return obj


def _parse_literal(text: str) -> Any:
    import ast

    text = text.strip()
    for loader in (json.loads, ast.literal_eval):
        try:
            return loader(text)
        except Exception:  # noqa: BLE001
            continue
    return text


_SKIP_KEYS = {"polyline", "coordinates", "provider_chain_tried", "customer_email", "customer_phone", "created_at", "source"}


def render(obj: Any, depth: int = 0) -> str:
    """Readable markdown for arbitrary tool output."""
    obj = _unwrap_mcp(obj)
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        if set(obj) == {"result"}:
            return render(obj["result"], depth)
        lines = []
        for k, v in obj.items():
            if k in _SKIP_KEYS or v in (None, "", [], {}):
                continue
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                lines.append(f"**{k.replace('_', ' ')}**:\n{_table(v)}")
            elif isinstance(v, dict) and depth < 1:
                lines.append(f"**{k.replace('_', ' ')}**: " + "; ".join(f"{a}={_fmt_value(b)}" for a, b in list(v.items())[:8]))
            else:
                lines.append(f"- **{k.replace('_', ' ')}**: {_fmt_value(v)}")
        return "\n".join(lines)
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            return _table(obj)
        return "\n".join(f"- {_fmt_value(x)}" for x in obj)
    return _fmt_value(obj)


_KEY_COLS = ("id", "status", "sku", "warehouse_id", "qty_on_hand", "reorder_point")


def _table(rows: list[dict], limit: int = 12) -> str:
    scalar: list[str] = []
    for r in rows[:limit]:
        for k, v in r.items():
            if k not in scalar and not isinstance(v, (dict, list)):
                scalar.append(k)
    # key columns (id, status) always survive the 7-column cap
    priority = [k for k in scalar if k in _KEY_COLS]
    cols = (priority + [k for k in scalar if k not in priority])[:7]
    head = "| " + " | ".join(c.replace("_", " ") for c in cols) + " |"
    sep = "|" + "---|" * len(cols)
    body = ["| " + " | ".join(_fmt_value(r.get(c, "")) for c in cols) + " |" for r in rows[:limit]]
    more = [f"\n_…and {len(rows) - limit} more_"] if len(rows) > limit else []
    return "\n".join([head, sep, *body, *more])


# ---------------------------------------------------------------- the model
def _text_of(content: types.Content) -> str:
    return " ".join(p.text for p in (content.parts or []) if getattr(p, "text", None)).strip()


def _has(content: types.Content, attr: str) -> bool:
    return any(getattr(p, attr, None) for p in (content.parts or []))


_ZERO_USAGE = types.GenerateContentResponseUsageMetadata(
    prompt_token_count=0, candidates_token_count=0, total_token_count=0
)


class OfflineLlm(BaseLlm):
    model: str = OFFLINE_MODEL
    policy: OfflinePolicy = Field(default_factory=OfflinePolicy)

    @classmethod
    def supported_models(cls) -> list[str]:
        return [r"offline/.*"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        yield self._respond(llm_request)

    # -- core decision logic (sync, unit-testable) --
    def _respond(self, req: LlmRequest) -> LlmResponse:
        contents = list(req.contents or [])
        tools = dict(req.tools_dict or {})
        # turn start = last user-role content carrying text (real user or sibling-agent context)
        start = 0
        for i in range(len(contents) - 1, -1, -1):
            c = contents[i]
            if c.role == "user" and _text_of(c) and not _has(c, "function_response"):
                start = i
                break
        user_texts = [
            _text_of(c) for c in contents if c.role == "user" and _text_of(c) and not _has(c, "function_response")
        ]
        real_texts = [t for t in user_texts if not t.startswith("For context")]
        # the operator's latest request (multi-turn safe); sibling-agent context is not a request
        request_text = real_texts[-1] if real_texts else (user_texts[-1] if user_texts else "")
        current_text = user_texts[-1] if user_texts else ""
        turn = contents[start + 1 :]
        responses = [
            p.function_response for c in turn for p in (c.parts or []) if getattr(p, "function_response", None)
        ]
        called = [p.function_call.name for c in turn for p in (c.parts or []) if getattr(p, "function_call", None)]

        if responses:
            last = responses[-1]
            # loop finisher (critic): plan valid -> exit_loop
            pol = self.policy
            if pol.finish_tool and pol.finish_tool in tools and pol.finish_tool not in called:
                resp = _unwrap_mcp(last.response or {})
                if isinstance(resp, dict) and resp.get(pol.finish_when_key) is True:
                    return self._call([(pol.finish_tool, {})])
            # chained follow-ups
            for rule in pol.rules:
                if rule.tool == last.name:
                    nxt = [t for t in rule.then if t in tools and t not in called]
                    if nxt:
                        calls = self._build_calls(nxt, request_text, tools)
                        if calls:
                            return self._call(calls)
            return self._text(self._summarise(responses))

        if self.policy.echo_context:
            ctx = [t for t in user_texts if t.startswith("For context")]
            return self._text(self._compose_brief(ctx, request_text))

        chosen = self._choose(request_text, tools)
        if chosen:
            calls = self._build_calls(chosen, request_text, tools)
            if calls:
                return self._call(calls)
            missing = ", ".join(chosen)
            return self._text(
                f"I can handle that with `{missing}`, but I need a bit more detail "
                "(e.g. a shipment ID like SHP-1003, an SKU, or origin/destination cities)."
            )
        return self._text(self.policy.help_text or "How can I help with your logistics operations?")

    def _choose(self, text: str, tools: dict) -> list[str]:
        low = text.lower()
        scored = []
        city_count = None
        for idx, rule in enumerate(self.policy.rules):
            if rule.tool not in tools:
                continue
            if rule.min_cities:
                if city_count is None:
                    city_count = len(_find_cities(text))
                if city_count < rule.min_cities:
                    continue
            hits = sum(1 for k in rule.keywords if re.search(rf"(?<![a-z]){re.escape(k.lower())}", low))
            if hits:
                scored.append((hits, -idx, rule.tool))
        scored.sort(reverse=True)
        if scored:
            if self.policy.multi:  # fan out only to specialists that clearly match
                top = scored[0][0]
                scored = [s for s in scored if s[0] >= max(1, top - 1)]
            names = list(dict.fromkeys(t for _, _, t in scored))
            return names if self.policy.multi else names[:1]
        if self.policy.default_tool and self.policy.default_tool in tools:
            return [self.policy.default_tool]
        return []

    def _build_calls(self, names: list[str], text: str, tools: dict) -> list[tuple[str, dict]]:
        cities = _find_cities(text)
        calls = []
        for name in names:
            props, required = _tool_params(tools[name])
            args: dict[str, Any] = {}
            for pname, ptype in props.items():
                if pname in ("tool_context",):
                    continue
                val = _extract(pname, ptype, text, cities)
                if val is not None:
                    val = _coerce(val, ptype)
                if val is not None:
                    args[pname] = val
            if all(r in args for r in required):
                calls.append((name, args))
        return calls

    def _summarise(self, responses: list) -> str:
        title = self.policy.title or self.policy.agent.replace("_", " ").title()
        parts = []
        for fr in responses:
            resp = fr.response if isinstance(fr.response, dict) else {"result": fr.response}
            if fr.name == self.policy.finish_tool:
                continue
            body = render(resp)
            if self.policy.multi:  # router: specialist answers already formatted
                parts.append(body)
            else:
                parts.append(f"**{fr.name.replace('_', ' ')}**\n{body}")
        text = "\n\n".join(p for p in parts if p.strip()) or "Done."
        if not self.policy.multi:
            text = f"### {title}\n{text}"
        return text

    _CTX_RE = re.compile(
        r"\[(?P<agent>[^\]]+)\]\s*(?P<kind>said|called tool\s*`?(?P<ctool>\w+)`?[^\n]*|`?(?P<rtool>\w+)`?\s*tool returned result)"
        r"[^\n]*\n?\s*(?:<<<BEGIN_QUOTED_AGENT_CONTENT>>>\s*(?P<quoted>.*?)\s*<<<END_QUOTED_AGENT_CONTENT>>>|(?P<plain>.*))",
        re.S,
    )

    def _compose_brief(self, ctx: list[str], request_text: str) -> str:
        """Summarise sibling agents' work: last statement per agent, else its last tool result."""
        said: dict[str, str] = {}
        results: dict[str, Any] = {}
        order: list[str] = []
        for c in ctx:
            m = self._CTX_RE.search(c)
            if not m:
                continue
            agent = m.group("agent")
            body = (m.group("quoted") if m.group("quoted") is not None else m.group("plain") or "").strip()
            if agent not in order:
                order.append(agent)
            if m.group("kind") == "said":
                if body and body.lower() not in ("none", "null"):
                    said[agent] = body
            elif m.group("rtool") and m.group("rtool") != "exit_loop":
                results[agent] = _parse_literal(body)
        title = self.policy.title or "Briefing"
        lines = [f"## {title}", ""]
        for agent in order:
            who = agent.replace("_", " ").title()
            if agent in said:
                body = said[agent]
            elif agent in results:
                body = render(results[agent])
            else:
                continue
            lines.append(f"**{who}**\n{body.strip()[:2500]}\n")
        if self.policy.help_text:
            lines.append(f"_{self.policy.help_text}_")
        return "\n".join(lines)

    def _call(self, calls: list[tuple[str, dict]]) -> LlmResponse:
        parts = [types.Part(function_call=types.FunctionCall(name=n, args=a)) for n, a in calls]
        return LlmResponse(
            content=types.Content(role="model", parts=parts),
            custom_metadata={"served_by": self.model},
            usage_metadata=_ZERO_USAGE,
        )

    def _text(self, text: str) -> LlmResponse:
        return LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            custom_metadata={"served_by": self.model},
            usage_metadata=_ZERO_USAGE,
        )
