"""Bounded usage receipts: counts and timings only, never prompts or identities."""
from __future__ import annotations

import contextvars
import threading
from collections import deque

_lock = threading.Lock()
_receipts: deque[dict] = deque(maxlen=512)
_current: contextvars.ContextVar[list | None] = contextvars.ContextVar("cargomind_usage", default=None)
# Standard short-context text rates per million tokens, verified 2026-10-17.
_RATES = {"gpt-6.1-sol": (2.0, 0.10, 10.0), "gpt-6-luna": (0.10, 0.01, 0.50)}


def begin_run():
    return _current.set([])


def end_run(token) -> None:
    _current.reset(token)


def record_model_call(model: str, usage: dict | None, elapsed_ms: float,
                      kind: str = "agent", status: str = "completed") -> dict:
    usage = usage or {}
    def count(*keys):
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return None
    input_tokens = count("input_tokens", "prompt_tokens", "prompt_token_count")
    output_tokens = count("output_tokens", "completion_tokens", "candidates_token_count")
    details = usage.get("input_tokens_details") or {}
    cached = details.get("cached_tokens", usage.get("cached_content_token_count", 0))
    cached = cached if isinstance(cached, int) and not isinstance(cached, bool) and cached >= 0 else 0
    canonical = model.removeprefix("openai/")
    rates = _RATES.get(canonical)
    cost = None
    if rates and input_tokens is not None and output_tokens is not None:
        cache = min(input_tokens, cached)
        cost = ((input_tokens-cache)*rates[0]+cache*rates[1]+output_tokens*rates[2])/1_000_000
    receipt = {"model": canonical, "kind": kind if kind in {"agent", "judge"} else "agent",
               "status": "completed" if status == "completed" else "failed",
               "input_tokens": input_tokens, "output_tokens": output_tokens,
               "cached_tokens": cached, "latency_ms": round(max(0, float(elapsed_ms)), 2),
               "estimated_text_cost_usd": cost}
    with _lock:
        _receipts.append(receipt)
    current = _current.get()
    if current is not None and len(current) < 128:
        current.append(receipt)
    return receipt


def _summary(receipts: list[dict]) -> dict:
    known = [r for r in receipts if r["input_tokens"] is not None and r["output_tokens"] is not None]
    prices = [r["estimated_text_cost_usd"] for r in receipts if r["estimated_text_cost_usd"] is not None]
    return {"model_calls": len(receipts), "failed_calls": sum(r["status"] == "failed" for r in receipts),
            "input_tokens": sum(r["input_tokens"] for r in known),
            "output_tokens": sum(r["output_tokens"] for r in known),
            "usage_unknown_calls": len(receipts)-len(known),
            "estimated_text_cost_usd": round(sum(prices), 8) if prices else None,
            "unpriced_calls": sum(r["estimated_text_cost_usd"] is None for r in receipts),
            "pricing_basis": "Standard short-context text estimate, 2026-10-17; excludes tool fees, cache writes and regional/tier premiums.",
            "models": sorted({r["model"] for r in receipts}), "receipts": receipts}


def run_summary() -> dict:
    return _summary(list(_current.get() or []))


def snapshot() -> dict:
    with _lock:
        values = list(_receipts)
    return _summary(values)
