"""Runtime: one ADK Runner for the whole app + a streaming chat API that merges
agent events with live self-healing journal events (used by the web dashboard
and the CLI)."""
from __future__ import annotations

import asyncio
import json
import mimetypes
from typing import Any, AsyncIterator

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from cargomind import config

USER_ID = "operator"
_runner: Runner | None = None
_sessions = InMemorySessionService()


def get_runner() -> Runner:
    global _runner
    if _runner is None:
        from cargomind.agent import app

        _runner = Runner(app=app, session_service=_sessions, auto_create_session=True)
    return _runner


def _truncate(obj: Any, limit: int = 1800) -> Any:
    try:
        s = json.dumps(obj, default=str)
    except Exception:
        s = str(obj)
    return obj if len(s) <= limit else s[:limit] + "…"


def build_message(text: str, image_ref: str | None = None) -> types.Content:
    parts = [types.Part(text=text)]
    if image_ref:
        parts[0] = types.Part(text=f"{text}\n\n[Attached delivery photo: image_ref={image_ref}]")
        path = (config.UPLOAD_DIR / image_ref).resolve()
        if config.online_gemini() and path.is_file() and config.UPLOAD_DIR.resolve() in path.parents:
            mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
            parts.append(types.Part.from_bytes(data=path.read_bytes(), mime_type=mime))
    return types.Content(role="user", parts=parts)


def event_to_dicts(event) -> list[dict]:
    out: list[dict] = []
    author = event.author or "agent"
    served_by = (getattr(event, "custom_metadata", None) or {}).get("served_by")
    for fc in event.get_function_calls() or []:
        out.append({"type": "tool_call", "author": author, "tool": fc.name, "args": _truncate(dict(fc.args or {}), 600), "served_by": served_by})
    for fr in event.get_function_responses() or []:
        out.append({"type": "tool_result", "author": author, "tool": fr.name, "result": _truncate(fr.response)})
    if event.content and event.content.parts and not getattr(event, "partial", False):
        text = "".join(p.text for p in event.content.parts if getattr(p, "text", None) and not getattr(p, "thought", False))
        if text.strip():
            out.append({"type": "text", "author": author, "text": text, "served_by": served_by,
                        "final": bool(event.is_final_response())})
    return out


async def stream_chat(session_id: str, text: str, image_ref: str | None = None) -> AsyncIterator[dict]:
    """Run one operator turn; yield trace dicts as they happen."""
    from cargomind.healing import journal

    runner = get_runner()
    queue: asyncio.Queue = asyncio.Queue()
    healing_q = journal.subscribe()
    done = asyncio.Event()

    async def pump_agent():
        try:
            async for event in runner.run_async(
                user_id=USER_ID, session_id=session_id, new_message=build_message(text, image_ref)
            ):
                for d in event_to_dicts(event):
                    await queue.put(d)
        except Exception as exc:  # noqa: BLE001
            await queue.put({"type": "error", "author": "runtime", "text": f"{type(exc).__name__}: {exc}"})
        finally:
            done.set()

    async def pump_healing():
        while not done.is_set():
            try:
                ev = await asyncio.wait_for(healing_q.get(), timeout=0.25)
                await queue.put({"type": "healing", "author": "self_healing", "event": ev})
            except asyncio.TimeoutError:
                continue

    tasks = [asyncio.create_task(pump_agent()), asyncio.create_task(pump_healing())]
    yield {"type": "agent", "author": "cargomind_concierge", "text": "thinking…", "mode": config.mode_summary()}
    try:
        while not (done.is_set() and queue.empty()):
            try:
                yield await asyncio.wait_for(queue.get(), timeout=0.3)
            except asyncio.TimeoutError:
                continue
        while not healing_q.empty():
            yield {"type": "healing", "author": "self_healing", "event": healing_q.get_nowait()}
    finally:
        for t in tasks:
            t.cancel()
        try:
            journal.unsubscribe(healing_q)
        except Exception:
            pass
    yield {"type": "done", "author": "runtime"}


async def ask(text: str, session_id: str = "cli", image_ref: str | None = None, verbose: bool = False) -> str:
    """Convenience: run a turn and return the final concierge answer."""
    final = []
    async for d in stream_chat(session_id, text, image_ref):
        if verbose and d["type"] in ("tool_call", "healing", "error"):
            print("  ·", json.dumps(d, default=str)[:220])
        if d["type"] == "text" and d.get("author") == "cargomind_concierge":
            final.append(d["text"])
        if d["type"] == "error":
            final.append(f"⚠️ {d['text']}")
    return final[-1] if final else ""
