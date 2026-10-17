"""Verifier challenges use hand-authored routes, including re-signed false claims."""
from __future__ import annotations

import copy

import pytest

from cargomind.evals.simulation_benchmark import generate_scenarios, markdown_report, run
from cargomind.simulation.evaluator import evaluate_decision
from cargomind.simulation.models import decision_hash


@pytest.fixture
def scenario():
    def edge(identifier, source, target, cost, minutes, distance, risk):
        return {"id": identifier, "source": source, "target": target, "cost": float(cost),
                "travel_minutes": float(minutes), "distance_km": float(distance), "risk": risk,
                "blocked": False, "capacity_kg": 100.0, "cold_chain": True}
    return {"nodes": [{"id": node} for node in "ABCD"],
            "edges": [edge("AB", "A", "B", 2, 3, 2, 0.05), edge("BD", "B", "D", 2, 3, 2, 0.05),
                      edge("AC", "A", "C", 5, 1, 1, 0.1), edge("CD", "C", "D", 5, 1, 1, 0.1),
                      edge("AD", "A", "D", 20, 1, 4, 0.4)],
            "origin": "A", "destination": "D", "shipment_weight_kg": 50.0,
            "cold_chain_required": False, "deadline_minutes": None, "max_risk": 1.0,
            "weights": {"time": 0.0, "cost": 1.0, "risk": 0.0, "distance": 0.0}}


def signed(scenario, decision):
    result = copy.deepcopy(decision)
    result["evidence_hash"] = decision_hash(scenario, result)
    return result


def cheap_route(scenario):
    return signed(scenario, {"path": ["A", "B", "D"], "edge_ids": ["AB", "BD"],
                            "totals": {"distance_km": 4.0, "cost": 4.0, "travel_minutes": 6.0, "risk": 0.1},
                            "objective_value": 0.2})


def test_hand_authored_optimal_route_is_approved(scenario):
    checked = evaluate_decision(scenario, cheap_route(scenario))
    assert checked["approved"] and checked["passed"]
    assert checked["grounded"] and checked["truthful"] and checked["constraints_satisfied"]
    assert checked["optimal"] and checked["metrics"]["oracle_complete"]
    assert checked["metrics"]["best_objective"] == pytest.approx(0.2)
    assert checked["metrics"]["objective_gap"] == pytest.approx(0)


@pytest.mark.parametrize("field", ["distance_km", "cost", "travel_minutes", "risk"])
def test_resigned_invented_totals_are_rejected(scenario, field):
    decision = cheap_route(scenario)
    decision["totals"][field] += 1
    checked = evaluate_decision(scenario, signed(scenario, decision))
    assert not checked["truthful"] and not checked["approved"]
    assert checked["metrics"]["evidence_hash_valid"]


@pytest.mark.parametrize("kind", ["invented_node", "invented_edge", "discontinuous_edge", "cycle", "extra_claim", "objective"])
def test_resigned_adversarial_decisions_are_rejected(scenario, kind):
    decision = cheap_route(scenario)
    if kind == "invented_node":
        decision["path"][1] = "Imaginary"
    elif kind == "invented_edge":
        decision["edge_ids"][0] = "Imaginary"
    elif kind == "discontinuous_edge":
        decision["edge_ids"] = ["AC", "BD"]
    elif kind == "cycle":
        decision["path"] = ["A", "A", "D"]
    elif kind == "extra_claim":
        decision["customer_notified"] = True
    elif kind == "objective":
        decision["objective_value"] = -100
    checked = evaluate_decision(scenario, signed(scenario, decision))
    assert not checked["approved"] and checked["issues"]


@pytest.mark.parametrize("constraint", ["closed", "capacity", "cold_chain", "deadline", "risk"])
def test_all_hard_constraints_checked_even_with_valid_signature(scenario, constraint):
    if constraint == "closed":
        scenario["edges"][0]["blocked"] = True
    elif constraint == "capacity":
        scenario["edges"][0]["capacity_kg"] = 49.0
    elif constraint == "cold_chain":
        scenario["cold_chain_required"] = True
        scenario["edges"][0]["cold_chain"] = False
    elif constraint == "deadline":
        scenario["deadline_minutes"] = 5.0
    elif constraint == "risk":
        scenario["max_risk"] = 0.09
    checked = evaluate_decision(scenario, cheap_route(scenario))
    assert checked["metrics"]["evidence_hash_valid"]
    assert not checked["constraints_satisfied"] and not checked["approved"]


def test_suboptimal_but_safe_route_has_measured_gap(scenario):
    decision = signed(scenario, {"path": ["A", "C", "D"], "edge_ids": ["AC", "CD"],
                                "totals": {"distance_km": 2.0, "cost": 10.0, "travel_minutes": 2.0, "risk": 0.2},
                                "objective_value": 0.5})
    checked = evaluate_decision(scenario, decision)
    assert checked["truthful"] and checked["grounded"] and checked["constraints_satisfied"]
    assert not checked["approved"] and not checked["optimal"]
    assert checked["metrics"]["objective_gap"] == pytest.approx(0.3)


def test_false_and_true_no_route_claims(scenario):
    false_claim = evaluate_decision(scenario, None)
    assert not false_claim["approved"] and not false_claim["truthful"]
    for edge in scenario["edges"]:
        edge["blocked"] = True
    true_claim = evaluate_decision(scenario, None)
    assert true_claim["approved"] and true_claim["status"] == "no_route"
    assert true_claim["metrics"]["feasible_route_exists"] is False


def test_unknown_oracle_budget_never_approves(scenario):
    for decision in (None, cheap_route(scenario)):
        checked = evaluate_decision(scenario, decision, max_expansions=0)
        assert not checked["approved"] and not checked["optimal"]
        assert checked["metrics"]["oracle_status"] == "unknown"
        assert checked["metrics"]["feasible_route_exists"] is None


def test_evidence_bound_to_scenario_and_order_independent(scenario):
    decision = cheap_route(scenario)
    scenario["nodes"].reverse()
    scenario["edges"].reverse()
    assert evaluate_decision(scenario, decision)["approved"]
    scenario["shipment_weight_kg"] = 51.0
    checked = evaluate_decision(scenario, decision)
    assert not checked["grounded"] and not checked["approved"]
    assert checked["truthful"]  # The totals still agree, but the evidence belongs to another input.


def test_zero_length_route_valid_only_when_endpoints_identical(scenario):
    scenario["destination"] = "A"
    decision = signed(scenario, {"path": ["A"], "edge_ids": [], "objective_value": 0.0,
                                "totals": {"distance_km": 0.0, "cost": 0.0, "travel_minutes": 0.0, "risk": 0.0}})
    assert evaluate_decision(scenario, decision)["approved"]


def test_nonfinite_claims_and_invalid_scenario_rejected(scenario):
    decision = cheap_route(scenario)
    decision["totals"]["risk"] = float("nan")
    assert not evaluate_decision(scenario, decision)["approved"]
    scenario["edges"][0]["cost"] = -1
    checked = evaluate_decision(scenario, None)
    assert not checked["approved"] and "schema" in checked["issues"][0]


def test_negative_near_zero_signed_claim_is_rejected(scenario):
    scenario["destination"] = "A"
    decision = signed(scenario, {"path": ["A"], "edge_ids": [], "objective_value": -1e-9,
                                "totals": {"distance_km": 0.0, "cost": -1e-9, "travel_minutes": 0.0, "risk": 0.0}})
    checked = evaluate_decision(scenario, decision)
    assert not checked["truthful"] and not checked["approved"]


def test_large_scale_signed_claim_cannot_hide_half_currency_unit(scenario):
    scenario["edges"] = [scenario["edges"][-1]]
    scenario["edges"][0]["cost"] = 1_000_000_000.0
    decision = signed(scenario, {"path": ["A", "D"], "edge_ids": ["AD"], "objective_value": 1.0,
                                "totals": {"distance_km": 4.0, "cost": 1_000_000_000.5,
                                           "travel_minutes": 1.0, "risk": 0.4}})
    checked = evaluate_decision(scenario, decision)
    assert checked["metrics"]["evidence_hash_valid"]
    assert not checked["truthful"] and not checked["approved"]


@pytest.mark.parametrize("resource,limit", [("risk", "max_risk"), ("travel_minutes", "deadline_minutes")])
def test_decimal_boundary_accepts_exact_sum_and_rejects_real_excess(scenario, resource, limit):
    for edge in scenario["edges"][2:]:
        edge["blocked"] = True
    scenario["edges"][0][resource] = 0.1
    scenario["edges"][1][resource] = 0.2
    scenario[limit] = 0.3
    decision = cheap_route(scenario)
    decision["totals"][resource] = 0.3
    checked = evaluate_decision(scenario, signed(scenario, decision))
    assert checked["approved"]
    scenario["edges"][1][resource] = 0.2000000001
    decision["totals"][resource] = 0.3000000001
    checked = evaluate_decision(scenario, signed(scenario, decision))
    assert not checked["constraints_satisfied"] and not checked["approved"]
    assert checked["metrics"]["feasible_route_exists"] is False


def test_decimal_hard_limit_preserves_smallest_float_contribution(scenario):
    scenario["edges"] = scenario["edges"][:2]
    scenario["edges"][0]["risk"] = 0.3
    scenario["edges"][1]["risk"] = 5e-324
    scenario["max_risk"] = 0.3
    decision = cheap_route(scenario)
    decision["totals"]["risk"] = 0.3  # Display rounding cannot authorize an actually excessive path.
    checked = evaluate_decision(scenario, signed(scenario, decision))
    assert not checked["constraints_satisfied"] and not checked["approved"]
    assert checked["metrics"]["feasible_route_exists"] is False


def test_generator_keeps_all_seeded_categories_without_filtering():
    cases = generate_scenarios(91237, 16)
    assert cases == generate_scenarios(91237, 16)
    assert cases != generate_scenarios(91238, 16)
    assert len(cases) == 16 and len({case["category"] for case in cases}) == 16
    assert all(3 <= len(case["scenario"]["nodes"]) <= 7 for case in cases)


def test_positive_weight_scaling_preserves_best_route(scenario):
    decision = cheap_route(scenario)
    scenario["weights"] = {key: value * 7 for key, value in scenario["weights"].items()}
    decision["objective_value"] *= 7
    checked = evaluate_decision(scenario, signed(scenario, decision))
    assert checked["approved"]
    assert checked["metrics"]["best_objective"] == pytest.approx(1.4)


def test_benchmark_records_raw_counts_and_resigned_adversarial_challenges():
    report = run(seed=91237, cases=16)
    assert len(report["results"]) == 16
    for algorithm in ("baseline", "optimizer"):
        assert report[algorithm]["cases"] == 16
        assert report[algorithm]["proposals"] + report[algorithm]["abstentions"] == 16
        assert report[algorithm]["schema_or_runtime_errors"] == 0
        assert report[algorithm]["oracle_unknown_cases"] == 0
    assert report["optimizer"]["unsafe_proposals"] == 0
    adversarial = report["adversarial_evidence"]
    assert adversarial["tested"] > 0 and adversarial["rejected"] == adversarial["tested"]
    assert report["property_controls"]["passed"] == report["property_controls"]["tested"]
    assert report["optimizer"]["proven_feasible_cases"] >= 9
    assert "not" in " ".join(report["limitations"]).lower()
    assert "Raw count" in markdown_report(report)
