"""Bounded, non-PII scenario schemas and reproducible evidence fingerprints."""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL_VERSION = "1.0"
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")]
Nonnegative = Annotated[float, Field(ge=0, le=1_000_000_000)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_default=True, strict=True)


class Node(StrictModel):
    id: Identifier


class Edge(StrictModel):
    id: Identifier
    source: Identifier
    target: Identifier
    distance_km: Nonnegative
    cost: Nonnegative
    travel_minutes: Nonnegative
    risk: float = Field(ge=0, le=1)
    blocked: bool = False
    capacity_kg: Nonnegative | None = None
    cold_chain: bool = False


class Weights(StrictModel):
    time: Nonnegative = 1
    cost: Nonnegative = 1
    risk: Nonnegative = 1
    distance: Nonnegative = 1

    @model_validator(mode="after")
    def positive_weight(self):
        if not any((self.time, self.cost, self.risk, self.distance)):
            raise ValueError("At least one objective weight must be positive")
        return self


class SimulationScenario(StrictModel):
    nodes: list[Node] = Field(min_length=1, max_length=64)
    edges: list[Edge] = Field(default_factory=list, max_length=512)
    origin: Identifier
    destination: Identifier
    shipment_weight_kg: Nonnegative
    cold_chain_required: bool = False
    deadline_minutes: Nonnegative | None = None
    max_risk: float = Field(default=1, ge=0, le=1)
    weights: Weights = Field(default_factory=Weights)

    @model_validator(mode="after")
    def valid_graph(self):
        ids = [node.id for node in self.nodes]
        if len(set(ids)) != len(ids):
            raise ValueError("Node IDs must be unique")
        edge_ids = [edge.id for edge in self.edges]
        if len(set(edge_ids)) != len(edge_ids):
            raise ValueError("Edge IDs must be unique")
        known = set(ids)
        if self.origin not in known or self.destination not in known:
            raise ValueError("Origin and destination must name existing nodes")
        if any(edge.source not in known or edge.target not in known for edge in self.edges):
            raise ValueError("Every edge endpoint must name an existing node")
        return self


class RouteTotals(StrictModel):
    distance_km: float = Field(ge=0, le=64_000_000_000)
    cost: float = Field(ge=0, le=64_000_000_000)
    travel_minutes: float = Field(ge=0, le=64_000_000_000)
    # Additive risk exposure, not a probability. A feasible scenario caps its sum at max_risk.
    risk: float = Field(ge=0, le=64)


class SimulationDecision(StrictModel):
    path: list[Identifier] = Field(min_length=1, max_length=64)
    edge_ids: list[Identifier] = Field(max_length=63)
    totals: RouteTotals
    objective_value: float = Field(ge=0)
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def canonical_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def canonical_scenario(scenario: SimulationScenario | dict) -> dict:
    scenario = SimulationScenario.model_validate(scenario)
    value = scenario.model_dump(mode="json")
    value["nodes"].sort(key=lambda node: node["id"])
    value["edges"].sort(key=lambda edge: edge["id"])
    return value


def scenario_hash(scenario: SimulationScenario | dict) -> str:
    return hashlib.sha256(canonical_json(canonical_scenario(scenario)).encode("utf-8")).hexdigest()


def decision_hash(scenario: SimulationScenario | dict, decision: SimulationDecision | dict) -> str:
    if isinstance(decision, BaseModel):
        decision = decision.model_dump(mode="json")
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "scenario_hash": scenario_hash(scenario),
        "decision": {key: value for key, value in decision.items() if key != "evidence_hash"},
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
