"""CargoMind evaluation harness.

`cargomind.evals.scenarios` holds the golden scenarios (one per specialist / agent
pattern) and `cargomind.evals.runner` replays them through the real runtime and
scores routing, answers, latency and self-healing:

    uv run python -m cargomind.evals.runner --mode offline --out docs/EVAL_REPORT.md
    uv run python -m cargomind.evals.runner --chaos 0.3 --out docs/EVAL_REPORT_CHAOS.md

The ADK-native eval set lives in ``evals/cargomind.evalset.json`` (``adk eval``).
"""
from __future__ import annotations

__all__ = ["run_eval"]


def __getattr__(name):  # lazy: importing the package must not build the agent graph
    if name == "run_eval":
        from cargomind.evals.runner import run_eval

        return run_eval
    raise AttributeError(name)
