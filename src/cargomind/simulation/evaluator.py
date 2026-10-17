"""Independent, bounded verifier for synthetic route proposals.

The producer's validity flags and totals are never trusted.  The optimality
oracle enumerates simple paths independently of the routing engine.  Exhausting
its explicit budget produces an unverified result, never an approval.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Mapping
from decimal import Context, Decimal
from typing import Any

from cargomind.simulation.models import SimulationScenario

TOTAL_FIELDS = ("distance_km", "cost", "travel_minutes", "risk")
DECISION_FIELDS = {"path", "edge_ids", "totals", "objective_value", "evidence_hash"}
# A bounded float input can range from 5e-324 to 1e9. Keep enough decimal
# precision to preserve even its smallest positive contribution to a route sum.
_EXACT_SUM = Context(prec=400)


def _evidence_hash(scenario: dict, decision: dict) -> str:
    """Recompute the wire protocol independently rather than trusting its producer."""
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=True, allow_nan=False)
    data = dict(scenario)
    data["nodes"] = sorted(data["nodes"], key=lambda node: node["id"])
    data["edges"] = sorted(data["edges"], key=lambda edge: edge["id"])
    source_hash = hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()
    payload = {"protocol_version": "1.0", "scenario_hash": source_hash,
               "decision": {key: value for key, value in decision.items() if key != "evidence_hash"}}
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _close(left: float, right: float) -> bool:
    # Accommodate only floating-point arithmetic noise, not an amount that grows
    # to whole currency units when route costs are large.
    return abs(left - right) <= max(1e-10, 4 * math.ulp(right))


def _objective(scenario: dict, totals: dict[str, float]) -> float:
    edges = scenario["edges"]
    weights = scenario["weights"]
    return sum(
        weights[weight] * float(totals[field]) / max(1.0, max((edge[field] for edge in edges), default=0.0))
        for weight, field in (("time", "travel_minutes"), ("cost", "cost"), ("distance", "distance_km"))
    ) + weights["risk"] * float(totals["risk"])


def _edge_allowed(scenario: dict, edge: dict) -> bool:
    capacity = edge.get("capacity_kg")
    return (
        not edge["blocked"]
        and (capacity is None or Decimal(str(scenario["shipment_weight_kg"])) <= Decimal(str(capacity)))
        and (not scenario["cold_chain_required"] or edge["cold_chain"])
    )


def _totals_allowed(scenario: dict, totals: dict) -> bool:
    deadline = scenario.get("deadline_minutes")
    return ((deadline is None or totals["travel_minutes"] <= Decimal(str(deadline)))
            and totals["risk"] <= Decimal(str(scenario["max_risk"])))


def _oracle(scenario: dict, max_expansions: int, timeout_seconds: float) -> dict:
    """Depth-first enumeration of bounded simple paths; no producer dependency."""
    started = time.perf_counter()
    adjacency: dict[str, list[dict]] = {node["id"]: [] for node in scenario["nodes"]}
    for edge in scenario["edges"]:
        if _edge_allowed(scenario, edge):
            adjacency[edge["source"]].append(edge)
    zero = dict.fromkeys(TOTAL_FIELDS, Decimal(0))
    stack = [(scenario["origin"], frozenset([scenario["origin"]]), zero)]
    best: float | None = None
    expanded = 0
    complete = True
    while stack:
        if expanded >= max_expansions or time.perf_counter() - started >= timeout_seconds:
            complete = False
            break
        node, visited, totals = stack.pop()
        expanded += 1
        if node == scenario["destination"]:
            score = _objective(scenario, totals)
            best = score if best is None else min(best, score)
            continue
        for edge in adjacency[node]:
            if edge["target"] in visited:
                continue
            candidate = {field: _EXACT_SUM.add(totals[field], Decimal(str(edge[field]))) for field in TOTAL_FIELDS}
            if not _totals_allowed(scenario, candidate):
                continue
            # Nonnegative costs make this a sound bound on every continuation.
            if best is not None and _objective(scenario, candidate) > best + 1e-8:
                continue
            stack.append((edge["target"], visited | {edge["target"]}, candidate))
    return {"complete": complete, "best_objective": best, "expanded": expanded,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 4)}


def evaluate_decision(
    scenario: dict | SimulationScenario,
    decision: dict | None,
    *,
    max_expansions: int = 50_000,
    timeout_seconds: float = 1.0,
) -> dict:
    """Verify evidence, edge facts, constraints and bounded global optimality.

    ``None`` is a no-route claim and is accepted only after exhaustive proof of
    infeasibility.  Hashes establish scenario binding, not authorship or trust.
    """
    issues: list[str] = []
    metrics: dict[str, Any] = {}
    grounded = truthful = constraints = False
    optimal = False
    objective: float | None = None

    def result(status: str = "rejected") -> dict:
        approved = status in {"approved", "no_route"}
        return {"passed": approved, "approved": approved, "status": status,
                "grounded": grounded, "truthful": truthful, "constraints_satisfied": constraints,
                "optimal": optimal, "issues": issues, "errors": list(issues),
                "metrics": metrics, "objective_value": objective}

    try:
        model = scenario if isinstance(scenario, SimulationScenario) else SimulationScenario.model_validate(scenario)
        data = model.model_dump(mode="json")
    except (ValueError, TypeError) as exc:
        issues.append("Scenario failed schema validation: " + type(exc).__name__)
        return result()
    if not isinstance(max_expansions, int) or isinstance(max_expansions, bool) or max_expansions < 0:
        issues.append("Invalid oracle expansion budget")
        return result()
    if not _number(timeout_seconds) or timeout_seconds < 0:
        issues.append("Invalid oracle time budget")
        return result()
    oracle = _oracle(data, max_expansions, timeout_seconds)
    metrics.update({"oracle_complete": oracle["complete"], "oracle_status": "complete" if oracle["complete"] else "unknown",
                    "oracle_expanded": oracle["expanded"], "oracle_elapsed_ms": oracle["elapsed_ms"],
                    "best_objective": oracle["best_objective"], "objective_gap": None,
                    "feasible_route_exists": oracle["best_objective"] is not None if oracle["complete"] else None})
    if not oracle["complete"]:
        issues.append("Independent oracle budget exhausted; feasibility/optimality remains unknown")
    if decision is None:
        grounded = constraints = True  # No invented evidence or unsafe action is proposed.
        truthful = bool(oracle["complete"] and oracle["best_objective"] is None)
        optimal = truthful
        metrics["decision_feasible"] = False
        if not truthful and oracle["complete"]:
            issues.append("No-route claim is false: a feasible route exists")
        return result("no_route" if truthful else "rejected")
    if not isinstance(decision, Mapping) or set(decision) != DECISION_FIELDS:
        issues.append("Decision must contain only the protocol's exact evidence fields")
        return result()
    path, edge_ids, reported = decision["path"], decision["edge_ids"], decision["totals"]
    if (not isinstance(path, list) or not 1 <= len(path) <= 64 or not all(isinstance(node, str) for node in path)
            or not isinstance(edge_ids, list) or len(edge_ids) > 63 or not all(isinstance(edge, str) for edge in edge_ids)
            or len(path) != len(edge_ids) + 1):
        issues.append("Malformed path or edge sequence")
        return result()
    node_ids = {node["id"] for node in data["nodes"]}
    edges = {edge["id"]: edge for edge in data["edges"]}
    grounded = True
    if path[0] != data["origin"] or path[-1] != data["destination"]:
        grounded = False
        issues.append("Path endpoints do not match the scenario")
    if len(set(path)) != len(path):
        grounded = False
        issues.append("Path is not simple")
    if not set(path) <= node_ids:
        grounded = False
        issues.append("Decision invents a node")
    totals = dict.fromkeys(TOTAL_FIELDS, Decimal(0))
    constraints = True
    for index, edge_id in enumerate(edge_ids):
        edge = edges.get(edge_id)
        if edge is None:
            grounded = constraints = False
            issues.append("Decision invents an edge")
            continue
        if edge["source"] != path[index] or edge["target"] != path[index + 1]:
            grounded = constraints = False
            issues.append("Edge does not connect the claimed consecutive nodes")
        for field in TOTAL_FIELDS:
            totals[field] = _EXACT_SUM.add(totals[field], Decimal(str(edge[field])))
        if not _edge_allowed(data, edge):
            constraints = False
            issues.append("Edge violates closure, capacity, or cold-chain constraints")
    if not _totals_allowed(data, totals):
        constraints = False
        issues.append("Route exceeds the deadline or cumulative risk limit")
    constraints = constraints and grounded
    # Compare hard limits before converting to display floats; 0.1 + 0.2 is
    # exactly 0.3 in the decimal input contract, but a real excess is rejected.
    totals = {field: float(value) for field, value in totals.items()}
    objective = _objective(data, totals)
    truthful = (isinstance(reported, Mapping) and set(reported) == set(TOTAL_FIELDS)
                and all(_number(reported[field]) and reported[field] >= 0 and _close(reported[field], totals[field])
                        for field in TOTAL_FIELDS)
                and _number(decision["objective_value"]) and decision["objective_value"] >= 0
                and _close(decision["objective_value"], objective))
    if not truthful:
        issues.append("Reported totals or objective do not match the actual scenario edges")
    try:
        hash_valid = isinstance(decision["evidence_hash"], str) and decision["evidence_hash"] == _evidence_hash(data, dict(decision))
    except (ValueError, TypeError, OverflowError):
        hash_valid = False
    if not hash_valid:
        grounded = False
        issues.append("Evidence hash does not bind this decision to this scenario")
    metrics["evidence_hash_valid"] = hash_valid
    metrics["actual_totals"] = totals
    metrics["decision_feasible"] = constraints
    if constraints and oracle["best_objective"] is not None:
        metrics["objective_gap"] = objective - oracle["best_objective"]
        optimal = oracle["complete"] and _close(objective, oracle["best_objective"])
        if oracle["complete"] and not optimal:
            issues.append("A feasible route with a better objective exists")
    approved = grounded and truthful and constraints and optimal and oracle["complete"]
    return result("approved" if approved else "rejected")
