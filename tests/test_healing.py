"""Tests for the self-healing layer (journal, chaos, breaker, ResilientLlm, plugin)."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Isolated DB + no chaos BEFORE cargomind (and its config) is imported.
os.environ.setdefault(
    "CARGOMIND_DB", str(Path(tempfile.mkdtemp(prefix="cargomind-test-")) / "test.db")
)
os.environ["CARGOMIND_CHAOS"] = "0"

import asyncio
import threading
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.function_tool import FunctionTool
from google.genai import types

from cargomind import config
from cargomind.healing import breaker as breaker_mod
from cargomind.healing import chaos, journal
from cargomind.healing.breaker import CircuitBreaker, get_breaker
from cargomind.healing.plugin import HEALING_HINT, SelfHealingPlugin, normalize_id, repair_args
from cargomind.healing.resilient_llm import ResilientLlm, build_model


@pytest.fixture(autouse=True)
def _clean_state():
    chaos.set_rate(0.0)
    chaos.seed(1234)
    journal.clear()
    breaker_mod.reset_all(forget=True)
    yield
    chaos.set_rate(0.0)


def kinds() -> list[str]:
    return [e["kind"] for e in reversed(journal.recent(500))]


# ------------------------------------------------------------- fakes
class FakeLlm(BaseLlm):
    """Scripted model: each call pops the next behaviour (an Exception or text)."""

    script: list[Any] = []
    seen_models: list[str] = []
    seen_lengths: list[int] = []
    mutate: bool = True

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self.seen_models.append(llm_request.model)
        self.seen_lengths.append(len(llm_request.contents))
        if self.mutate:  # models edit the request in place, like real ones
            llm_request.contents.append(
                types.Content(role="user", parts=[types.Part(text=f"junk from {self.model}")])
            )
        step = self.script.pop(0) if self.script else "ok"
        if isinstance(step, BaseException):
            raise step
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=step)]))


class StreamThenFail(BaseLlm):
    async def generate_content_async(self, llm_request, stream=False):
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text="partial")]), partial=True
        )
        raise ConnectionError("dropped mid-stream")


def make_request() -> LlmRequest:
    return LlmRequest(
        model="resilient",
        contents=[types.Content(role="user", parts=[types.Part(text="where is SHP-1001?")])],
    )


async def collect(llm: BaseLlm, req: LlmRequest, stream: bool = False) -> list[LlmResponse]:
    return [r async for r in llm.generate_content_async(req, stream=stream)]


# ------------------------------------------------------------- journal
def test_journal_record_recent_stats_and_history():
    e = journal.record("fault", "tool:x", "boom", action="injected")
    assert set(e) == {"id", "ts", "kind", "component", "detail", "action", "outcome"}
    journal.record("recovered", "tool:x", "boom", outcome="recovered")
    journal.record("reflect", "tool:y", "bad", outcome="failed")
    recent = journal.recent(2)
    assert [r["kind"] for r in recent] == ["reflect", "recovered"]  # newest first
    s = journal.stats()
    assert s["by_kind"]["fault"] == 1 and s["recovered"] == 1 and s["failed"] == 1
    assert s["recovery_rate"] == 0.5
    assert any(h["id"] == e["id"] for h in journal.history(50))  # persisted to sqlite


def test_journal_subscribe_receives_events_from_other_threads():
    async def main():
        q = journal.subscribe()
        t = threading.Thread(target=journal.record, args=("retry", "tool:z", "from thread"))
        t.start()
        t.join()
        ev = await asyncio.wait_for(q.get(), timeout=2)
        journal.unsubscribe(q)
        return ev

    ev = asyncio.run(main())
    assert ev["kind"] == "retry" and ev["detail"] == "from thread"


def test_journal_subscribe_without_running_loop():
    q = journal.subscribe()
    journal.record("fault", "tool:q")
    assert q.get_nowait()["component"] == "tool:q"
    journal.unsubscribe(q)


# ------------------------------------------------------------- chaos
def test_chaos_rate_no_chaos_and_force():
    chaos.maybe_fail("tool:a")  # rate 0: nothing
    chaos.set_rate(1.0)
    with pytest.raises(chaos.ChaosError):
        chaos.maybe_fail("tool:a")
    assert kinds()[-1] == "fault"
    with chaos.no_chaos():
        chaos.maybe_fail("tool:a")  # suppressed during recovery
    assert chaos.set_rate(5) == 1.0 and chaos.set_rate(-1) == 0.0
    chaos.force_fail("model:gem", 1)
    with pytest.raises(chaos.ChaosError):
        chaos.maybe_fail("model:gemini-x")
    chaos.maybe_fail("model:gemini-x")  # disarmed after one


def test_is_transient_classification():
    assert chaos.is_transient(chaos.ChaosError("x"))
    assert chaos.is_transient(TimeoutError())
    assert chaos.is_transient(ConnectionResetError())
    assert not chaos.is_transient(ValueError("bad id"))
    assert not chaos.is_transient(FileNotFoundError("x"))

    class Http429(Exception):
        status_code = 429

    class Http401(Exception):
        status_code = 401

    assert chaos.is_transient(Http429()) and not chaos.is_transient(Http401())


# ------------------------------------------------------------- breaker
def test_breaker_lifecycle_and_journal():
    import time

    b = CircuitBreaker("tool:flaky", threshold=2, cooldown=0.3)
    assert b.allow() and b.state == "closed"
    b.failure()
    assert b.state == "closed"
    b.failure()
    assert b.state == "open" and not b.allow()
    time.sleep(0.4)
    assert b.state == "half_open" and b.allow()
    b.failure()  # failed probe re-opens
    assert b.state == "open"
    time.sleep(0.4)
    b.success()
    assert b.state == "closed"
    assert kinds().count("breaker_open") == 2 and kinds()[-1] == "breaker_close"
    assert get_breaker("x") is get_breaker("x")
    assert any(s["name"] == "x" for s in breaker_mod.snapshot())


# ------------------------------------------------------------- ResilientLlm
def test_fallback_order_restore_and_metadata():
    a = FakeLlm(model="gemini-a", script=[ValueError("missing API key")])
    b = FakeLlm(model="claude-b", script=[RuntimeError("401 auth")])
    c = FakeLlm(model="offline/rule-engine", script=["offline answer"])
    llm = ResilientLlm(chain=[a, b, c], tier="fast", retry_backoff=(0.0, 0.0))
    req = make_request()
    out = asyncio.run(collect(llm, req))

    assert [a.seen_models, b.seen_models, c.seen_models] == [
        ["gemini-a"],
        ["claude-b"],
        ["offline/rule-engine"],
    ]
    # Each candidate saw the pristine request (the failed ones' edits were rolled back).
    assert a.seen_lengths == b.seen_lengths == c.seen_lengths == [1]
    assert out[0].content.parts[0].text == "offline answer"
    assert out[0].custom_metadata == {
        "served_by": "offline/rule-engine",
        "fallbacks": 2,
        "tier": "fast",
    }
    k = kinds()
    assert k.count("fallback") == 2 and k[-1] == "recovered"
    fb = [e for e in journal.recent() if e["kind"] == "fallback"]
    assert {e["action"] for e in fb} == {"→ claude-b", "→ offline/rule-engine"}
    assert get_breaker("model:gemini-a").state == "open"  # bad key = permanent -> breaker trips at once


def test_transient_error_retried_in_place_before_fallback():
    a = FakeLlm(model="gemini-a", script=[chaos.ChaosError("boom"), TimeoutError(), "third time"])
    c = FakeLlm(model="offline/rule-engine")
    llm = ResilientLlm(chain=[a, c], retry_backoff=(0.0, 0.0))
    out = asyncio.run(collect(llm, make_request()))
    assert out[0].custom_metadata["served_by"] == "gemini-a"
    assert out[0].custom_metadata["fallbacks"] == 0
    assert a.seen_lengths == [1, 1, 1]  # restored between attempts
    assert c.seen_models == []
    assert kinds().count("retry") == 2 and kinds()[-1] == "recovered"


def test_chaos_injected_on_models_but_never_offline():
    chaos.set_rate(1.0)
    a = FakeLlm(model="gemini-a")
    c = FakeLlm(model="offline/rule-engine", script=["safe"])
    llm = ResilientLlm(chain=[a, c], retry_backoff=(0.0, 0.0))
    out = asyncio.run(collect(llm, make_request()))
    assert a.seen_models == []  # every attempt sabotaged before the call
    assert out[0].custom_metadata["served_by"] == "offline/rule-engine"
    assert kinds().count("fault") == 3


def test_open_breaker_skips_candidate_but_never_last():
    for _ in range(config.BREAKER_THRESHOLD):
        get_breaker("model:gemini-a").failure()
        get_breaker("model:offline/rule-engine").failure()
    a = FakeLlm(model="gemini-a")
    c = FakeLlm(model="offline/rule-engine", script=["still here"])
    llm = ResilientLlm(chain=[a, c], retry_backoff=())
    out = asyncio.run(collect(llm, make_request()))
    assert a.seen_models == [] and out[0].content.parts[0].text == "still here"
    assert out[0].custom_metadata["fallbacks"] == 1
    assert get_breaker("model:offline/rule-engine").state == "closed"


def test_all_fail_raises_last_error():
    a = FakeLlm(model="a", script=[ValueError("a")])
    b = FakeLlm(model="b", script=[KeyError("b")])
    llm = ResilientLlm(chain=[a, b], retry_backoff=())
    with pytest.raises(KeyError):
        asyncio.run(collect(llm, make_request()))
    assert [e for e in journal.recent() if e["kind"] == "fallback"][0]["outcome"] == "failed"


def test_no_fallback_after_first_streamed_chunk():
    s = StreamThenFail(model="streamer")
    c = FakeLlm(model="offline/rule-engine")
    llm = ResilientLlm(chain=[s, c], retry_backoff=(0.0,))
    with pytest.raises(ConnectionError):
        asyncio.run(collect(llm, make_request(), stream=True))
    assert c.seen_models == []


def test_build_model_chains(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "multi")
    from google.adk.models.google_llm import Gemini

    from cargomind.healing.offline_llm import OfflineLlm, OfflinePolicy

    policy = OfflinePolicy(agent="tester")
    monkeypatch.setattr(config, "online_gemini", lambda: True)
    monkeypatch.setattr(config, "FORCE_OFFLINE", False)
    monkeypatch.setattr(config, "GEMINI_FAST", "gem-fast")
    monkeypatch.setattr(config, "GEMINI_PRO", "gem-pro")
    monkeypatch.setattr(config, "GEMINI_BACKUP", "gem-backup")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(config, "OPENAI_API_KEY", None)

    fast = build_model("fast", policy)
    assert [m.model for m in fast.chain] == ["gem-fast", "gem-backup", "offline/rule-engine"]
    assert isinstance(fast.chain[0], Gemini) and fast.chain[0].retry_options.attempts == 1  # ResilientLlm owns retries
    assert isinstance(fast.chain[-1], OfflineLlm) and fast.chain[-1].policy.agent == "tester"

    monkeypatch.setattr(config, "GEMINI_BACKUP", "gem-pro")  # same as tier model: no dup
    assert [m.model for m in build_model("pro", policy).chain] == ["gem-pro", "offline/rule-engine"]

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    critic = build_model("critic", policy)
    assert [m.model for m in critic.chain] == [config.CLAUDE_MODEL, "gem-pro", "offline/rule-engine"]
    vision = build_model("vision", policy)
    assert isinstance(vision.chain[0], Gemini)
    assert vision.chain[-2].model == config.CLAUDE_MODEL  # cross-vendor AFTER Gemini

    monkeypatch.setattr(config, "online_gemini", lambda: False)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    offline = build_model("fast", policy)
    assert [m.model for m in offline.chain] == ["offline/rule-engine"]
    with pytest.raises(ValueError):
        build_model("nope", policy)


# ------------------------------------------------------------- plugin
class Ctx:
    """Minimal ToolContext stand-in (FunctionTool only needs it for injection)."""

    state: dict = {}
    invocation_id = "inv-test"


def test_normalize_id_cases():
    assert normalize_id("shp1003", "SHP") == "SHP-1003"
    assert normalize_id("SHP 1003", "SHP") == "SHP-1003"
    assert normalize_id("1003", "SHP") == "SHP-1003"
    assert normalize_id(1003, "SHP") == "SHP-1003"
    assert normalize_id("trk101", "TRK") == "TRK-101"
    assert normalize_id("wh-blr", "WH") == "WH-BLR"
    assert normalize_id("blr", "WH") == "WH-BLR"
    assert normalize_id("ord_5001", None) == "ORD-5001"
    assert normalize_id("SHP-1001", "SHP") == "SHP-1001"
    assert normalize_id("abc-def", None) == "abc-def"  # unknown: untouched


def test_before_tool_repairs_args_in_place_and_journals():
    plugin = SelfHealingPlugin()
    tool = BaseTool(name="track_shipment", description="t")
    args = {
        "shipment_id": "shp1003",
        "vehicle_id": "trk101",
        "warehouse_id": "wh-blr",
        "sku": "sku 12",
        "origin_city": "bangalore",
        "destination": "Bombay",
        "stops": ["madras", "Pune"],
        "note": "leave me alone",
    }
    res = asyncio.run(plugin.before_tool_callback(tool=tool, tool_args=args, tool_context=Ctx()))
    assert res is None
    assert args["shipment_id"] == "SHP-1003"
    assert args["vehicle_id"] == "TRK-101"
    assert args["warehouse_id"] == "WH-BLR"
    assert args["sku"] == "SKU-12"
    assert args["origin_city"] == "Bengaluru"
    assert args["destination"] == "Mumbai"
    assert args["stops"] == ["Chennai", "Pune"]
    assert args["note"] == "leave me alone"
    ev = journal.recent(1)[0]
    assert ev["kind"] == "arg_repair" and ev["component"] == "tool:track_shipment"
    assert repair_args({"shipment_id": "SHP-1001"}) == []


def _flaky_tool(failures: int, exc: type[Exception] = ConnectionError) -> tuple[FunctionTool, dict]:
    calls = {"n": 0}

    async def get_eta(shipment_id: str) -> dict:
        """Returns the ETA for a shipment."""
        calls["n"] += 1
        chaos.maybe_fail("tool:get_eta")
        if calls["n"] <= failures:
            raise exc(f"upstream hiccup #{calls['n']}")
        return {"status": "ok", "shipment_id": shipment_id, "eta": "2h"}

    return FunctionTool(get_eta), calls


def test_on_tool_error_retries_transient_and_recovers():
    plugin = SelfHealingPlugin(max_retries=2, backoff_base=0.0)
    tool, calls = _flaky_tool(failures=2)
    args = {"shipment_id": "SHP-1001"}

    async def main():
        try:
            await tool.run_async(args=args, tool_context=Ctx())
        except Exception as err:  # noqa: BLE001
            return await plugin.on_tool_error_callback(
                tool=tool, tool_args=args, tool_context=Ctx(), error=err
            )

    res = asyncio.run(main())
    assert res == {"status": "ok", "shipment_id": "SHP-1001", "eta": "2h"}
    assert calls["n"] == 3
    assert kinds() == ["retry", "retry", "recovered"]


def test_on_tool_error_retry_runs_without_chaos():
    plugin = SelfHealingPlugin(max_retries=1, backoff_base=0.0)
    tool, calls = _flaky_tool(failures=0)
    chaos.set_rate(1.0)
    args = {"shipment_id": "SHP-1001"}

    async def main():
        try:
            await tool.run_async(args=args, tool_context=Ctx())
        except chaos.ChaosError as err:
            return await plugin.on_tool_error_callback(
                tool=tool, tool_args=args, tool_context=Ctx(), error=err
            )

    res = asyncio.run(main())
    assert res["status"] == "ok"
    assert kinds() == ["fault", "retry", "recovered"]


def test_on_tool_error_non_transient_reflects_and_breaker_opens():
    plugin = SelfHealingPlugin(max_retries=2, backoff_base=0.0)
    tool, calls = _flaky_tool(failures=99, exc=KeyError)
    args = {"shipment_id": "SHP-1001"}

    async def once():
        try:
            await tool.run_async(args=args, tool_context=Ctx())
        except Exception as err:  # noqa: BLE001
            return await plugin.on_tool_error_callback(
                tool=tool, tool_args=args, tool_context=Ctx(), error=err
            )

    res = asyncio.run(once())
    assert res["status"] == "error" and res["healing_hint"] == HEALING_HINT
    assert res["tool"] == "get_eta" and res["transient"] is False
    assert calls["n"] == 1  # no retries for non-transient errors
    assert kinds() == ["reflect"]

    for _ in range(config.BREAKER_THRESHOLD - 1):
        asyncio.run(once())
    assert get_breaker("tool:get_eta").state == "open"
    short = asyncio.run(
        plugin.before_tool_callback(tool=tool, tool_args=dict(args), tool_context=Ctx())
    )
    assert short["status"] == "error" and short["degraded"] is True
    # after_tool must not double-journal a result the plugin already produced
    n = len(journal.recent(500))
    assert asyncio.run(
        plugin.after_tool_callback(tool=tool, tool_args=args, tool_context=Ctx(), result=short)
    ) is None
    assert len(journal.recent(500)) == n


def test_on_tool_error_unknown_tool_is_reflected_without_breaker():
    plugin = SelfHealingPlugin()
    tool = BaseTool(name="teleport", description="Tool not found")
    err = ValueError("Tool 'teleport' not found.\nAvailable tools: a, b\n\nPossible causes: ...")
    res = asyncio.run(
        plugin.on_tool_error_callback(tool=tool, tool_args={}, tool_context=Ctx(), error=err)
    )
    assert res["status"] == "error" and "Available tools: a, b" in res["error"]
    assert "Possible causes" not in res["error"]
    assert breaker_mod.peek_breaker("tool:teleport") is None


def test_after_tool_adds_hint_to_error_results():
    plugin = SelfHealingPlugin()
    tool = BaseTool(name="track_shipment", description="t")
    bad = {"status": "error", "error": "SHP-9999 not found", "suggestions": ["SHP-1001"]}
    out = asyncio.run(
        plugin.after_tool_callback(tool=tool, tool_args={}, tool_context=Ctx(), result=bad)
    )
    assert out["healing_hint"] == HEALING_HINT and out["suggestions"] == ["SHP-1001"]
    assert kinds() == ["reflect"]
    ok = asyncio.run(
        plugin.after_tool_callback(tool=tool, tool_args={}, tool_context=Ctx(), result={"status": "ok"})
    )
    assert ok is None


def test_on_model_error_returns_graceful_response():
    plugin = SelfHealingPlugin()
    resp = asyncio.run(
        plugin.on_model_error_callback(
            callback_context=Ctx(), llm_request=make_request(), error=RuntimeError("down")
        )
    )
    assert isinstance(resp, LlmResponse)
    assert "temporary problem" in resp.content.parts[0].text
    assert resp.custom_metadata["degraded"] is True
    assert kinds() == ["fallback"]
