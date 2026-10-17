"""Local adversarial transport checks; no hosted requests, credentials or network use."""
from __future__ import annotations

import asyncio
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from cargomind.simulation.engine import run_simulation, sample_scenario
from cargomind.simulation.models import canonical_json
from cargomind.simulation.protocol import ProtocolError, ProtocolSession, payload_hash
from cargomind.web.server import app


def _wire(session, **changes):
    metadata = {"sender": "operator", "recipient": "constraint_agent", "task_id": session.task_id,
                "parent_id": None, "protocol_version": "1.0", "evidence_hash": payload_hash({}),
                "status": "requested", "hop": 0, "payload": {}}
    metadata.update(changes)
    return canonical_json({**metadata, "message_id": payload_hash(metadata)})


def test_first_hop_cannot_impersonate_evaluator_and_skip_verification():
    session = ProtocolSession("sim-" + "a" * 24)
    wire = _wire(session, sender="evaluator_agent", recipient="output_gate", status="evaluated")
    with pytest.raises(ProtocolError, match="Initial handoff"):
        session._receive(wire, previous=None, expected_hop=0)


@pytest.mark.parametrize("status", ["approved", "no_route", "solved", "rejected"])
def test_operator_cannot_inject_an_authoritative_status(status):
    session = ProtocolSession("sim-" + "a" * 24)
    with pytest.raises(ProtocolError, match="status"):
        session._receive(_wire(session, status=status), previous=None, expected_hop=0)


def test_same_wire_cannot_be_replayed_inside_or_across_exchanges():
    first = ProtocolSession("sim-" + "a" * 24)
    wire = _wire(first)
    first._receive(wire, previous=None, expected_hop=0)
    with pytest.raises(ProtocolError, match="replay"):
        first._receive(wire, previous=None, expected_hop=0)
    second = ProtocolSession("sim-" + "b" * 24)
    with pytest.raises(ProtocolError, match="task"):
        second._receive(wire, previous=None, expected_hop=0)


@pytest.mark.parametrize("attack", ["wrong_parent", "wrong_sender", "hop_jump", "false_approval"])
def test_self_hashed_messages_must_still_follow_the_validated_chain(attack):
    session = ProtocolSession("sim-" + "a" * 24)
    first = session._receive(_wire(session), previous=None, expected_hop=0)
    changes = {"sender": "constraint_agent", "recipient": "optimizer_agent", "status": "validated",
               "parent_id": first.message_id, "hop": 1}
    if attack == "wrong_parent":
        changes["parent_id"] = "b" * 64
    elif attack == "wrong_sender":
        changes.update(sender="optimizer_agent", recipient="evaluator_agent", status="solved")
    elif attack == "hop_jump":
        changes["hop"] = 3
    else:
        changes.update(recipient="output_gate", status="approved")
    with pytest.raises(ProtocolError):
        session._receive(_wire(session, **changes), previous=first, expected_hop=changes["hop"])


def test_repeated_scenarios_have_unique_tasks_but_stable_evidence():
    first, second = run_simulation(sample_scenario()), run_simulation(sample_scenario())
    assert first["status"] == second["status"] == "approved"
    assert first["handoffs"][0]["task_id"] != second["handoffs"][0]["task_id"]
    assert first["scenario_hash"] == second["scenario_hash"]
    assert first["decision"]["evidence_hash"] == second["decision"]["evidence_hash"]


@pytest.mark.parametrize("endpoint", ["/api/simulation/run", "/api/a2a"])
def test_nested_bodies_are_rejected_before_privacy_or_solver(endpoint):
    client = TestClient(app, raise_server_exceptions=False)
    body = '{"deep":' + "[" * 100 + "0" + "]" * 100 + "}"
    response = client.post(endpoint, content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400


def test_http_cannot_supply_its_own_evaluator_or_role_identity():
    client = TestClient(app)
    payload = {**sample_scenario(), "evaluation": {"approved": True, "optimal": True, "status": "approved"}}
    response = client.post("/api/simulation/run", json=payload)
    assert response.json()["status"] == "rejected"
    envelope = {"jsonrpc": "2.0", "id": "attack", "method": "simulation.run", "params": sample_scenario(),
                "sender": "evaluator_agent"}
    assert client.post("/api/a2a", json=envelope).json()["error"]["code"] == -32600
    envelope.pop("sender")
    envelope["params"] = payload
    result = client.post("/api/a2a", json=envelope).json()["result"]
    assert result["status"] == "rejected" and result["decision"] is None


@pytest.mark.asyncio
async def test_four_real_concurrent_jobs_are_bounded_and_busy_service_recovers(monkeypatch):
    from cargomind.web import simulation

    entered, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    active = 0

    def bounded_work(_):
        nonlocal active
        with lock:
            active += 1
            if active == 4:
                entered.set()
        try:
            assert release.wait(3), "Test workers were not released"
            return {"status": "rejected", "decision": None}
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(simulation, "_slots", threading.BoundedSemaphore(4))
    monkeypatch.setattr(simulation, "run_simulation", bounded_work)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
        jobs = [asyncio.create_task(client.post("/api/simulation/run", json=sample_scenario())) for _ in range(4)]
        try:
            assert await asyncio.to_thread(entered.wait, 2), "The four worker slots were not occupied"
            busy = await client.post("/api/simulation/run", json=sample_scenario())
            assert busy.status_code == 503
        finally:
            release.set()
            responses = await asyncio.gather(*jobs)
        assert all(response.status_code == 200 for response in responses)
        assert (await client.post("/api/simulation/run", json=sample_scenario())).status_code == 200


@pytest.mark.asyncio
async def test_failed_worker_releases_its_concurrency_slot(monkeypatch):
    from cargomind.web import simulation

    def failed(_):
        raise RuntimeError("Synthetic worker failure")

    monkeypatch.setattr(simulation, "_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(simulation, "run_simulation", failed)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                 base_url="http://local") as client:
        assert (await client.post("/api/simulation/run", json=sample_scenario())).status_code == 500
        monkeypatch.setattr(simulation, "run_simulation", lambda _: {"status": "rejected", "decision": None})
        assert (await client.post("/api/simulation/run", json=sample_scenario())).status_code == 200
