"""Deterministic privacy and action boundaries for the CargoMind simulation.

These checks cover structured personal fields and explicit unsafe requests; they
are not a universal name detector or a substitute for production access control.
"""
from cargomind.safety.privacy import inspect_input, inspect_output, sanitize

__all__ = ["inspect_input", "inspect_output", "sanitize"]
