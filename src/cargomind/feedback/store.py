"""Operator feedback loop: ratings, corrections and lasting preferences.

Stored in sqlite (``feedback`` + ``preferences`` tables in ``config.DB_PATH``,
own connection) and distilled deterministically into short lessons that are
injected into every agent's instructions via :func:`render_lessons_block`.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from typing import Any

from cargomind import config

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    session_id TEXT,
    rating     INTEGER NOT NULL,
    comment    TEXT,
    message    TEXT,
    agent      TEXT
);
CREATE TABLE IF NOT EXISTS preferences (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

RULE_PREFIX = "operator_rule:"
_RULE_RE = re.compile(r"\b(always|never|prefer|preferred|prefers|avoid)\b", re.IGNORECASE)
_STOPWORDS = {"a", "an", "the", "for", "to", "of", "and", "or", "in", "on", "with", "please", "we", "i", "you"}

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
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
    return _conn


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _sign(rating: Any) -> int:
    try:
        r = int(rating)
    except (TypeError, ValueError):
        return 0
    return (r > 0) - (r < 0)


def _rule_key(text: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9>]+", text.lower()) if w not in _STOPWORDS]
    slug = "_".join(words)[:48].strip("_") or "note"
    return RULE_PREFIX + slug


# ------------------------------------------------------------------- feedback
def add_feedback(
    session_id: str,
    rating: int,
    comment: str = "",
    message: str = "",
    agent: str = "",
) -> dict[str, Any]:
    """Stores a thumbs-up (+1) / thumbs-down (-1) with an optional comment.

    Args:
      session_id: Chat session the feedback belongs to.
      rating: +1 or -1 (any positive/negative int is clamped; 0 = neutral note).
      comment: Operator's explanation; becomes a KEEP/AVOID lesson.
      message: The assistant message being rated.
      agent: Name of the agent that produced it.

    Returns:
      The stored row as a dict.
    """
    row = {
        "ts": round(time.time(), 3),
        "session_id": str(session_id or ""),
        "rating": _sign(rating),
        "comment": _norm(comment)[:500],
        "message": str(message or "")[:4000],
        "agent": str(agent or ""),
    }
    with _lock:
        conn = _db()
        cur = conn.execute(
            "INSERT INTO feedback (ts, session_id, rating, comment, message, agent)"
            " VALUES (:ts, :session_id, :rating, :comment, :message, :agent)",
            row,
        )
        conn.commit()
        row["id"] = cur.lastrowid
    return row


def recent_feedback(limit: int = 50) -> list[dict[str, Any]]:
    """Most recent feedback rows, newest first."""
    with _lock:
        rows = _db().execute(
            "SELECT id, ts, session_id, rating, comment, message, agent"
            " FROM feedback ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    keys = ("id", "ts", "session_id", "rating", "comment", "message", "agent")
    return [dict(zip(keys, r)) for r in rows]


# ---------------------------------------------------------------- preferences
def set_preference(key: str, value: str) -> None:
    """Upserts a lasting operator preference."""
    key = _norm(key)
    if not key:
        raise ValueError("preference key must not be empty")
    with _lock:
        conn = _db()
        conn.execute(
            "INSERT INTO preferences (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (key, _norm(value)[:500], time.time()),
        )
        conn.commit()


def get_preferences() -> dict[str, str]:
    """All preferences, most recently updated first."""
    with _lock:
        rows = _db().execute(
            "SELECT key, value FROM preferences ORDER BY updated_at DESC, key"
        ).fetchall()
    return {k: v for k, v in rows}


def delete_preference(key: str) -> bool:
    with _lock:
        conn = _db()
        cur = conn.execute("DELETE FROM preferences WHERE key = ?", (key,))
        conn.commit()
        return cur.rowcount > 0


# -------------------------------------------------------------------- lessons
def lessons(limit: int = 8) -> list[str]:
    """Distils feedback into at most `limit` deduplicated one-line lessons.

    Order: preferences (lasting), then recent negative comments ("AVOID: ..."),
    then recent positive comments ("KEEP: ...").
    """
    limit = max(0, int(limit))
    out: list[str] = []
    seen: set[str] = set()

    def add(line: str, key: str) -> None:
        k = key.lower()
        if k and k not in seen and len(out) < limit:
            seen.add(k)
            out.append(line)

    for key, value in get_preferences().items():
        if key.startswith(RULE_PREFIX):
            add(f"PREFERENCE: {value}", value)
        else:
            add(f"PREFERENCE: {key}={value}", f"{key}={value}")

    with _lock:
        rows = _db().execute(
            "SELECT rating, comment FROM feedback"
            " WHERE comment IS NOT NULL AND comment != '' AND rating != 0"
            " ORDER BY id DESC LIMIT 200"
        ).fetchall()
    for want, label in ((-1, "AVOID"), (1, "KEEP")):
        for rating, comment in rows:
            if rating == want:
                add(f"{label}: {comment}", comment)
    return out


def render_lessons_block(limit: int = 8) -> str:
    """Markdown block for agent instructions, or "" when nothing was learned."""
    try:
        items = lessons(limit)
    except Exception:
        logger.warning("feedback: could not load lessons", exc_info=True)
        return ""
    if not items:
        return ""
    body = "\n".join(f"- {line}" for line in items)
    return (
        "## Operator feedback (learned)\n"
        "Apply these lessons from the operator to every answer:\n"
        f"{body}\n"
    )


def stats() -> dict[str, Any]:
    """``{total, positive, negative, satisfaction}``; satisfaction is 0..1 or None."""
    with _lock:
        total, pos, neg = _db().execute(
            "SELECT COUNT(*),"
            " COALESCE(SUM(CASE WHEN rating > 0 THEN 1 ELSE 0 END), 0),"
            " COALESCE(SUM(CASE WHEN rating < 0 THEN 1 ELSE 0 END), 0)"
            " FROM feedback"
        ).fetchone()
        n_prefs = _db().execute("SELECT COUNT(*) FROM preferences").fetchone()[0]
    rated = pos + neg
    return {
        "total": total,
        "positive": pos,
        "negative": neg,
        "satisfaction": round(pos / rated, 3) if rated else None,
        "preferences": n_prefs,
    }


def clear() -> None:
    """Deletes all feedback and preferences (tests / demo reset)."""
    with _lock:
        conn = _db()
        conn.execute("DELETE FROM feedback")
        conn.execute("DELETE FROM preferences")
        conn.commit()


# ----------------------------------------------------------------- ADK tool
def remember_operator_feedback(feedback: str, rating: int = -1) -> dict:
    """Stores operator feedback so it is applied to all future turns.

    Call this whenever the operator corrects you ("that ETA is wrong, Pune hub
    is closed on Sundays") or states a lasting preference ("always prefer rail
    for >1000kg", "never use CR-03 for critical shipments"). The lesson is
    persisted and injected into every agent's instructions from now on.

    Args:
      feedback: The operator's correction or preference, in their own words.
      rating: -1 for a correction/complaint (default), +1 for praise of
        something that should be kept.

    Returns:
      dict with status, whether it was stored as a lasting preference, and the
      number of active lessons.
    """
    text = _norm(feedback)
    if not text:
        return {"status": "error", "error": "feedback text is empty", "suggestions": ["Pass the operator's words in `feedback`."]}
    try:
        add_feedback("operator-tool", rating, comment=text, agent="remember_operator_feedback")
        pref_key = None
        if _RULE_RE.search(text):
            pref_key = _rule_key(text)
            set_preference(pref_key, text)
        active = len(lessons())
    except sqlite3.Error as exc:
        return {"status": "error", "error": f"could not store feedback: {exc}"}
    return {
        "status": "ok",
        "stored": True,
        "preference": pref_key,
        "lessons_active": active,
        "message": (
            "Saved as a lasting preference; it will be applied to all future turns."
            if pref_key
            else "Feedback saved; it will inform future answers."
        ),
    }
