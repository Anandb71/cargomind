"""Synthetic optimization, bounded inputs, evidence and real role-message boundaries."""
from __future__ import annotations

import copy
import sys
from itertools import pairwise

import pytest
from pydantic import ValidationError

from cargomind.simulation import engine
from cargomind.simulation.models import SimulationScenario, canonical_scenario, decision_hash, scenario_hash
from cargomind.simulation.protocol import AgentReply, ProtocolError, ProtocolSession, payload_hash


def test_exact_optimizer_beats_separate_greedy_baseline():
    scenario = engine.sample_scenario()
    exact = engine.solve_scenario(scenario)
    greedy = engine.greedy_baseline(scenario)
    assert exact["complete"] and exact["reason"] == "optimal"
    assert exact["decision"]["path"] == ["Origin", "FastHub", "Destination"]
    assert greedy["decision"]["path"] == ["Origin", "GreedyHub", "Destination"]
    assert exact["decision"]["objective_value"] < greedy["decision"]["objective_value"]
    assert "BlockedFast" not in exact["decision"]["edge_ids"]
    assert exact["decision"]["evidence_hash"] == decision_hash(scenario, exact["decision"])


def test_weights_change_optimal_decision_without_hardcoded_route():
    scenario = engine.sample_scenario()
    scenario["weights"] = {"time": 0, "cost": 1, "risk": 0, "distance": 0}
    decision = engine.solve_scenario(scenario)["decision"]
    assert decision["path"] == ["Origin", "EconomyHub", "Destination"]
    assert decision["totals"]["cost"] == 4


@pytest.mark.parametrize("constraint", ["blocked", "capacity", "cold_chain", "deadline", "risk"])
def test_no_route_proof_obeys_every_hard_constraint(constraint):
    scenario = engine.sample_scenario()
    if constraint == "blocked":
        for edge in scenario["edges"]:
            edge["blocked"] = True
    elif constraint == "capacity":
        for edge in scenario["edges"]:
            edge["capacity_kg"] = 499
    elif constraint == "cold_chain":
        for edge in scenario["edges"]:
            edge["cold_chain"] = False
    elif constraint == "deadline":
        scenario["deadline_minutes"] = 10
    else:
        scenario["max_risk"] = 0.015
    result = engine.solve_scenario(scenario)
    assert result["complete"] and result["decision"] is None
    assert result["reason"] == "no_feasible_route"


def test_risk_budget_applies_to_complete_path_not_each_edge():
    scenario = engine.sample_scenario()
    scenario["max_risk"] = 0.03
    result = engine.solve_scenario(scenario)
    assert result["decision"]["path"] == ["Origin", "EconomyHub", "Destination"]
    assert result["decision"]["totals"]["risk"] == pytest.approx(0.02)


def test_parallel_edges_and_zero_cost_cycles_do_not_break_exact_search():
    scenario = {
        "nodes": [{"id": node} for node in ("A", "B", "C")],
        "origin": "A", "destination": "C", "shipment_weight_kg": 0,
        "weights": {"time": 0, "cost": 1, "risk": 0, "distance": 0},
        "edges": [
            {"id": "AB_cheap", "source": "A", "target": "B", "distance_km": 0,
             "cost": 0, "travel_minutes": 0, "risk": 0},
            {"id": "AB_costly", "source": "A", "target": "B", "distance_km": 0,
             "cost": 3, "travel_minutes": 0, "risk": 0},
            {"id": "BA_cycle", "source": "B", "target": "A", "distance_km": 0,
             "cost": 0, "travel_minutes": 0, "risk": 0},
            {"id": "BC", "source": "B", "target": "C", "distance_km": 0,
             "cost": 1, "travel_minutes": 0, "risk": 0},
        ],
    }
    decision = engine.solve_scenario(scenario)["decision"]
    assert decision["edge_ids"] == ["AB_cheap", "BC"]
    assert decision["path"] == ["A", "B", "C"]


def test_origin_equals_destination_has_verified_empty_path():
    scenario = {"nodes": [{"id": "A"}], "origin": "A", "destination": "A", "shipment_weight_kg": 0}
    result = engine.solve_scenario(scenario)
    assert result["complete"]
    assert result["decision"]["path"] == ["A"] and result["decision"]["edge_ids"] == []
    assert result["decision"]["objective_value"] == 0


def test_canonical_scenario_hash_is_order_independent_and_includes_defaults():
    scenario = engine.sample_scenario()
    changed = copy.deepcopy(scenario)
    changed["nodes"].reverse()
    changed["edges"].reverse()
    assert scenario_hash(changed) == scenario_hash(scenario)
    minimal = {"nodes": [{"id": "A"}], "origin": "A", "destination": "A", "shipment_weight_kg": 0}
    assert scenario_hash(minimal) == scenario_hash(canonical_scenario(minimal))
    changed["edges"][0]["cost"] += 1
    assert scenario_hash(changed) != scenario_hash(scenario)


@pytest.mark.parametrize("mutation", ["notes", "pii", "endpoint", "duplicate", "nan", "negative", "weights", "size", "bool"])
def test_untrusted_scenario_is_bounded_and_rejects_unknown_fields(mutation):
    scenario = engine.sample_scenario()
    if mutation == "notes":
        scenario["notes"] = "not accepted"
    elif mutation == "pii":
        scenario["nodes"][0]["email"] = "not accepted"
    elif mutation == "endpoint":
        scenario["edges"][0]["target"] = "Missing"
    elif mutation == "duplicate":
        scenario["edges"][1]["id"] = scenario["edges"][0]["id"]
    elif mutation == "nan":
        scenario["edges"][0]["cost"] = float("nan")
    elif mutation == "negative":
        scenario["shipment_weight_kg"] = -1
    elif mutation == "weights":
        scenario["weights"] = {"time": 0, "cost": 0, "risk": 0, "distance": 0}
    elif mutation == "size":
        scenario["nodes"] += [{"id": f"Extra{i}"} for i in range(64)]
    else:
        scenario["edges"][0]["blocked"] = "false"
    with pytest.raises(ValidationError):
        SimulationScenario.model_validate(scenario)
    result = engine.run_simulation(scenario)
    assert result["status"] == "rejected" and result["decision"] is None
    assert "not accepted" not in str(result)


def test_budget_exhaustion_never_claims_no_route_or_approval(monkeypatch):
    assert not engine.solve_scenario(engine.sample_scenario(), max_expansions=0)["complete"]
    monkeypatch.setattr(engine, "solve_scenario", lambda *_: {
        "complete": False, "decision": None, "reason": "search_budget_exceeded", "expanded": 0,
    })
    result = engine.run_simulation(engine.sample_scenario())
    assert result["status"] == "rejected" and not result["evaluation"]["approved"]


def test_missing_independent_evaluator_blocks_output(monkeypatch):
    monkeypatch.setitem(sys.modules, "cargomind.simulation.evaluator", None)
    result = engine.run_simulation(engine.sample_scenario())
    assert result["status"] == "rejected" and result["decision"] is None
    assert "evaluator" in str(result["evaluation"]).lower()


@pytest.mark.parametrize("value", ["no_route", None, [], {"approved": "yes", "optimal": True, "status": "approved"}])
def test_malformed_oracle_verdicts_fail_closed(monkeypatch, value):
    from types import SimpleNamespace

    monkeypatch.setitem(sys.modules, "cargomind.simulation.evaluator", SimpleNamespace(
        evaluate_decision=lambda *_: value,
    ))
    result = engine.run_simulation(engine.sample_scenario())
    assert result["status"] == "rejected" and result["decision"] is None


@pytest.mark.parametrize("flag", ["passed", "grounded", "truthful", "constraints_satisfied"])
@pytest.mark.parametrize("contradiction", [False, None, 1, "true"])
def test_output_gate_requires_all_independent_evidence_flags_literal_true(monkeypatch, flag, contradiction):
    from cargomind.simulation import evaluator

    scenario = engine.sample_scenario()
    verdict = evaluator.evaluate_decision(scenario, engine.solve_scenario(scenario)["decision"])
    assert verdict["approved"] is True
    verdict[flag] = contradiction
    monkeypatch.setattr(evaluator, "evaluate_decision", lambda *_: verdict)
    result = engine.run_simulation(scenario)
    assert result["status"] == "rejected" and result["decision"] is None


@pytest.mark.parametrize("value", [[], None, 42, "scenario"])
def test_array_scalar_and_null_scenarios_are_rejected_without_crashing(value):
    result = engine.run_simulation(value)
    assert result["status"] == "rejected" and result["decision"] is None


@pytest.mark.parametrize(("field", "value"), [
    ("blocked", "false"), ("blocked", "0"), ("blocked", 0), ("blocked", 1),
    ("cold_chain", "true"), ("cold_chain", 1), ("capacity_kg", True),
    ("id", 7),
])
def test_edge_types_are_strict_and_do_not_coerce_identifiers_or_booleans(field, value):
    scenario = engine.sample_scenario()
    scenario["edges"][0][field] = value
    assert engine.run_simulation(scenario)["status"] == "rejected"


def test_large_valid_aggregates_are_supported_instead_of_crashing():
    scenario = {
        "nodes": [{"id": name} for name in ("A", "B", "C")], "origin": "A", "destination": "C",
        "shipment_weight_kg": 0,
        "edges": [{"id": edge, "source": source, "target": target, "cost": 1e9,
                   "distance_km": 1e9, "travel_minutes": 1e9, "risk": 0}
                  for edge, source, target in (("AB", "A", "B"), ("BC", "B", "C"))],
    }
    result = engine.solve_scenario(scenario)
    assert result["complete"] and result["decision"]["totals"]["cost"] == 2e9
    released = engine.run_simulation(scenario)
    assert released["status"] == "approved", released["evaluation"]


def _decimal_boundary_scenario():
    return {
        "nodes": [{"id": name} for name in ("A", "B", "C")], "origin": "A", "destination": "C",
        "shipment_weight_kg": 0, "cold_chain_required": True, "deadline_minutes": 0.3, "max_risk": 0.3,
        "edges": [{"id": edge, "source": source, "target": target, "cost": 0, "distance_km": 0,
                   "travel_minutes": amount, "risk": amount, "capacity_kg": 0, "cold_chain": True}
                  for edge, source, target, amount in (("AB", "A", "B", 0.1), ("BC", "B", "C", 0.2))],
    }


def test_decimal_budget_equality_accepts_zero_capacity_and_zero_cost_cold_chain():
    scenario = _decimal_boundary_scenario()
    result = engine.solve_scenario(scenario)
    assert result["decision"]["totals"]["risk"] == 0.3
    assert result["decision"]["totals"]["travel_minutes"] == 0.3
    assert result["decision"]["totals"]["cost"] == 0
    released = engine.run_simulation(scenario)
    assert released["status"] == "approved", released["evaluation"]


@pytest.mark.parametrize("constraint", ["risk", "deadline", "capacity", "cold_chain"])
def test_true_tiny_hard_constraint_violations_are_not_hidden_by_epsilon(constraint):
    scenario = _decimal_boundary_scenario()
    if constraint == "risk":
        scenario["max_risk"] = 0.299999999999
    elif constraint == "deadline":
        scenario["deadline_minutes"] = 0.299999999999
    elif constraint == "capacity":
        scenario["shipment_weight_kg"] = 1e-12
    else:
        scenario["edges"][0]["cold_chain"] = False
    result = engine.solve_scenario(scenario)
    assert result["complete"] and result["decision"] is None


def test_exact_zero_deadline_risk_and_capacity_are_feasible_without_motion_costs():
    scenario = _decimal_boundary_scenario()
    scenario.update(deadline_minutes=0, max_risk=0)
    for edge in scenario["edges"]:
        edge.update(travel_minutes=0, risk=0)
    result = engine.run_simulation(scenario)
    assert result["status"] == "approved", result["evaluation"]
    assert result["decision"]["objective_value"] == 0


@pytest.mark.parametrize("field", ["risk", "travel_minutes"])
def test_decimal_budget_rejects_subnormal_overage_without_rounding_it_away(field):
    scenario = _decimal_boundary_scenario()
    scenario["nodes"].append({"id": "D"})
    scenario["destination"] = "D"
    final_edge = {"id": "CD", "source": "C", "target": "D", "cost": 0, "distance_km": 0,
                  "travel_minutes": 0, "risk": 0, "capacity_kg": 0, "cold_chain": True}
    final_edge[field] = 5e-324
    scenario["edges"].append(final_edge)
    assert engine.solve_scenario(scenario)["decision"] is None
    result = engine.run_simulation(scenario)
    assert result["status"] == "no_route", result["evaluation"]


def test_protocol_correlates_messages_and_enforces_role_hop_timeout_boundaries(monkeypatch):
    session = ProtocolSession("sim-" + "a" * 24, max_hops=1)
    with pytest.raises(ProtocolError):
        session.register("rogue_agent", lambda _: None)
    session.register("constraint_agent", lambda message: AgentReply(
        recipient="output_gate", status="rejected", payload={"reason": "test"},
    ))
    with pytest.raises(ProtocolError, match="hop budget"):
        session.run({"scenario": {}})

    session = ProtocolSession("sim-" + "a" * 24)
    session.register("constraint_agent", lambda _: AgentReply(recipient="evaluator_agent", status="validated", payload={}))
    with pytest.raises(ProtocolError, match="transition"):
        session.run({})

    session = ProtocolSession("sim-" + "a" * 24, timeout_seconds=0)
    with pytest.raises(ProtocolError, match="time budget"):
        session.run({})


def test_protocol_rejects_payload_tampering():
    from cargomind.simulation.models import canonical_json

    session = ProtocolSession("sim-" + "a" * 24)
    metadata = {"sender": "operator", "recipient": "constraint_agent", "task_id": session.task_id,
                "parent_id": None, "protocol_version": "1.0", "evidence_hash": payload_hash({"original": True}),
                "status": "requested", "hop": 0, "payload": {"tampered": True}}
    wire = canonical_json({**metadata, "message_id": payload_hash(metadata)})
    with pytest.raises(ProtocolError, match="evidence hash"):
        session._receive(wire, previous=None, expected_hop=0)


def test_real_agents_exchange_json_and_gate_independent_evaluation():
    pytest.importorskip("cargomind.simulation.evaluator")
    result = engine.run_simulation_example()
    assert result["status"] == "approved", result["evaluation"]
    handoffs = result["handoffs"]
    assert [message["recipient"] for message in handoffs] == [
        "constraint_agent", "optimizer_agent", "evaluator_agent", "output_gate", "operator",
    ]
    assert len({message["task_id"] for message in handoffs}) == 1
    assert handoffs[0]["parent_id"] is None
    assert all(current["parent_id"] == previous["message_id"] for previous, current in pairwise(handoffs))
    assert result["agent_kind"] == "deterministic" and result["protocol_kind"] == "custom_json"
    assert result["decision"] == engine.solve_scenario(engine.sample_scenario())["decision"]
