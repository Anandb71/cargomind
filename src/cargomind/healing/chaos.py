"""Chaos engineering: opt-in transient fault injection.

Every tool that does I/O calls ``maybe_fail("tool:<name>")`` first and
``ResilientLlm`` calls ``maybe_fail("model:<id>")`` before each provider call.
With ``CARGOMIND_CHAOS=0.3`` roughly 30% of those calls raise a
:class:`ChaosError`, which the self-healing layer must absorb. Recovery paths
run inside :func:`no_chaos` so a retry is never sabotaged by chaos itself.
"""
from __future__ import annotations

import asyncio
import contextvars
import random
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from cargomind import config
from cargomind.healing import journal

__all__ = [
    "ChaosError",
    "TransientError",
    "force_fail",
    "get_rate",
    "is_transient",
    "maybe_fail",
    "no_chaos",
    "retrying",
    "seed",
    "set_rate",
]


class TransientError(Exception):
    """A failure that is expected to succeed if retried (network, quota, ...)."""


class ChaosError(TransientError):
    """A deliberately injected transient failure."""


def _clamp(rate: float) -> float:
    return max(0.0, min(1.0, float(rate)))


_rate: float = _clamp(config.CHAOS_RATE)
_rng = random.Random()
_forced: dict[str, int] = {}
_forced_lock = threading.Lock()

retrying: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "cargomind_chaos_retrying", default=False
)
"""True while a recovery path runs; :func:`maybe_fail` never injects then."""


def set_rate(rate: float) -> float:
    """Sets the global fault-injection probability (clamped to 0..1)."""
    global _rate
    _rate = _clamp(rate)
    return _rate


def get_rate() -> float:
    """Returns the global fault-injection probability."""
    return _rate


def seed(value: int | None) -> None:
    """Seeds the chaos RNG (for reproducible demos and tests)."""
    _rng.seed(value)


def force_fail(component_prefix: str, times: int = 1) -> None:
    """Arms `times` deterministic faults for components starting with the prefix.

    Works even when the rate is 0, e.g. ``force_fail("model:gemini", 3)`` to
    demo a model fallback on stage. ``times=0`` disarms.
    """
    with _forced_lock:
        if times > 0:
            _forced[component_prefix] = int(times)
        else:
            _forced.pop(component_prefix, None)


def _take_forced(component: str) -> bool:
    with _forced_lock:
        for prefix, left in list(_forced.items()):
            if component.startswith(prefix) and left > 0:
                if left == 1:
                    del _forced[prefix]
                else:
                    _forced[prefix] = left - 1
                return True
    return False


@contextmanager
def no_chaos() -> Iterator[None]:
    """Suppresses fault injection in this context (used by retries)."""
    token = retrying.set(True)
    try:
        yield
    finally:
        retrying.reset(token)


def maybe_fail(component: str) -> None:
    """Raises :class:`ChaosError` with probability :func:`get_rate`.

    Args:
      component: What is being sabotaged, e.g. ``tool:get_route``.

    Raises:
      ChaosError: When a fault is injected (and journaled as ``kind="fault"``).
    """
    if retrying.get():
        return
    forced = _take_forced(component)
    rate = _rate
    if not forced and (rate <= 0.0 or _rng.random() >= rate):
        return
    reason = "forced" if forced else f"rate={rate:.0%}"
    journal.record(
        "fault",
        component,
        detail=f"chaos: simulated transient failure ({reason})",
        action="injected",
        outcome="pending",
    )
    raise ChaosError(f"[chaos] simulated transient failure in {component}")


# ----------------------------------------------------------- classification
_TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
_NON_TRANSIENT_OS = (
    FileNotFoundError,
    FileExistsError,
    PermissionError,
    IsADirectoryError,
    NotADirectoryError,
)
_TRANSIENT_NAME_HINTS = (
    "timeout",
    "ratelimit",
    "serviceunavailable",
    "overloaded",
    "connection",
    "internalserver",
    "badgateway",
    "temporarily",
)


def _status_code(error: BaseException) -> int | None:
    code = getattr(error, "code", None)  # google-genai APIError
    if isinstance(code, int) and 100 <= code < 600:
        return code
    code = getattr(error, "status_code", None)  # litellm / openai / anthropic
    if isinstance(code, int):
        return code
    code = getattr(getattr(error, "response", None), "status_code", None)  # httpx
    return code if isinstance(code, int) else None


def is_transient(error: BaseException) -> bool:
    """Whether `error` looks like a failure worth retrying as-is."""
    if isinstance(error, TransientError):
        return True
    if isinstance(error, (TimeoutError, asyncio.TimeoutError, ConnectionError)):
        return True
    try:  # httpx: every transport-level failure (timeouts, network, protocol)
        import httpx

        if isinstance(error, httpx.TransportError):
            return True
    except ImportError:  # pragma: no cover
        pass
    status = _status_code(error)
    if status is not None:
        return status in _TRANSIENT_STATUS
    if isinstance(error, OSError) and not isinstance(error, _NON_TRANSIENT_OS):
        return True
    name = type(error).__name__.lower()
    return any(hint in name for hint in _TRANSIENT_NAME_HINTS)
