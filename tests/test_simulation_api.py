"""Exercise actual HTTP/privacy boundaries, not only optimizer helper functions."""
import copy

import pytest
from fastapi.testclient import TestClient

from cargomind.simulation.engine import sample_scenario
from cargomind.web.server import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CARGOMIND_WATCHTOWER", "0")
    return TestClient(app)


def test_api_releases_only_verified_decision_and_keeps_protocol_roles(client):
    result = client.post("/api/simulation/run", json=sample_scenario()).json()
    assert result["status"] == "approved"
    assert result["evaluation"]["passed"] is True
    assert result["decision"]["path"] == ["Origin", "FastHub", "Destination"]
    assert result["handoffs"][0]["recipient"] == "constraint_agent"
    assert result["handoffs"][-1]["recipient"] == "operator"
    forged = copy.deepcopy(result["decision"])
    forged["totals"]["cost"] += 1
    check = client.post("/api/simulation/evaluate", json={"scenario": sample_scenario(), "decision": forged}).json()
    assert check["passed"] is False
    assert check["truthful"] is False


def test_privacy_boundary_does_not_echo_or_dispatch_sensitive_input(client, monkeypatch):
    from cargomind import runtime
    monkeypatch.setattr(runtime, "get_runner", lambda: pytest.fail("Unsafe input reached the agent runner"))
    secret = "operator@example.invalid"
    chat = client.post("/api/chat", json={"session_id": "privacy-test", "message": f"Notify {secret}"})
    assert chat.status_code == 200 and "guardrail/input" in chat.text
    assert secret not in chat.text
    payload = {**sample_scenario(), "customer_email": secret}
    response = client.post("/api/simulation/run", json=payload)
    assert response.status_code == 422 and secret not in response.text
    validation = client.post("/api/chat", json={"session_id": "safe", "message": "Track SHP-1003", "email": secret})
    assert validation.status_code == 422 and secret not in validation.text


@pytest.mark.parametrize("body", ["{\"risk\":0,\"risk\":1}", "{\"cost\":NaN}", "[]", "{", '{"deep":'+ '['*1100+'0'+']'*1100+'}'],
                         ids=["duplicate", "nan", "array", "malformed", "deep"])
def test_bad_or_ambiguous_wire_json_rejected_without_crash(client, body):
    response = client.post("/api/simulation/run", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code in (400, 422)


def test_large_request_and_extra_personal_fields_rejected(client):
    response = client.post("/api/simulation/run", content='{"x":"'+'a'*262_144+'"}')
    assert response.status_code == 413
    response = client.post("/api/simulation/run", json={**sample_scenario(), "driver_name": "Person Example"})
    assert response.status_code == 422
    assert "Person Example" not in response.text


def test_custom_rpc_has_closed_methods_and_real_correlated_handoffs(client):
    card = client.get("/.well-known/agent-card.json").json()
    assert card["standard_a2a_compliant"] is False
    envelope = {"jsonrpc": "2.0", "id": "task-1", "method": "simulation.run", "params": sample_scenario()}
    result = client.post("/api/a2a", json=envelope).json()
    assert result["id"] == "task-1" and result["result"]["status"] == "approved"
    assert len(result["result"]["handoffs"]) == 5
    assert client.post("/api/a2a", json={**envelope, "method": "shell.execute"}).json()["error"]["code"] == -32601


def test_missing_evaluator_and_capacity_limit_do_not_release_proposal(client, monkeypatch):
    from cargomind.simulation import evaluator
    from cargomind.web import simulation
    def unavailable(*args, **kwargs):
        raise RuntimeError("Unavailable verifier")
    monkeypatch.setattr(evaluator, "evaluate_decision", unavailable)
    result = client.post("/api/simulation/run", json=sample_scenario()).json()
    assert result["status"] == "rejected" and result["decision"] is None
    held = 0
    try:
        while simulation._slots.acquire(blocking=False):
            held += 1
        assert client.post("/api/simulation/run", json=sample_scenario()).status_code == 503
    finally:
        for _ in range(held):
            simulation._slots.release()


def test_images_not_accepted_into_operational_boundary(client):
    assert client.post("/api/upload", json={"data_b64": "AAA="}).status_code == 403
    assert client.get("/api/uploads/"+'0'*32+'.png').status_code == 403


def test_schema_and_demonstrator_available(client):
    assert client.get("/api/simulation/schema").json()["additionalProperties"] is False
    assert client.get("/api/simulation/example").json() == sample_scenario()
    response = client.get("/simulation")
    assert response.status_code == 200 and "Run agents" in response.text
