"""Exact bounded routing and an explicit deterministic multi-agent exchange.

Agents implement constraint validation, optimization, independent evaluation and
an output gate. They are deterministic software roles, not simulated LLM calls.
"""
from __future__ import annotations

import heapq
import time
import uuid
from decimal import Context, Decimal
from typing import Any

from pydantic import ValidationError

from cargomind.simulation.models import (
    PROTOCOL_VERSION,
    Edge,
    SimulationDecision,
    SimulationScenario,
    canonical_scenario,
    decision_hash,
    scenario_hash,
)
from cargomind.simulation.protocol import AgentReply, Handoff, ProtocolError, ProtocolSession


def _factors(scenario: SimulationScenario) -> dict:
    return {"time": max(1.0, max((edge.travel_minutes for edge in scenario.edges), default=0)),
            "cost": max(1.0, max((edge.cost for edge in scenario.edges), default=0)),
            "distance": max(1.0, max((edge.distance_km for edge in scenario.edges), default=0))}


def _objective(scenario: SimulationScenario, totals: dict, factors: dict) -> float:
    weights = scenario.weights
    return (weights.time * float(totals["travel_minutes"]) / factors["time"]
            + weights.cost * float(totals["cost"]) / factors["cost"]
            + weights.distance * float(totals["distance_km"]) / factors["distance"]
            + weights.risk * float(totals["risk"]))


def _extend(scenario: SimulationScenario, totals: dict, edge: Edge, decimal_context: Context) -> dict | None:
    if edge.blocked or (scenario.cold_chain_required and not edge.cold_chain):
        return None
    if edge.capacity_kg is not None and scenario.shipment_weight_kg > edge.capacity_kg:
        return None
    # Decimal arithmetic over the validated numbers' decimal spellings keeps exact
    # budget equality (0.1 + 0.2 == 0.3) without permitting any genuine overage.
    updated = {key: decimal_context.add(totals[key], Decimal(str(getattr(edge, key)))) for key in totals}
    if updated["risk"] > Decimal(str(scenario.max_risk)):
        return None
    if (scenario.deadline_minutes is not None
            and updated["travel_minutes"] > Decimal(str(scenario.deadline_minutes))):
        return None
    return updated


def _decision(scenario: SimulationScenario, path: tuple, edges: tuple, totals: dict, factors: dict) -> dict:
    value = {"path": list(path), "edge_ids": list(edges),
             "totals": {key: float(value) for key, value in totals.items()},
             "objective_value": _objective(scenario, totals, factors)}
    value["evidence_hash"] = decision_hash(scenario, value)
    return SimulationDecision.model_validate(value).model_dump(mode="json")


def solve_scenario(
    payload: SimulationScenario | dict, *, max_expansions: int = 50_000, timeout_seconds: float = 1.0,
) -> dict:
    """Uniform-cost enumeration of feasible simple directed paths; bounded exact search.

    Nonnegative additive edge objectives make the first popped destination optimal.
    Exhaustion proves infeasibility; exceeding either budget proves nothing and
    therefore cannot result in an approved decision.
    """
    scenario = SimulationScenario.model_validate(payload)
    factors = _factors(scenario)
    # The complete accepted float range needs 334 decimal places when added to 1e9.
    # Keep a per-run context; never change the caller's/global Decimal precision.
    decimal_context = Context(prec=400)
    outgoing: dict[str, list[Edge]] = {node.id: [] for node in scenario.nodes}
    for edge in sorted(scenario.edges, key=lambda item: item.id):
        outgoing[edge.source].append(edge)
    zero = dict.fromkeys(("distance_km", "cost", "travel_minutes", "risk"), Decimal(0))
    frontier = [(0.0, (), (scenario.origin,), zero)]
    expanded = 0
    started = time.monotonic()
    while frontier:
        if expanded >= max_expansions or time.monotonic() - started >= timeout_seconds:
            return {"complete": False, "decision": None, "reason": "search_budget_exceeded", "expanded": expanded}
        _, edge_ids, path, totals = heapq.heappop(frontier)
        expanded += 1
        if path[-1] == scenario.destination:
            return {"complete": True, "decision": _decision(scenario, path, edge_ids, totals, factors),
                    "reason": "optimal", "expanded": expanded}
        for edge in outgoing[path[-1]]:
            if edge.target in path:
                continue
            updated = _extend(scenario, totals, edge, decimal_context)
            if updated is not None:
                heapq.heappush(frontier, (_objective(scenario, updated, factors), edge_ids + (edge.id,),
                                         path + (edge.target,), updated))
                if len(frontier) > max_expansions:
                    return {"complete": False, "decision": None, "reason": "search_budget_exceeded", "expanded": expanded}
    return {"complete": True, "decision": None, "reason": "no_feasible_route", "expanded": expanded}


def greedy_baseline(payload: SimulationScenario | dict) -> dict:
    """Locally cheapest next feasible edge, without backtracking or an optimality claim."""
    scenario = SimulationScenario.model_validate(payload)
    factors = _factors(scenario)
    decimal_context = Context(prec=400)
    path, edges = (scenario.origin,), ()
    totals = dict.fromkeys(("distance_km", "cost", "travel_minutes", "risk"), Decimal(0))
    while path[-1] != scenario.destination:
        candidates = []
        for edge in scenario.edges:
            if edge.source == path[-1] and edge.target not in path:
                updated = _extend(scenario, totals, edge, decimal_context)
                if updated is not None:
                    delta = {key: getattr(edge, key) for key in totals}
                    candidates.append((_objective(scenario, delta, factors), edge.id, edge, updated))
        if not candidates:
            return {"complete": True, "decision": None, "reason": "greedy_dead_end", "expanded": len(path)}
        _, _, edge, totals = min(candidates, key=lambda candidate: candidate[:2])
        path, edges = path + (edge.target,), edges + (edge.id,)
    return {"complete": True, "decision": _decision(scenario, path, edges, totals, factors),
            "reason": "greedy_feasible", "expanded": len(path)}


def _rejected_evaluation(issue: str) -> dict:
    return {"approved": False, "status": "rejected", "optimal": False, "errors": [issue], "objective_value": None}


def run_simulation(payload: dict) -> dict:
    """Run validated JSON handoffs and release only independently verified decisions."""
    session: ProtocolSession | None = None
    try:
        scenario = SimulationScenario.model_validate(payload)
        fingerprint = scenario_hash(scenario)
        # Task correlation is unique per exchange; scenario and decision hashes remain reproducible.
        session = ProtocolSession("sim-" + uuid.uuid4().hex[:24])

        def constraints(message: Handoff) -> AgentReply:
            checked = SimulationScenario.model_validate(message.payload["scenario"])
            if scenario_hash(checked) != fingerprint:
                raise ProtocolError("Scenario evidence changed at the constraint agent")
            return AgentReply(recipient="optimizer_agent", status="validated", payload={
                "scenario": canonical_scenario(checked), "scenario_hash": fingerprint,
            })

        def optimizer(message: Handoff) -> AgentReply:
            checked = SimulationScenario.model_validate(message.payload["scenario"])
            return AgentReply(recipient="evaluator_agent", status="solved", payload={
                **message.payload, "solver": solve_scenario(checked), "baseline": greedy_baseline(checked),
            })

        def evaluator(message: Handoff) -> AgentReply:
            solver = message.payload["solver"]
            if not solver["complete"]:
                evaluation = _rejected_evaluation("Optimizer budget exhausted; optimality is unverified")
            else:
                try:
                    # Kept local so the optimizer can be imported without the independently owned oracle.
                    from cargomind.simulation.evaluator import evaluate_decision

                    evaluation = evaluate_decision(message.payload["scenario"], solver["decision"])
                    if (not isinstance(evaluation, dict)
                            or not isinstance(evaluation.get("approved"), bool)
                            or not isinstance(evaluation.get("optimal"), bool)
                            or evaluation.get("status") not in ("approved", "no_route", "rejected")):
                        raise ValueError("Independent evaluator returned a malformed verdict")
                except Exception:  # noqa: BLE001 - verifier failures must never release a proposal.
                    evaluation = _rejected_evaluation("Independent evaluator unavailable or failed")
            return AgentReply(recipient="output_gate", status="evaluated",
                              payload={**message.payload, "evaluation": evaluation})

        def gate(message: Handoff) -> AgentReply:
            data = message.payload
            solver, evaluation = data["solver"], data["evaluation"]
            decision = solver["decision"]
            release = (solver["complete"] and evaluation.get("approved") is True
                       and evaluation.get("optimal") is True
                       and all(evaluation.get(flag) is True
                               for flag in ("passed", "grounded", "truthful", "constraints_satisfied")))
            expected = "approved" if decision is not None else "no_route"
            release = release and evaluation.get("status") == expected
            if decision is not None:
                release = release and decision.get("evidence_hash") == decision_hash(data["scenario"], decision)
            status = expected if release else "rejected"
            result = {"status": status, "protocol_version": PROTOCOL_VERSION,
                      "scenario_hash": fingerprint, "decision": decision if release else None,
                      "evaluation": evaluation, "baseline": data["baseline"],
                      "search": {key: value for key, value in solver.items() if key != "decision"},
                      "agent_kind": "deterministic", "protocol_kind": "custom_json"}
            return AgentReply(recipient="operator", status=status, payload=result)

        for role, handler in (("constraint_agent", constraints), ("optimizer_agent", optimizer),
                              ("evaluator_agent", evaluator), ("output_gate", gate)):
            session.register(role, handler)
        result = session.run({"scenario": canonical_scenario(scenario)})
        result["handoffs"] = session.handoffs
        return result
    except ValidationError as exc:
        issues = [{"field": ".".join(map(str, error["loc"])), "message": error["msg"]}
                  for error in exc.errors(include_input=False, include_url=False, include_context=False)[:12]]
        evaluation = _rejected_evaluation("Scenario validation failed")
        evaluation["validation_issues"] = issues
    except (ValueError, TypeError, KeyError, ProtocolError):
        evaluation = _rejected_evaluation("Scenario or handoff rejected by the protocol boundary")
    return {"status": "rejected", "protocol_version": PROTOCOL_VERSION, "scenario_hash": None,
            "decision": None, "evaluation": evaluation, "baseline": None,
            "handoffs": session.handoffs if session else [],
            "agent_kind": "deterministic", "protocol_kind": "custom_json"}


def sample_scenario() -> dict[str, Any]:
    """Small counterexample to greedy routing, with a blocked fastest direct lane."""
    def edge(identifier, source, target, distance, cost, minutes, risk, blocked=False):
        return {"id": identifier, "source": source, "target": target, "distance_km": distance,
                "cost": cost, "travel_minutes": minutes, "risk": risk, "blocked": blocked,
                "capacity_kg": 800, "cold_chain": True}
    return {
        "nodes": [{"id": name} for name in ("Origin", "GreedyHub", "FastHub", "EconomyHub", "Destination")],
        "edges": [
            edge("BlockedFast", "Origin", "Destination", 10, 50, 10, 0.01, True),
            edge("GreedyFirst", "Origin", "GreedyHub", 5, 1, 5, 0.02),
            edge("GreedyLast", "GreedyHub", "Destination", 30, 30, 80, 0.15),
            edge("FastFirst", "Origin", "FastHub", 10, 8, 20, 0.03),
            edge("FastLast", "FastHub", "Destination", 10, 8, 20, 0.03),
            edge("EconomyFirst", "Origin", "EconomyHub", 15, 2, 35, 0.01),
            edge("EconomyLast", "EconomyHub", "Destination", 15, 2, 35, 0.01),
        ],
        "origin": "Origin", "destination": "Destination", "shipment_weight_kg": 500,
        "cold_chain_required": True, "deadline_minutes": 120, "max_risk": 1,
        "weights": {"time": 1, "cost": 1, "risk": 1, "distance": 1},
    }


def run_simulation_example() -> dict:
    """Run the public synthetic logistics example through all four deterministic agents.

    Uses no customer records, API keys or external services. The result is released
    only after independent constraint, evidence and optimality verification.
    """
    return run_simulation(sample_scenario())
