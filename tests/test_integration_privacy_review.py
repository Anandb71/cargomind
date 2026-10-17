"""Independent failure-path and legacy-storage review checks; no live providers."""
from __future__ import annotations

import importlib.util
import logging
import shutil

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from cargomind import config, runtime
from cargomind.web import server


def test_broken_chat_stream_does_not_expose_raw_exception_in_response_or_logs(monkeypatch, caplog):
    sentinel = "review-only@example.invalid"

    async def broken(*_args, **_kwargs):
        raise OSError(sentinel)
        yield {}  # pragma: no cover - keep the failure inside an async generator.

    monkeypatch.setattr(runtime, "stream_chat", broken)
    with caplog.at_level(logging.WARNING, logger="cargomind.web"):
        response = TestClient(server.app).post(
            "/api/chat", json={"session_id": "broken-stream-review", "message": "Track SHP-1003"},
        )
    assert response.status_code == 200
    assert '"type": "error"' in response.text
    assert sentinel not in response.text and sentinel not in caplog.text


def test_unavailable_subsystem_does_not_log_or_wrap_private_exception_contents(monkeypatch, caplog):
    sentinel = "private-import@example.invalid"
    original = server.importlib.import_module

    def unavailable(name):
        if name == "cargomind.review_missing_subsystem":
            raise OSError(sentinel)
        return original(name)

    monkeypatch.setattr(server.importlib, "import_module", unavailable)
    with caplog.at_level(logging.WARNING, logger="cargomind.web"), pytest.raises(HTTPException) as failure:
        server._lazy("cargomind.review_missing_subsystem")
    assert failure.value.status_code == 503
    assert sentinel not in str(failure.value.detail) and sentinel not in caplog.text


def test_default_config_uses_fresh_namespace_and_preserves_legacy_database(tmp_path, monkeypatch):
    for name in ("CARGOMIND_DATA_DIR", "CARGOMIND_DB"):
        monkeypatch.delenv(name, raising=False)
    package = tmp_path / "src" / "cargomind"
    package.mkdir(parents=True)
    local_config = package / "config.py"
    shutil.copyfile(config.__file__, local_config)
    legacy = tmp_path / "data" / "cargomind.db"
    legacy.parent.mkdir()
    legacy.write_bytes(b"untouched legacy database sentinel")
    spec = importlib.util.spec_from_file_location("review_isolated_config", local_config)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.DB_PATH == tmp_path / "data" / "operational-v1" / "cargomind.db"
    assert module.DATA_DIR.is_dir()
    assert legacy.read_bytes() == b"untouched legacy database sentinel"
