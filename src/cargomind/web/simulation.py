"""Bounded operational-only HTTP and custom JSON-RPC agent transport."""
from __future__ import annotations

import json
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from cargomind.safety.privacy import inspect_input, sanitize
from cargomind.simulation.engine import run_simulation, sample_scenario
from cargomind.simulation.evaluator import evaluate_decision
from cargomind.simulation.models import SimulationScenario
from cargomind.simulation.parsing import MAX_BODY, InvalidPayload, strict_object

router = APIRouter()
_slots = threading.BoundedSemaphore(4)
AGENTS = [
    {"id": "constraint_agent", "permissions": ["validate_scenario", "filter_edges"]},
    {"id": "optimizer_agent", "permissions": ["propose_route"]},
    {"id": "evaluator_agent", "permissions": ["verify_evidence", "recompute_totals", "verify_optimality"]},
    {"id": "output_gate", "permissions": ["approve_verified_proposal", "reject"]},
]


async def _payload(request: Request) -> dict:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY:
            raise HTTPException(413, "Scenario exceeds the bounded request size.")
    try:
        payload = strict_object(raw)
    except InvalidPayload as exc:
        raise HTTPException(400, str(exc)) from None
    if not inspect_input(json.dumps(payload))["allowed"]:
        raise HTTPException(422, "Privacy guard: use operational IDs and logistics attributes only.")
    return payload


async def _bounded_call(fn, *args):
    if not _slots.acquire(blocking=False):
        raise HTTPException(503, "Simulation busy; retry after the current bounded tasks finish.")
    try:
        started = time.perf_counter()
        result = await run_in_threadpool(fn, *args)
        if isinstance(result, dict):
            result["execution_ms"] = round((time.perf_counter()-started)*1000, 3)
        return result
    finally:
        _slots.release()


def _evaluate(payload: dict) -> dict:
    if set(payload) != {"scenario", "decision"}:
        raise HTTPException(422, "Expected only scenario and decision.")
    return evaluate_decision(payload["scenario"], payload["decision"])


@router.get("/simulation", include_in_schema=False)
def simulation_page():
    from cargomind.web.server import STATIC_DIR
    return FileResponse(STATIC_DIR / "simulation.html", headers={"Cache-Control": "no-cache"})


@router.get("/api/simulation/schema")
def schema():
    return SimulationScenario.model_json_schema()


@router.get("/api/simulation/example")
def example():
    return sample_scenario()


@router.post("/api/simulation/run")
async def run(request: Request):
    payload = await _payload(request)
    result = await _bounded_call(run_simulation, payload)
    return JSONResponse(sanitize(result))


@router.post("/api/simulation/evaluate")
async def evaluate(request: Request):
    payload = await _payload(request)
    result = await _bounded_call(_evaluate, payload)
    return JSONResponse(sanitize(result))


@router.get("/.well-known/agent-card.json")
def agent_card():
    return {
        "name": "CargoMind Simulation", "version": "1.0",
        "protocol": "CargoMind JSON handoff 1.0 over JSON-RPC 2.0",
        "transport": "/api/a2a", "standard_a2a_compliant": False,
        "methods": ["agents.list", "simulation.run", "decision.evaluate"],
        "agents": AGENTS, "actions": "read-only simulation proposals",
        "guardrails": ["strict_schema", "no_personal_fields", "bounded_search", "independent_evaluator"],
    }


@router.post("/api/a2a")
async def agent_rpc(request: Request):
    payload = await _payload(request)
    rpc_id = payload.get("id")
    if isinstance(rpc_id, bool) or not isinstance(rpc_id, (str, int, type(None))):
        rpc_id = None
    if payload.get("jsonrpc") != "2.0" or set(payload) - {"jsonrpc", "id", "method", "params"}:
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id,
                             "error": {"code": -32600, "message": "Invalid request"}})
    method, params = payload.get("method"), payload.get("params", {})
    if not isinstance(params, dict):
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id,
                             "error": {"code": -32602, "message": "Expected object params"}})
    if method == "agents.list":
        result = {"agents": AGENTS, "protocol_version": "1.0"}
    elif method == "simulation.run":
        result = await _bounded_call(run_simulation, params)
    elif method == "decision.evaluate":
        result = await _bounded_call(_evaluate, params)
    else:
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id,
                             "error": {"code": -32601, "message": "Method not found"}})
    return JSONResponse(sanitize({"jsonrpc": "2.0", "id": rpc_id, "result": result}))
