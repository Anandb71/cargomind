"""CargoMind mission-control web server.

FastAPI app that serves the no-build dashboard (``static/``) and a small JSON/SSE
API over the agent runtime, the logistics database, the self-healing journal and
the feedback store.

Every import of a sibling subsystem (``cargomind.runtime``, ``cargomind.data``,
``cargomind.healing``, ``cargomind.feedback``) is done lazily inside the handler so
the dashboard boots even when a subsystem is missing or broken; the affected
endpoint answers 503 (or, for chat, a single SSE ``error`` event) instead.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import dataclasses
import datetime as _dt
import enum
import importlib
import importlib.metadata
import importlib.util
import json
import logging
import os
import platform
import re
import time
import uuid
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from cargomind import config

log = logging.getLogger("cargomind.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
KEEPALIVE_S = 15.0
_UPLOAD_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpg|gif|webp|bmp)$")
_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # disable proxy buffering (nginx / Cloud Run front ends)
}

def _warm_data() -> None:
    """Touch the DB once so the seed runs before the first dashboard poll."""
    with contextlib.suppress(Exception):
        importlib.import_module("cargomind.data.db").kpis()


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Start the Watchtower background scanner (if that module exists) and warm the DB."""
    task: asyncio.Task | None = None
    if os.getenv("CARGOMIND_WATCHTOWER", "1").lower() not in ("0", "false", "no", "off"):
        try:
            watchtower = importlib.import_module("cargomind.watchtower")
            task = asyncio.create_task(watchtower.run_watchtower(60), name="cargomind-watchtower")
            log.info("watchtower started (60s scan interval)")
        except Exception as exc:  # module missing or not a coroutine: dashboard still works
            log.info("watchtower not started: %s", exc)
    asyncio.get_running_loop().run_in_executor(None, _warm_data)
    try:
        yield
    finally:
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


app = FastAPI(
    title="CargoMind",
    version="0.1.0",
    description="Multi-agent logistics mission control (Google ADK + MCP + self-healing).",
    lifespan=_lifespan,
)


class _RevalidatingStaticFiles(StaticFiles):
    """Static files that always revalidate (ETag/304), so an update never runs stale JS/CSS."""

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", _RevalidatingStaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _json_default(o: Any) -> Any:
    """Best-effort JSON encoding for whatever the agent runtime hands us."""
    if hasattr(o, "model_dump"):
        try:
            return o.model_dump(mode="json", exclude_none=True)
        except Exception:  # pragma: no cover - defensive
            pass
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    if isinstance(o, (_dt.datetime, _dt.date, _dt.time)):
        return o.isoformat()
    if isinstance(o, enum.Enum):
        return o.value
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, (bytes, bytearray)):
        return f"<{len(o)} bytes>"
    return str(o)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=_json_default, ensure_ascii=False)


def _sse(obj: Any) -> str:
    """One Server-Sent-Events frame. JSON never contains raw newlines, so one data line suffices."""
    return f"data: {_dumps(obj)}\n\n"


def _jsonable(obj: Any) -> Any:
    return json.loads(_dumps(obj))


def _lazy(module: str):
    """Import a sibling subsystem on demand; 503 if it is not available (yet)."""
    try:
        return importlib.import_module(module)
    except Exception as exc:  # ImportError, or the module blew up while importing
        log.warning("subsystem %s unavailable: %s", module, exc)
        raise HTTPException(status_code=503, detail=f"{module} unavailable: {exc}") from exc


async def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call a sibling function that may be sync (run in a thread) or async."""
    if asyncio.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    result = await run_in_threadpool(fn, *args, **kwargs)
    if asyncio.iscoroutine(result):
        result = await result
    return result


def _as_list(result: Any, *keys: str) -> list:
    """Normalise db helpers that may return a list or a dict wrapping a list."""
    if result is None:
        return []
    if isinstance(result, list):
        return result
    if isinstance(result, tuple):
        return list(result)
    if isinstance(result, dict):
        for k in keys:
            if isinstance(result.get(k), list):
                return result[k]
        for v in result.values():
            if isinstance(v, list):
                return v
        return [result]
    return list(result) if isinstance(result, Iterable) and not isinstance(result, (str, bytes)) else [result]


def _pkg_version(dist: str) -> str | None:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _chaos_rate() -> float | None:
    try:
        chaos = importlib.import_module("cargomind.healing.chaos")
        return float(chaos.get_rate())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})


_FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="#0d1117"/>'
    '<path d="M7 22 L13 12 L19 18 L25 9" fill="none" stroke="#ffb020" stroke-width="3" stroke-linecap="round" '
    'stroke-linejoin="round"/><circle cx="25" cy="9" r="3" fill="#ffb020"/></svg>'
)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(_FAVICON, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


# ---------------------------------------------------------------------------
# chat (SSE relay of cargomind.runtime.stream_chat)
# ---------------------------------------------------------------------------
class ChatIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    message: str = Field(default="", max_length=8000)
    image_ref: str | None = None


async def _relay(stream: AsyncIterator[dict]) -> AsyncIterator[str]:
    """Pump the runtime iterator through a queue so we can emit keep-alives during
    long tool calls without cancelling the agent's own generator."""
    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    async def pump() -> None:
        try:
            async for event in stream:
                await queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("stream_chat failed")
            await queue.put({"type": "error", "author": "system", "text": f"Agent run failed: {type(exc).__name__}: {exc}"})
        finally:
            await queue.put(sentinel)

    task = asyncio.create_task(pump())
    saw_done = saw_error = False
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event is sentinel:
                break
            if not isinstance(event, dict):
                event = {"type": "text", "author": "agent", "text": str(event)}
            etype = event.get("type")
            saw_done |= etype == "done"
            saw_error |= etype == "error"
            yield _sse(event)
        if not saw_done and not saw_error:
            yield _sse({"type": "done", "author": "system"})
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(BaseException):
                await aclose()


@app.post("/api/chat")
async def chat(body: ChatIn) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        try:
            runtime = importlib.import_module("cargomind.runtime")
            stream = runtime.stream_chat(body.session_id, body.message, body.image_ref or None)
        except Exception as exc:
            log.warning("agent runtime unavailable: %s", exc)
            yield _sse({"type": "error", "author": "system", "text": f"Agent runtime unavailable: {type(exc).__name__}: {exc}"})
            return
        if asyncio.iscoroutine(stream):  # tolerate `async def stream_chat` returning an iterator
            stream = await stream
        async for frame in _relay(stream):
            yield frame

    return StreamingResponse(events(), media_type="text/event-stream", headers=_SSE_HEADERS)


# ---------------------------------------------------------------------------
# uploads
# ---------------------------------------------------------------------------
def _sniff_image(data: bytes) -> tuple[str, str] | None:
    """Return (extension, mime) for supported raster images by magic bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    if data.startswith(b"BM") and len(data) > 26:
        return "bmp", "image/bmp"
    return None


def _verify_with_pillow(data: bytes) -> bool:
    try:
        import io

        from PIL import Image
    except Exception:  # Pillow missing -> rely on magic bytes
        return True
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        return True
    except Exception:
        return False


@app.post("/api/upload")
async def upload(request: Request) -> dict:
    """Accepts multipart (`file` or `image` field) or JSON `{filename, data_b64}`."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES * 1.4 + 4096:
        raise HTTPException(413, "Image too large (max 8 MB).")

    ctype = request.headers.get("content-type", "")
    filename = "upload"
    if ctype.startswith("multipart/form-data"):
        try:
            form = await request.form(max_files=1, max_fields=10)
        except Exception as exc:
            raise HTTPException(400, f"Malformed multipart body: {exc}") from exc
        field = form.get("file") or form.get("image")
        if field is None or not hasattr(field, "read"):
            raise HTTPException(400, "Expected an image file in field 'file'.")
        filename = getattr(field, "filename", None) or filename
        data = await field.read(MAX_UPLOAD_BYTES + 1)
        await field.close()
    else:
        try:
            payload = await request.json()
            raw = str(payload.get("data_b64") or "")
            filename = str(payload.get("filename") or filename)
        except Exception as exc:
            raise HTTPException(400, "Send multipart/form-data or JSON {filename, data_b64}.") from exc
        if raw.startswith("data:") and "," in raw:  # tolerate data URLs
            raw = raw.split(",", 1)[1]
        if len(raw) > MAX_UPLOAD_BYTES * 4 // 3 + 8:
            raise HTTPException(413, "Image too large (max 8 MB).")
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(400, "data_b64 is not valid base64.") from exc

    if not data:
        raise HTTPException(400, "Empty upload.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Image too large (max 8 MB).")
    sniffed = _sniff_image(data)
    if sniffed is None or not _verify_with_pillow(data):
        raise HTTPException(415, "Unsupported file: upload a PNG, JPEG, GIF, WebP or BMP image.")
    ext, mime = sniffed

    upload_dir = Path(config.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{ext}"
    (upload_dir / name).write_bytes(data)
    return {
        "image_ref": name,
        "url": f"/api/uploads/{name}",
        "content_type": mime,
        "bytes": len(data),
        "original_filename": Path(filename).name[:120],
    }


@app.get("/api/uploads/{name}")
def get_upload(name: str) -> FileResponse:
    if not _UPLOAD_NAME_RE.match(name):
        raise HTTPException(404, "Not found")
    path = Path(config.UPLOAD_DIR) / name
    if not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(path)


# ---------------------------------------------------------------------------
# feedback loop
# ---------------------------------------------------------------------------
class FeedbackIn(BaseModel):
    session_id: str = Field(default="", max_length=128)
    rating: int | str
    comment: str = Field(default="", max_length=2000)
    message: str = Field(default="", max_length=20000)
    agent: str = Field(default="", max_length=128)


def _norm_rating(r: int | str) -> int:
    if isinstance(r, str):
        s = r.strip().lower()
        if s in ("up", "good", "positive", "+", "+1", "1", "thumbs_up", "true"):
            return 1
        if s in ("down", "bad", "negative", "-", "-1", "0", "thumbs_down", "false"):
            return -1
        try:
            r = int(float(s))
        except ValueError as exc:
            raise HTTPException(422, "rating must be up/down or an integer") from exc
    return 1 if r > 0 else -1


@app.post("/api/feedback")
def feedback(body: FeedbackIn) -> dict:
    store = _lazy("cargomind.feedback.store")
    rating = _norm_rating(body.rating)
    result = store.add_feedback(body.session_id, rating, comment=body.comment, message=body.message, agent=body.agent)
    try:
        lessons = store.lessons()
    except Exception:
        lessons = []
    return {"ok": True, "rating": rating, "result": _jsonable(result), "lessons": _jsonable(lessons)}


@app.get("/api/feedback/lessons")
def feedback_lessons(limit: int = Query(12, ge=1, le=100)) -> dict:
    store = _lazy("cargomind.feedback.store")
    lessons = store.lessons(limit=limit)
    stats: Any = None
    with contextlib.suppress(Exception):
        stats = store.stats()
    return {"lessons": _jsonable(lessons), "stats": _jsonable(stats)}


# ---------------------------------------------------------------------------
# logistics data
# ---------------------------------------------------------------------------
@app.get("/api/kpis")
def kpis() -> Any:
    db = _lazy("cargomind.data.db")
    return _jsonable(db.kpis())


@app.get("/api/shipments")
def shipments(
    status: str | None = None,
    city: str | None = None,
    priority: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    db = _lazy("cargomind.data.db")
    rows = _as_list(
        db.list_shipments(status=status or None, city=city or None, priority=priority or None, limit=limit),
        "shipments",
        "items",
        "results",
    )
    return {"shipments": _jsonable(rows), "count": len(rows)}


@app.get("/api/vehicles")
def vehicles(status: str | None = None, city: str | None = None) -> dict:
    db = _lazy("cargomind.data.db")
    rows = _as_list(db.list_vehicles(status=status or None, city=city or None), "vehicles", "items")
    return {"vehicles": _jsonable(rows), "count": len(rows)}


@app.get("/api/warehouses")
def warehouses() -> dict:
    db = _lazy("cargomind.data.db")
    rows = _as_list(db.list_warehouses(), "warehouses", "items")
    return {"warehouses": _jsonable(rows), "count": len(rows)}


@app.get("/api/inventory")
def inventory(low_stock_only: bool = False, sku: str | None = None, warehouse_id: str | None = None) -> dict:
    db = _lazy("cargomind.data.db")
    rows = _as_list(
        db.get_inventory(sku=sku or None, warehouse_id=warehouse_id or None, low_stock_only=low_stock_only),
        "inventory",
        "items",
    )
    return {"inventory": _jsonable(rows), "count": len(rows)}


@app.get("/api/outbox")
def outbox(limit: int = Query(30, ge=1, le=500)) -> dict:
    db = _lazy("cargomind.data.db")
    rows = _as_list(db.list_outbox(limit), "outbox", "messages", "items")
    return {"outbox": _jsonable(rows), "count": len(rows)}


@app.get("/api/cities")
def cities() -> dict:
    gaz = _lazy("cargomind.data.gazetteer")
    return {name: [float(lat), float(lon)] for name, (lat, lon) in gaz.CITIES.items()}


# ---------------------------------------------------------------------------
# self-healing
# ---------------------------------------------------------------------------
def _breaker_view(name: str, b: Any) -> dict:
    view: dict[str, Any] = {"name": str(getattr(b, "name", name) or name)}
    state = getattr(b, "state", None)
    if callable(state):
        with contextlib.suppress(Exception):
            state = state()
    if isinstance(state, enum.Enum):
        state = state.value
    view["state"] = str(state or "unknown").lower()
    for attr in ("failures", "failure_count", "consecutive_failures", "opened_at", "threshold", "cooldown_s"):
        val = getattr(b, attr, None)
        if isinstance(val, (int, float, str)) and not isinstance(val, bool):
            view[attr] = val
    return view


def _breaker_entry(name: str, v: Any) -> dict:
    if isinstance(v, dict):
        entry = {"name": name, **v}
        entry["state"] = str(entry.get("state", "unknown")).lower()
        return entry
    if isinstance(v, (str, int, float)):
        return {"name": name, "state": str(v).lower()}
    return _breaker_view(name, v)


def _breaker_snapshot() -> list[dict]:
    try:
        mod = importlib.import_module("cargomind.healing.breaker")
    except Exception:
        return []
    for fn_name in ("snapshot", "all_states", "states", "snapshot_all", "list_breakers", "all_breakers"):
        fn = getattr(mod, fn_name, None)
        if callable(fn):
            try:
                res = fn()
            except Exception:
                continue
            if isinstance(res, dict):
                return [_breaker_entry(str(k), v) for k, v in res.items()]
            if isinstance(res, list):
                return [_breaker_entry(str(i), r) for i, r in enumerate(res)]
    cls = getattr(mod, "CircuitBreaker", None)
    for attr in ("REGISTRY", "BREAKERS", "_REGISTRY", "_BREAKERS", "_registry", "_breakers", "registry"):
        reg = getattr(mod, attr, None) if mod else None
        reg = reg if reg is not None else (getattr(cls, attr, None) if cls else None)
        if isinstance(reg, dict):
            return [_breaker_view(str(k), v) for k, v in reg.items()]
    return []


@app.get("/api/healing")
def healing(limit: int = Query(60, ge=1, le=500)) -> dict:
    journal = _lazy("cargomind.healing.journal")
    recent = journal.recent(limit)
    stats: Any = None
    with contextlib.suppress(Exception):
        stats = journal.stats()
    return {
        "recent": _jsonable(recent),
        "stats": _jsonable(stats),
        "breakers": _jsonable(_breaker_snapshot()),
        "chaos_rate": _chaos_rate(),
    }


@app.get("/api/healing/stream")
async def healing_stream(request: Request) -> StreamingResponse:
    journal = _lazy("cargomind.healing.journal")
    queue = journal.subscribe()
    if asyncio.iscoroutine(queue):
        queue = await queue

    async def events() -> AsyncIterator[str]:
        try:
            yield "retry: 3000\n: connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
                except TimeoutError:
                    yield f": keepalive {int(time.time())}\n\n"
                    continue
                yield _sse(event)
        finally:
            for name in ("unsubscribe", "unsubscribe_queue", "remove_subscriber"):
                fn = getattr(journal, name, None)
                if callable(fn):
                    with contextlib.suppress(Exception):
                        res = fn(queue)
                        if asyncio.iscoroutine(res):
                            await res
                    break

    return StreamingResponse(events(), media_type="text/event-stream", headers=_SSE_HEADERS)


class ChaosIn(BaseModel):
    rate: float = Field(ge=0, le=100)


@app.get("/api/chaos")
def chaos_get() -> dict:
    chaos = _lazy("cargomind.healing.chaos")
    return {"rate": float(chaos.get_rate())}


@app.post("/api/chaos")
def chaos_set(body: ChaosIn) -> dict:
    chaos = _lazy("cargomind.healing.chaos")
    rate = body.rate / 100.0 if body.rate > 1 else body.rate  # accept 0..1 or a percentage
    rate = max(0.0, min(1.0, rate))
    chaos.set_rate(rate)
    return {"rate": float(chaos.get_rate())}


class McpChaosIn(BaseModel):
    down: bool = True
    times: int = Field(default=3, ge=1, le=20)


@app.post("/api/chaos/mcp")
def chaos_mcp(body: McpChaosIn) -> dict:
    """'Kill the MCP server': arm N deterministic tool faults (works even at chaos rate 0)."""
    chaos = _lazy("cargomind.healing.chaos")
    force_fail = getattr(chaos, "force_fail", None)
    if not callable(force_fail):
        raise HTTPException(501, "chaos.force_fail is not available in this build")
    times = body.times if body.down else 0
    force_fail("tool:", times)  # times=0 disarms
    return {"down": body.down, "injected": times, "prefix": "tool:"}


# ---------------------------------------------------------------------------
# watchtower (proactive alerts)
# ---------------------------------------------------------------------------
@app.get("/api/alerts")
async def alerts() -> dict:
    watchtower = _lazy("cargomind.watchtower")
    items = _as_list(await _call(watchtower.latest_alerts), "alerts", "active", "items")
    return {"alerts": _jsonable(items), "count": len(items)}


@app.post("/api/alerts/scan")
async def alerts_scan() -> dict:
    watchtower = _lazy("cargomind.watchtower")
    result = await _call(watchtower.scan_once)
    if isinstance(result, dict):
        new = _as_list(result.get("new"))
        active = _as_list(result.get("active"))
    else:
        new, active = [], _as_list(result)
    return {"new": _jsonable(new), "active": _jsonable(active)}


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict:
    mode = config.mode_summary()
    return {
        "status": "ok",
        "app": config.APP_NAME,
        "mode": _jsonable(mode),
        "offline_forced": config.FORCE_OFFLINE,
        "chaos_rate": _chaos_rate(),
        "components": {
            "runtime": _module_present("cargomind.runtime"),
            "data": _module_present("cargomind.data.db"),
            "mcp_server": _module_present("cargomind.mcp_server.server"),
            "healing": _module_present("cargomind.healing.journal"),
            "feedback": _module_present("cargomind.feedback.store"),
            "watchtower": _module_present("cargomind.watchtower"),
        },
        "versions": {
            "cargomind": _pkg_version("cargomind"),
            "google-adk": _pkg_version("google-adk"),
            "google-genai": _pkg_version("google-genai"),
            "mcp": _pkg_version("mcp"),
            "fastapi": _pkg_version("fastapi"),
            "python": platform.python_version(),
        },
        "time": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
    }


@app.exception_handler(HTTPException)
async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail, "status": exc.status_code}, status_code=exc.status_code)


def main() -> None:
    """Entry point for ``uv run cargomind-web``."""
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("HOST", "0.0.0.0")
    print(f"\n  CargoMind mission control -> http://localhost:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level=os.getenv("LOG_LEVEL", "info"), timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
