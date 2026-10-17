"""Tests for the operator feedback store."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault(
    "CARGOMIND_DB", str(Path(tempfile.mkdtemp(prefix="cargomind-test-")) / "test.db")
)

import pytest

from cargomind.feedback import store


@pytest.fixture(autouse=True)
def _clean():
    store.clear()
    yield
    store.clear()


def test_empty_store_renders_nothing():
    assert store.lessons() == []
    assert store.render_lessons_block() == ""
    assert store.stats() == {
        "total": 0,
        "positive": 0,
        "negative": 0,
        "satisfaction": None,
        "preferences": 0,
    }


def test_lessons_distilled_deduped_and_ordered():
    store.add_feedback("s1", -1, comment="Do not quote CR-03 for critical loads")
    store.add_feedback("s1", 1, comment="Great ETA breakdown")
    store.add_feedback("s2", -5, comment="do not quote CR-03 for critical loads")  # dup (case)
    store.add_feedback("s2", 1, comment="")  # rating only: counted, no lesson
    store.set_preference("units", "metric")

    assert store.lessons() == [
        "PREFERENCE: units=metric",
        "AVOID: do not quote CR-03 for critical loads",
        "KEEP: Great ETA breakdown",
    ]
    assert store.lessons(limit=1) == ["PREFERENCE: units=metric"]
    block = store.render_lessons_block()
    assert block.startswith("## Operator feedback (learned)\n")
    assert "- AVOID: do not quote CR-03 for critical loads" in block

    s = store.stats()
    assert (s["total"], s["positive"], s["negative"]) == (4, 2, 2)
    assert s["satisfaction"] == 0.5


def test_preferences_upsert():
    store.set_preference("mode", "rail")
    store.set_preference("mode", "road")
    assert store.get_preferences() == {"mode": "road"}
    with pytest.raises(ValueError):
        store.set_preference("  ", "x")


def test_remember_tool_stores_lasting_preference():
    res = store.remember_operator_feedback("Always prefer rail for >1000kg")
    assert res["status"] == "ok" and res["preference"]
    assert res["preference"].startswith(store.RULE_PREFIX)
    # Stored once as a preference; the identical AVOID line is deduplicated.
    assert store.lessons() == ["PREFERENCE: Always prefer rail for >1000kg"]

    res2 = store.remember_operator_feedback("Nice concise summary", rating=1)
    assert res2["preference"] is None and res2["lessons_active"] == 2
    assert store.lessons()[-1] == "KEEP: Nice concise summary"

    assert store.remember_operator_feedback("   ")["status"] == "error"


def test_remember_tool_has_adk_friendly_signature():
    from google.adk.tools.function_tool import FunctionTool

    decl = FunctionTool(store.remember_operator_feedback)._get_declaration()
    assert decl.name == "remember_operator_feedback"
    if decl.parameters is not None:
        props = decl.parameters.properties or {}
    else:  # JSON-schema declarations (newer ADK feature flag)
        props = (decl.parameters_json_schema or {}).get("properties", {})
    assert set(props) == {"feedback", "rating"}
