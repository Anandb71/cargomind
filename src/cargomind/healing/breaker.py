"""Circuit breakers for tools and models.

closed --(threshold consecutive failures)--> open --(cooldown)--> half_open
half_open --success--> closed, half_open --failure--> open.
Transitions are journaled (``breaker_open`` / ``breaker_close``).
"""
from __future__ import annotations

import threading
import time
from typing import Any

from cargomind import config
from cargomind.healing import journal

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class CircuitBreaker:
    """A thread-safe consecutive-failure circuit breaker."""

    def __init__(
        self,
        name: str,
        threshold: int | None = None,
        cooldown: float | None = None,
    ) -> None:
        self.name = name
        self.threshold = max(1, int(threshold if threshold is not None else config.BREAKER_THRESHOLD))
        self.cooldown = float(cooldown if cooldown is not None else config.BREAKER_COOLDOWN_S)
        self._lock = threading.Lock()
        self._state = CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self.total_failures = 0
        self.total_successes = 0

    # -- state -----------------------------------------------------------------
    def _refresh(self) -> str:
        """Moves open -> half_open once the cooldown has elapsed (lock held)."""
        if self._state == OPEN and time.monotonic() - self._opened_at >= self.cooldown:
            self._state = HALF_OPEN
        return self._state

    @property
    def state(self) -> str:
        with self._lock:
            return self._refresh()

    @property
    def failures(self) -> int:
        return self._failures

    def retry_in(self) -> float:
        """Seconds until an open breaker lets a probe through (0 if not open)."""
        with self._lock:
            if self._refresh() != OPEN:
                return 0.0
            return max(0.0, self.cooldown - (time.monotonic() - self._opened_at))

    # -- protocol --------------------------------------------------------------
    def allow(self) -> bool:
        """Whether a call may proceed (closed, or a half-open probe)."""
        with self._lock:
            return self._refresh() != OPEN

    def success(self) -> None:
        """Records a successful call; closes a half-open/open breaker."""
        with self._lock:
            self.total_successes += 1
            previous = self._refresh()
            self._failures = 0
            self._state = CLOSED
        if previous != CLOSED:
            journal.record(
                "breaker_close",
                self.name,
                detail="probe call succeeded",
                action="circuit closed, traffic resumed",
                outcome="closed",
            )

    def failure(self) -> None:
        """Records a failed call; opens the breaker at the threshold."""
        opened = False
        with self._lock:
            self.total_failures += 1
            state = self._refresh()
            self._failures += 1
            if state == HALF_OPEN or (state == CLOSED and self._failures >= self.threshold):
                self._state = OPEN
                self._opened_at = time.monotonic()
                opened = True
            elif state == OPEN:
                self._opened_at = time.monotonic()
            failures = self._failures
        if opened:
            journal.record(
                "breaker_open",
                self.name,
                detail=f"{failures} consecutive failure(s)",
                action=f"skipping for {self.cooldown:g}s",
                outcome="open",
            )

    def reset(self) -> None:
        with self._lock:
            self._state = CLOSED
            self._failures = 0
            self._opened_at = 0.0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = self._refresh()
            retry_in = (
                max(0.0, self.cooldown - (time.monotonic() - self._opened_at))
                if state == OPEN
                else 0.0
            )
            return {
                "name": self.name,
                "state": state,
                "failures": self._failures,
                "threshold": self.threshold,
                "cooldown_s": self.cooldown,
                "retry_in_s": round(retry_in, 1),
                "total_failures": self.total_failures,
                "total_successes": self.total_successes,
            }

    def __repr__(self) -> str:  # pragma: no cover
        return f"CircuitBreaker({self.name!r}, state={self.state}, failures={self._failures})"


_registry: dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()


def get_breaker(
    name: str, threshold: int | None = None, cooldown: float | None = None
) -> CircuitBreaker:
    """Returns the process-wide breaker called `name`, creating it if needed."""
    with _registry_lock:
        breaker = _registry.get(name)
        if breaker is None:
            breaker = CircuitBreaker(name, threshold=threshold, cooldown=cooldown)
            _registry[name] = breaker
        return breaker


def peek_breaker(name: str) -> CircuitBreaker | None:
    """Returns the breaker called `name` if one exists, without creating it."""
    with _registry_lock:
        return _registry.get(name)


def snapshot() -> list[dict[str, Any]]:
    """All breakers' state for the dashboard, sorted by name."""
    with _registry_lock:
        breakers = list(_registry.values())
    return sorted((b.snapshot() for b in breakers), key=lambda s: s["name"])


def reset_all(forget: bool = False) -> None:
    """Closes every breaker (and drops them from the registry if `forget`)."""
    with _registry_lock:
        if forget:
            _registry.clear()
            return
        breakers = list(_registry.values())
    for b in breakers:
        b.reset()
