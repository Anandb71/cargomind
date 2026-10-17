"""Healing journal: every fault, retry, fallback and recovery CargoMind performs.

Events live in a thread-safe in-memory ring (for the live dashboard) and are
persisted to the ``healing_events`` table in ``config.DB_PATH`` (for history).
Subscribers get each new event pushed onto an ``asyncio.Queue`` from any thread.

Journaling must never break the caller: storage and fan-out failures are logged
and swallowed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
import uuid
from collections import Counter, deque
from typing import Any

from cargomind import config
from cargomind.safety.privacy import sanitize

logger = logging.getLogger(__name__)

KINDS = (
    "fault",
    "retry",
    "fallback",
    "breaker_open",
    "breaker_close",
    "arg_repair",
    "reflect",
    "recovered",
)
"""Known event kinds. Unknown kinds are accepted, but dashboards colour these."""

RING_SIZE = 500
_QUEUE_MAXSIZE = 1000

_lock = threading.RLock()
_ring: deque[dict[str, Any]] = deque(maxlen=RING_SIZE)
_counts: Counter[str] = Counter()
_failed = 0
_subscribers: list[tuple[asyncio.AbstractEventLoop | None, asyncio.Queue]] = []

_db_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_db_disabled = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS healing_events (
    id        TEXT PRIMARY KEY,
    ts        REAL NOT NULL,
    kind      TEXT NOT NULL,
    component TEXT NOT NULL,
    detail    TEXT,
    action    TEXT,
    outcome   TEXT
);
CREATE INDEX IF NOT EXISTS ix_healing_events_ts ON healing_events(ts);
"""


# --------------------------------------------------------------------------- db
def _get_conn() -> sqlite3.Connection | None:
    """Returns the journal's own sqlite connection, creating table on first use."""
    global _conn, _db_disabled
    if _conn is not None or _db_disabled:
        return _conn
    try:
        path = config.DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=5.0, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        conn.executescript(_SCHEMA)
        conn.commit()
        _conn = conn
    except Exception:  # noqa: BLE001 - journal failures must not expose exception contents
        logger.warning("healing journal: sqlite unavailable, memory-only mode")
        _db_disabled = True
    return _conn


def _persist(event: dict[str, Any]) -> None:
    with _db_lock:
        conn = _get_conn()
        if conn is None:
            return
        try:
            conn.execute(
                "INSERT OR REPLACE INTO healing_events"
                " (id, ts, kind, component, detail, action, outcome)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event["id"],
                    event["ts"],
                    event["kind"],
                    event["component"],
                    event["detail"],
                    event["action"],
                    event["outcome"],
                ),
            )
            conn.commit()
        except Exception:  # noqa: BLE001 - journal failures must not expose exception contents
            logger.warning("healing journal: failed to persist event")


# -------------------------------------------------------------------- fan-out
def _offer(queue: asyncio.Queue, event: dict[str, Any]) -> None:
    """Puts `event` on `queue`, dropping the oldest item when it is full."""
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass


def _fan_out(event: dict[str, Any]) -> None:
    with _lock:
        subs = list(_subscribers)
    dead: list[asyncio.Queue] = []
    for loop, queue in subs:
        if loop is None:
            _offer(queue, event)
            continue
        if loop.is_closed():
            dead.append(queue)
            continue
        try:
            loop.call_soon_threadsafe(_offer, queue, event)
        except RuntimeError:  # loop closed between the check and the call
            dead.append(queue)
    for queue in dead:
        unsubscribe(queue)


# ------------------------------------------------------------------ public API
def record(
    kind: str,
    component: str,
    detail: str = "",
    action: str = "",
    outcome: str = "",
) -> dict[str, Any]:
    """Records a healing event and pushes it to every subscriber.

    Args:
      kind: One of :data:`KINDS` (fault, retry, fallback, breaker_open,
        breaker_close, arg_repair, reflect, recovered).
      component: What was affected, e.g. ``tool:track_shipment`` or
        ``model:gemini-3.5-flash``.
      detail: Human-readable description (error text, repaired args, ...).
      action: What the healing layer did about it.
      outcome: Result of the action, e.g. ``recovered`` / ``failed`` / ``retrying``.

    Returns:
      The event dict ``{id, ts, kind, component, detail, action, outcome}``.
    """
    global _failed
    event = sanitize({
        "id": uuid.uuid4().hex[:12],
        "ts": round(time.time(), 3),
        "kind": str(kind),
        "component": str(component),
        "detail": str(detail or "")[:1000],
        "action": str(action or ""),
        "outcome": str(outcome or ""),
    })
    with _lock:
        _ring.append(event)
        _counts[event["kind"]] += 1
        if event["outcome"] == "failed":
            _failed += 1
    if logger.isEnabledFor(logging.INFO):
        # ASCII-only so cp1252 consoles (Windows) never choke on arrows.
        line = "healing {kind} {component}: {detail} | {action} {outcome}".format(
            **{**event, "detail": event["detail"][:120]}
        )
        logger.info(line.replace("→", "->").encode("ascii", "replace").decode("ascii"))
    _persist(event)
    try:
        _fan_out(event)
    except Exception:  # noqa: BLE001 - journal failures must not expose exception contents
        logger.warning("healing journal: fan-out failed")
    return event


def subscribe(maxsize: int = _QUEUE_MAXSIZE) -> asyncio.Queue:
    """Returns a queue that receives every event recorded from now on.

    Bound to the running event loop when there is one, so events recorded on
    other threads are delivered via ``loop.call_soon_threadsafe``. Without a
    running loop, events are put on the queue directly. Call
    :func:`unsubscribe` when done.
    """
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    with _lock:
        _subscribers.append((loop, queue))
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    """Stops delivering events to `queue`."""
    with _lock:
        _subscribers[:] = [(lp, q) for lp, q in _subscribers if q is not queue]


def recent(limit: int = 50) -> list[dict[str, Any]]:
    """Returns up to `limit` most recent events of this process, newest first."""
    with _lock:
        items = list(_ring)
    items.reverse()
    return sanitize([dict(e) for e in items[: max(0, int(limit))]])


def history(limit: int = 200) -> list[dict[str, Any]]:
    """Returns up to `limit` persisted events (all processes/runs), newest first."""
    with _db_lock:
        conn = _get_conn()
        if conn is None:
            return recent(limit)
        try:
            rows = conn.execute(
                "SELECT id, ts, kind, component, detail, action, outcome"
                " FROM healing_events ORDER BY ts DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        except Exception:  # noqa: BLE001 - journal failures must not expose exception contents
            logger.warning("healing journal: history query failed")
            return recent(limit)
    keys = ("id", "ts", "kind", "component", "detail", "action", "outcome")
    return sanitize([dict(zip(keys, row)) for row in rows])


def stats() -> dict[str, Any]:
    """Counts by kind plus a recovery rate, for this process.

    ``recovery_rate`` = recovered / (recovered + events whose outcome is
    ``failed``); ``None`` when nothing has gone wrong yet.
    """
    with _lock:
        by_kind = {k: _counts.get(k, 0) for k in KINDS}
        for k, v in _counts.items():
            by_kind.setdefault(k, v)
        total = sum(_counts.values())
        failed = _failed
    recovered = by_kind.get("recovered", 0)
    denom = recovered + failed
    return {
        "total": total,
        "by_kind": by_kind,
        "recovered": recovered,
        "failed": failed,
        "recovery_rate": round(recovered / denom, 3) if denom else None,
    }


def clear(persisted: bool = False) -> None:
    """Clears the in-memory ring and counters (and the sqlite table if asked)."""
    global _failed
    with _lock:
        _ring.clear()
        _counts.clear()
        _failed = 0
    if persisted:
        with _db_lock:
            conn = _get_conn()
            if conn is not None:
                conn.execute("DELETE FROM healing_events")
                conn.commit()


def to_json(event: dict[str, Any]) -> str:
    """Serialises an event for SSE/websocket streaming."""
    return json.dumps(sanitize(event), ensure_ascii=False)
