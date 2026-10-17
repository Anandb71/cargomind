"""Deterministic logistics agents using a custom JSON handoff protocol.

This protocol is application-specific; it does not claim official A2A compliance.
"""
from cargomind.simulation.engine import (
    greedy_baseline,
    run_simulation,
    run_simulation_example,
    sample_scenario,
    solve_scenario,
)

__all__ = ["greedy_baseline", "run_simulation", "run_simulation_example", "sample_scenario", "solve_scenario"]
