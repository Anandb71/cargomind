"""The offline rule-engine must drive real tool calls from plain language."""
import os

os.environ.setdefault("CARGOMIND_OFFLINE", "1")

from google.adk.models import LlmRequest  # noqa: E402
from google.adk.tools import FunctionTool  # noqa: E402
from google.genai import types  # noqa: E402

from cargomind.healing.offline_llm import OfflineLlm, OfflinePolicy, OfflineRule, render  # noqa: E402


def track_shipment(shipment_id: str) -> dict:
    """Track a shipment by id."""
    return {"id": shipment_id}


def quote_carriers(origin: str, destination: str, weight_kg: float, priority: str = "standard") -> dict:
    """Quote carriers."""
    return {}


def exit_loop() -> None:
    """Exit."""


def _req(contents, *fns):
    tools = {f.__name__: FunctionTool(f) for f in fns}
    return LlmRequest(contents=contents, tools_dict=tools)


def _user(text):
    return types.Content(role="user", parts=[types.Part(text=text)])


def test_extracts_shipment_id_and_calls_tool():
    llm = OfflineLlm(policy=OfflinePolicy(rules=[OfflineRule(tool="track_shipment", keywords=["where"])]))
    resp = llm._respond(_req([_user("where is shp 1003 right now?")], track_shipment))
    fc = resp.content.parts[0].function_call
    assert fc.name == "track_shipment" and fc.args == {"shipment_id": "SHP-1003"}


def test_extracts_cities_weight_priority():
    llm = OfflineLlm(policy=OfflinePolicy(rules=[OfflineRule(tool="quote_carriers", keywords=["quote"])]))
    resp = llm._respond(_req([_user("Quote carriers Delhi to Bangalore 800kg express")], quote_carriers))
    args = resp.content.parts[0].function_call.args
    assert args["weight_kg"] == 800.0 and args["priority"] == "express"
    assert args.get("origin") == "Delhi" and args.get("destination") == "Bengaluru"


def test_summarises_function_response():
    llm = OfflineLlm(policy=OfflinePolicy(title="Tracking"))
    contents = [
        _user("where is SHP-1001"),
        types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name="track_shipment", args={}))]),
        types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
            name="track_shipment", response={"id": "SHP-1001", "status": "in_transit"}))]),
    ]
    text = llm._respond(_req(contents, track_shipment)).content.parts[0].text
    assert "SHP-1001" in text and "in_transit" in text


def test_critic_exits_loop_when_valid():
    llm = OfflineLlm(policy=OfflinePolicy(default_tool="track_shipment", finish_tool="exit_loop"))
    contents = [
        _user("validate"),
        types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name="track_shipment", args={}))]),
        types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
            name="track_shipment", response={"valid": True}))]),
    ]
    resp = llm._respond(_req(contents, track_shipment, exit_loop))
    assert resp.content.parts[0].function_call.name == "exit_loop"


def test_render_table():
    out = render({"shipments": [{"id": "SHP-1", "status": "delayed"}, {"id": "SHP-2", "status": "in_transit"}]})
    assert "| id | status |" in out


def test_which_is_not_a_warehouse_id_and_tables_keep_status():
    from cargomind.healing.offline_llm import _ID_PATTERNS, _table
    import re

    pat = _ID_PATTERNS["warehouse"][0]
    assert re.search(pat, "Which SKUs are low on stock?", re.I) is None
    assert re.search(pat, "which ones", re.I) is None
    for t in ("stock at WH-BLR", "stock at WHBLR", "stock at wh-blr"):
        assert re.search(pat, t, re.I).group(1).upper() == "BLR"
    rows = [{"id": "SHP-1", **{f"c{i}": i for i in range(8)}, "status": "delayed"}]
    assert "delayed" in _table(rows) and "status" in _table(rows).splitlines()[0]
