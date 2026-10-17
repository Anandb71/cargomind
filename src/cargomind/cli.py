"""`cargomind` command line.

  cargomind chat            interactive conversational agent in the terminal
  cargomind demo            scripted end-to-end scenario (great for judges)
  cargomind web             launch the mission-control dashboard (http://localhost:8080)
  cargomind mcp             run the logistics MCP server on stdio (for Claude Desktop / any MCP client)
  cargomind doctor          show which models / APIs are live and run a smoke test
  cargomind reset-db        re-seed the demo database
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

DEMO_SCRIPT = [
    "Morning ops briefing",
    "Where is SHP-1003 and will it make its SLA?",
    "Any weather risk on Mumbai → Pune today?",
    "Quote carriers Delhi → Bengaluru for 800kg express",
    "Plan today's dispatch from Mumbai",
    "Which SKUs need reordering?",
    "Draft a WhatsApp delay notice for SHP-1007",
    "Remember: always prioritise critical pharma shipments over express ones",
]


def _print_mode() -> None:
    from cargomind import config

    m = config.mode_summary()
    live = [k for k in ("gemini", "claude", "openai", "google_maps") if m[k]]
    print(f"CargoMind | live: {', '.join(live) or 'none'} | offline rule-engine fallback: on")
    print(f"models: {json.dumps(m['models'])}\n")


async def _chat() -> None:
    from cargomind.runtime import stream_chat

    _print_mode()
    sid = f"cli-{uuid.uuid4().hex[:6]}"
    print("Type a request (or 'exit'). Try: Where is SHP-1003?\n")
    while True:
        try:
            text = input("you › ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in ("exit", "quit", ":q"):
            break
        if not text:
            continue
        async for d in stream_chat(sid, text):
            t = d["type"]
            if t == "tool_call":
                print(f"   ↳ {d['author']} → {d['tool']}({json.dumps(d.get('args'), default=str)[:120]})")
            elif t == "healing":
                ev = d["event"]
                print(f"   ⚕ {ev.get('kind')}: {ev.get('component')} - {ev.get('detail', '')[:100]} {ev.get('action', '')}")
            elif t == "error":
                print(f"   ⚠ {d['text']}")
            elif t == "text" and d["author"] == "cargomind_concierge":
                served = f"  [{d.get('served_by')}]" if d.get("served_by") else ""
                print(f"\ncargomind{served} ›\n{d['text']}\n")


async def _demo(chaos: float) -> None:
    from cargomind.healing import chaos as chaos_mod
    from cargomind.runtime import ask

    _print_mode()
    if chaos:
        chaos_mod.set_rate(chaos)
        print(f"Chaos Monkey ON: {chaos:.0%} of tool/model calls fail on first try\n")
    for i, q in enumerate(DEMO_SCRIPT, 1):
        print(f"=== [{i}/{len(DEMO_SCRIPT)}] operator › {q}")
        print(await ask(q, session_id="demo", verbose=True))
        print()
    from cargomind.healing import journal

    print("Self-healing stats:", json.dumps(journal.stats(), default=str))


def _doctor() -> None:
    _print_mode()
    from cargomind.data import db

    print("db kpis:", json.dumps(db.kpis(), default=str)[:300])
    from cargomind.runtime import ask

    print("smoke:", asyncio.run(ask("Where is SHP-1003?", session_id="doctor"))[:400])


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="cargomind", description="Self-healing multi-agent logistics handler")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("chat")
    d = sub.add_parser("demo")
    d.add_argument("--chaos", type=float, default=0.0, help="fault-injection rate 0..1")
    w = sub.add_parser("web")
    w.add_argument("--port", type=int, default=None)
    sub.add_parser("mcp")
    sub.add_parser("doctor")
    sub.add_parser("reset-db")
    args = p.parse_args(argv)

    if args.cmd == "chat":
        asyncio.run(_chat())
    elif args.cmd == "demo":
        asyncio.run(_demo(args.chaos))
    elif args.cmd == "web":
        import os

        if args.port:
            os.environ["PORT"] = str(args.port)
        from cargomind.web.server import main as web_main

        web_main()
    elif args.cmd == "mcp":
        from cargomind.mcp_server.server import main as mcp_main

        mcp_main()
    elif args.cmd == "doctor":
        _doctor()
    elif args.cmd == "reset-db":
        from cargomind.data import db

        db.init_db(reset=True)
        print("database re-seeded")
    else:
        p.print_help()


if __name__ == "__main__":
    main()
