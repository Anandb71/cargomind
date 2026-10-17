"""ADK tool adapter; scenario values must come from the operator, never invented."""
from cargomind.safety.privacy import inspect_input
from cargomind.simulation.engine import run_simulation, sample_scenario
from cargomind.simulation.parsing import InvalidPayload, strict_object


def optimize_simulation(scenario_json: str = "") -> dict:
    """Optimize an operator-provided JSON logistics graph and independently verify it.

    Args:
        scenario_json: Complete scenario matching /api/simulation/schema. Never infer missing
            nodes, edges, distances, constraints or weights. Empty input requests the schema.

    Returns:
        Verified proposal with independent evaluation and correlated agent handoffs, or
        a rejected/needs_input response. This never dispatches vehicles or books carriers.
    """
    if not scenario_json:
        return {"status": "needs_input", "message": "Supply the graph JSON at /simulation or POST /api/simulation/run.",
                "schema_url": "/api/simulation/schema", "example_is_synthetic": True,
                "example": sample_scenario()}
    try:
        payload = strict_object(scenario_json)
    except InvalidPayload:
        return {"status": "rejected", "message": "Invalid scenario JSON."}
    if not inspect_input(scenario_json)["allowed"]:
        return {"status": "rejected", "message": "Guardrail: operational-only JSON required."}
    return run_simulation(payload)
