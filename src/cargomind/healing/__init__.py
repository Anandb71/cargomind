"""Self-healing layer: journal, chaos, circuit breakers, resilient models, plugin.

Deliberately import-free (no ADK, no submodules) to avoid circular imports;
import submodules directly, e.g. ``from cargomind.healing.chaos import maybe_fail``.
"""
