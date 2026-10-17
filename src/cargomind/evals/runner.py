"""CargoMind evaluation runner: hard numbers for routing, answers, latency and self-healing.

Replays every scenario from :mod:`cargomind.evals.scenarios` through the real
runtime (``runtime.stream_chat``, i.e. the full ADK Runner + SelfHealingPlugin),
each in a fresh session, and scores:

* routing precision / recall / F1 of the concierge's tool calls vs the expected set
* answer pass (expected substrings present, failure markers absent)
* task success (routing recall == 1 and answer pass and no runtime error)
* p50 / p95 latency, models that served the turns, model fallbacks
* self-healing events by kind, and under chaos the recovery rate
  (scenarios that still succeeded / total)

    uv run python -m cargomind.evals.runner --mode offline --out docs/EVAL_REPORT.md
    uv run python -m cargomind.evals.runner --mode offline --chaos 0.3 --out docs/EVAL_REPORT_CHAOS.md

Writes a markdown report to ``--out`` and the raw results as JSON next to it
(``EVAL_REPORT.md`` -> ``eval_results.json``, ``EVAL_REPORT_CHAOS.md`` -> ``eval_results_chaos.json``).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import shutil
import sys
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from cargomind.evals.scenarios import SAMPLE_IMAGES, Scenario, get_scenarios

ROOT_AUTHOR = "cargomind_concierge"
OFFLINE_MODEL = "offline/rule-engine"
DEFAULT_TIMEOUT_S = 180.0


# ----------------------------------------------------------------------------- results
@dataclass
class ScenarioResult:
    id: str
    category: str
    prompt: str
    pattern: str
    expected: list[str]
    routed: list[str]
    precision: float
    recall: float
    f1: float
    answer_pass: bool
    missing: list[str]
    forbidden_hits: list[str]
    success: bool
    latency_s: float
    served_by: list[str]
    healing: dict[str, int]
    errors: list[str]
    answer: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def _prf(routed: list[str], expected: set[str]) -> tuple[float, float, float]:
    got = set(routed)
    tp = len(got & expected)
    precision = tp / len(got) if got else (1.0 if not expected else 0.0)
    recall = tp / len(expected) if expected else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return round(precision, 3), round(recall, 3), round(f1, 3)


def _check_answer(sc: Scenario, answer: str) -> tuple[bool, list[str], list[str]]:
    low = answer.lower()
    missing: list[str] = []
    if sc.must_contain and not any(s in low for s in sc.must_contain):
        missing.append("any of: " + " | ".join(sc.must_contain))
    missing += [s for s in sc.must_contain_all if s not in low]
    forbidden = [s for s in sc.must_not_contain if s in low]
    ok = bool(answer.strip()) and not missing and not forbidden
    return ok, missing, forbidden


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (no interpolation; honest for small n)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return round(ordered[rank - 1], 3)


# ----------------------------------------------------------------------------- setup
def _apply_mode(mode: str) -> list[str]:
    """Configure offline/online BEFORE the agent graph is built. Returns notes for the report."""
    from cargomind import config

    notes: list[str] = []
    if mode == "offline":
        os.environ["CARGOMIND_OFFLINE"] = "1"
        config.FORCE_OFFLINE = True
    elif mode == "online":
        os.environ["CARGOMIND_OFFLINE"] = "0"
        config.FORCE_OFFLINE = False
        if not config.online_gemini():
            notes.append("online mode requested but no Gemini key / Vertex config: "
                         "every model chain falls back to the offline rule-engine automatically.")
    else:
        raise ValueError(f"mode must be 'offline' or 'online', got {mode!r}")
    agent_mod = sys.modules.get("cargomind.agent")
    if agent_mod is not None:
        try:
            chain = [m.model for m in agent_mod.root_agent.model.chain]
            built_offline = chain == [OFFLINE_MODEL]
            if built_offline != (mode == "offline" or not config.online_gemini()):
                notes.append(f"agent graph was already built before the eval (root chain: {chain}); "
                             "the effective mode is what 'served by' shows.")
        except Exception:  # noqa: BLE001
            pass
    return notes


def _stage_images() -> None:
    from cargomind import config

    for ref, rel in SAMPLE_IMAGES.items():
        src = config.ROOT_DIR / rel
        dst = config.UPLOAD_DIR / ref
        if src.is_file() and not dst.is_file():
            config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)


def _feedback_snapshot() -> tuple[set[str], int] | None:
    """Remember operator feedback state so the eval's own 'Remember: ...' turn can be undone."""
    try:
        from cargomind.feedback import store

        prefs = set(store.get_preferences())
        max_id = store._db().execute("SELECT COALESCE(MAX(id), 0) FROM feedback").fetchone()[0]
        return prefs, int(max_id)
    except Exception:  # noqa: BLE001
        return None


def _feedback_restore(snap: tuple[set[str], int] | None) -> None:
    if snap is None:
        return
    try:
        from cargomind.feedback import store

        prefs, max_id = snap
        for key in set(store.get_preferences()) - prefs:
            store.delete_preference(key)
        conn = store._db()
        conn.execute("DELETE FROM feedback WHERE id > ?", (max_id,))
        conn.commit()
    except Exception:  # noqa: BLE001
        pass


# ----------------------------------------------------------------------------- one scenario
async def run_scenario(sc: Scenario, timeout_s: float = DEFAULT_TIMEOUT_S) -> ScenarioResult:
    from cargomind.runtime import stream_chat

    session_id = f"eval-{sc.id}-{uuid.uuid4().hex[:6]}"
    routed: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    answers: list[str] = []
    served: list[str] = []
    healing: Counter[str] = Counter()
    errors: list[str] = []

    t0 = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_s):
            async for d in stream_chat(session_id, sc.prompt, sc.image_ref):
                kind = d.get("type")
                if kind == "tool_call" and d.get("author") == ROOT_AUTHOR:
                    routed.append(d.get("tool", ""))
                    tool_calls.append({"tool": d.get("tool"), "args": d.get("args")})
                    if d.get("served_by"):
                        served.append(d["served_by"])
                elif kind == "text" and d.get("author") == ROOT_AUTHOR:
                    answers.append(d.get("text") or "")
                    if d.get("served_by"):
                        served.append(d["served_by"])
                elif kind == "healing":
                    healing[(d.get("event") or {}).get("kind", "unknown")] += 1
                elif kind == "error":
                    errors.append(str(d.get("text", "error")))
    except TimeoutError:
        errors.append(f"timeout after {timeout_s:.0f}s")
    except Exception as exc:  # noqa: BLE001 - an eval must never crash on one scenario
        errors.append(f"{type(exc).__name__}: {exc}")
    latency = round(time.perf_counter() - t0, 3)

    answer = answers[-1] if answers else ""
    precision, recall, f1 = _prf(routed, set(sc.expected_root_tools))
    answer_pass, missing, forbidden = _check_answer(sc, answer)
    success = recall == 1.0 and answer_pass and not errors
    return ScenarioResult(
        id=sc.id, category=sc.category, prompt=sc.prompt, pattern=sc.pattern,
        expected=sorted(sc.expected_root_tools), routed=list(dict.fromkeys(routed)),
        precision=precision, recall=recall, f1=f1,
        answer_pass=answer_pass, missing=missing, forbidden_hits=forbidden,
        success=success, latency_s=latency, served_by=sorted(set(served)),
        healing=dict(healing), errors=errors, answer=answer, tool_calls=tool_calls,
    )


# ----------------------------------------------------------------------------- aggregate
def _summarise(results: list[ScenarioResult], chaos: float, journal_stats: dict) -> dict[str, Any]:
    n = len(results)
    tp = fp = fn = 0
    for r in results:
        got, exp = set(r.routed), set(r.expected)
        tp += len(got & exp)
        fp += len(got - exp)
        fn += len(exp - got)
    micro_p = tp / (tp + fp) if tp + fp else 0.0
    micro_r = tp / (tp + fn) if tp + fn else 0.0
    micro_f1 = 2 * micro_p * micro_r / (micro_p + micro_r) if micro_p + micro_r else 0.0
    latencies = [r.latency_s for r in results]
    healing: Counter[str] = Counter()
    for r in results:
        healing.update(r.healing)
    served: Counter[str] = Counter(m for r in results for m in r.served_by)
    succeeded = sum(r.success for r in results)
    summary: dict[str, Any] = {
        "scenarios": n,
        "succeeded": succeeded,
        "success_rate": round(succeeded / n, 3) if n else 0.0,
        "answer_pass_rate": round(sum(r.answer_pass for r in results) / n, 3) if n else 0.0,
        "routing_precision": round(micro_p, 3),
        "routing_recall": round(micro_r, 3),
        "routing_f1": round(micro_f1, 3),
        "routing_macro_f1": round(sum(r.f1 for r in results) / n, 3) if n else 0.0,
        "routing_exact_rate": round(sum(set(r.routed) == set(r.expected) for r in results) / n, 3) if n else 0.0,
        "latency_p50_s": _percentile(latencies, 50),
        "latency_p95_s": _percentile(latencies, 95),
        "latency_mean_s": round(sum(latencies) / n, 3) if n else 0.0,
        "errors": sum(len(r.errors) for r in results),
        "healing_events": sum(healing.values()),
        "healing_by_kind": dict(sorted(healing.items())),
        "fallbacks": healing.get("fallback", 0),
        "faults_injected": healing.get("fault", 0),
        "served_by": dict(served),
        "chaos_rate": chaos,
        "journal": journal_stats,
    }
    summary["recovery_rate"] = summary["success_rate"] if chaos > 0 else None
    return summary


# ----------------------------------------------------------------------------- report
def _mark(ok: bool) -> str:
    return "✅" if ok else "❌"


def _cell(text: str, limit: int = 90) -> str:
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def render_report(result: dict[str, Any]) -> str:
    s, env, rows = result["summary"], result["environment"], result["scenarios"]
    chaos = s["chaos_rate"]
    title = "CargoMind evaluation report" + (f" — chaos {chaos:.0%}" if chaos else "")
    live = [k for k in ("gemini", "claude", "openai", "google_maps") if env["live"].get(k)]
    lines = [
        f"# {title}",
        "",
        f"> {env['date']} · mode **{env['mode']}** (effective: {env['effective_mode']}) · "
        f"chaos **{chaos:.0%}** (seed {env['seed']}) · **{s['scenarios']} scenarios**, one fresh session each",
        "",
        "## Headline numbers",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| **Task success** (routing recall = 1, answer pass, no error) | **{s['succeeded']}/{s['scenarios']} ({_pct(s['success_rate'])})** |",
    ]
    if chaos:
        lines.append(f"| **Recovery rate under chaos** (scenarios still succeeding / total) | **{_pct(s['recovery_rate'])}** |")
        lines.append(f"| Faults injected → healing events | {s['faults_injected']} → {s['healing_events']} |")
    lines += [
        f"| Routing precision / recall / F1 (micro) | {s['routing_precision']:.3f} / {s['routing_recall']:.3f} / **{s['routing_f1']:.3f}** |",
        f"| Routing exact-set match | {_pct(s['routing_exact_rate'])} |",
        f"| Answer pass rate | {_pct(s['answer_pass_rate'])} |",
        f"| Latency p50 / p95 (mean) | {s['latency_p50_s']:.2f} s / {s['latency_p95_s']:.2f} s ({s['latency_mean_s']:.2f} s) |",
        f"| Model fallbacks (journaled) | {s['fallbacks']} |",
        f"| Self-healing events | {s['healing_events']}"
        + (f" ({', '.join(f'{k} {v}' for k, v in s['healing_by_kind'].items())})" if s["healing_by_kind"] else "")
        + " |",
        f"| Runtime errors | {s['errors']} |",
        "",
        "## Scenarios",
        "",
        "| # | Scenario | Category | Prompt | Routed to | Routing F1 | Answer | Latency | Healing | Result |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        heal = ", ".join(f"{k}×{v}" for k, v in sorted(r["healing"].items())) or "–"
        routed = ", ".join(f"`{t}`" for t in r["routed"]) or "_(none)_"
        lines.append(
            f"| {i} | `{r['id']}` | {r['category']} | {_cell(r['prompt'], 60)} | {routed} | "
            f"{_mark(r['recall'] == 1.0)} {r['f1']:.2f} | {_mark(r['answer_pass'])} | {r['latency_s']:.2f} s | {heal} | "
            f"{_mark(r['success'])} |"
        )
    failures = [r for r in rows if not r["success"] or r["precision"] < 1.0]
    if failures:
        lines += ["", "## Findings (failures and over-routing)", ""]
        for r in failures:
            verdict = "FAILED" if not r["success"] else "passed, but over-routed"
            lines += [
                f"### `{r['id']}` — {verdict}",
                "",
                f"- Prompt: “{r['prompt']}”",
                f"- Expected root tools: {', '.join(f'`{t}`' for t in r['expected'])} · routed: "
                + (", ".join(f"`{t}`" for t in r["routed"]) or "_(none)_")
                + f" (P {r['precision']:.2f} / R {r['recall']:.2f})",
            ]
            if r["missing"]:
                lines.append(f"- Missing in answer: {'; '.join(r['missing'])}")
            if r["forbidden_hits"]:
                lines.append(f"- Failure markers in answer: {', '.join(repr(x) for x in r['forbidden_hits'])}")
            if r["errors"]:
                lines.append(f"- Errors: {_cell('; '.join(r['errors']), 300)}")
            lines.append(f"- Answer excerpt: `{_cell(r['answer'], 240) or '(empty)'}`")
            lines.append("")
    lines += [
        "",
        "## Agent patterns covered",
        "",
        "| Scenario | Pattern exercised |",
        "|---|---|",
        *[f"| `{r['id']}` | {_cell(r['pattern'], 120)} |" for r in rows],
        "",
        "## Self-healing journal (this run)",
        "",
    ]
    j = s.get("journal") or {}
    by_kind = {k: v for k, v in (j.get("by_kind") or {}).items() if v}
    if by_kind:
        lines += ["| Kind | Count |", "|---|---|", *[f"| {k} | {v} |" for k, v in by_kind.items()], ""]
        lines.append(f"Journal recovery rate (recovered / (recovered + failed)): {_pct(j.get('recovery_rate'))}")
    else:
        lines.append("No healing events — nothing failed.")
    lines += [
        "",
        "## Environment",
        "",
        f"- Date: {env['date']}",
        f"- Mode requested: `{env['mode']}` · effective: **{env['effective_mode']}**",
        f"- Live providers: {', '.join(live) or 'none (offline rule-engine)'} · offline fallback: always on",
        f"- Turns served by: {', '.join(f'`{m}` ×{c}' for m, c in s['served_by'].items()) or 'n/a'}",
        f"- Configured models: {', '.join(f'{k}=`{v}`' for k, v in env['models'].items())}",
        f"- Chaos rate {chaos:.0%}, seed {env['seed']} · MCP: {env['mcp']}",
        f"- Python {env['python']} · google-adk {env['adk']} · {env['platform']}",
        f"- Reproduce: `{env['command']}`",
    ]
    for note in env.get("notes") or []:
        lines.append(f"- Note: {note}")
    lines += [
        "",
        "_Metric definitions: routing scores compare the set of tools the root concierge called with the expected set; "
        "answer pass = at least one expected phrase, all required IDs/cities, and no failure marker "
        "(e.g. a clarification fallback or a raw tool error); latency is wall-clock per turn including all "
        "sub-agents and tool calls; recovery rate = scenarios that still fully succeeded while faults were injected._",
        "",
    ]
    return "\n".join(lines)


def _json_path_for(out: Path) -> Path:
    stem = out.stem
    name = stem.lower().replace("eval_report", "eval_results")
    return out.with_name(f"{name}.json")


def _print_console(result: dict[str, Any]) -> None:
    s = result["summary"]
    print(f"\nCargoMind eval | mode={result['environment']['effective_mode']} chaos={s['chaos_rate']:.0%} "
          f"| {s['succeeded']}/{s['scenarios']} succeeded ({_pct(s['success_rate'])})")
    for r in result["scenarios"]:
        flag = "PASS" if r["success"] else "FAIL"
        routed = ",".join(r["routed"]) or "-"
        print(f"  {flag}  {r['id']:<22} {r['latency_s']:>6.2f}s  F1={r['f1']:.2f}  -> {routed}")
    print(f"  routing P/R/F1={s['routing_precision']:.2f}/{s['routing_recall']:.2f}/{s['routing_f1']:.2f}  "
          f"answers={_pct(s['answer_pass_rate'])}  p50={s['latency_p50_s']:.2f}s p95={s['latency_p95_s']:.2f}s  "
          f"healing={s['healing_events']} {s['healing_by_kind'] or ''}"
          + (f"  recovery={_pct(s['recovery_rate'])}" if s["chaos_rate"] else ""))
    if result.get("report_path"):
        print(f"  report: {result['report_path']}  json: {result['json_path']}")


# ----------------------------------------------------------------------------- entry points
async def run_eval(
    mode: str = "offline",
    chaos: float = 0.0,
    out: str | None = "docs/EVAL_REPORT.md",
    seed: int = 7,
    only: list[str] | None = None,
    *,
    warmup: bool = True,
    reset: bool = True,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    verbose: bool = True,
) -> dict:
    """Run the scenario suite and return ``{"summary", "scenarios", "environment", ...}``.

    Args:
      mode: ``offline`` (force the rule-engine) or ``online`` (use keys; falls back automatically).
      chaos: Fault-injection rate 0..1 for every tool / model call.
      out: Markdown report path (JSON written next to it); ``None`` to skip writing.
      seed: Chaos RNG seed (reproducible fault pattern).
      only: Optional scenario ids or categories to run.
      warmup: Run one untimed throw-away turn first so imports don't skew latency.
      reset: Re-seed the demo DB and clear the healing journal first.
      timeout_s: Per-scenario timeout.
      verbose: Print a compact console summary.
    """
    notes = _apply_mode(mode)
    from cargomind import config
    from cargomind.healing import breaker, chaos as chaos_mod, journal

    scenarios = get_scenarios(only)
    if not scenarios:
        raise ValueError(f"no scenarios match {only!r}")
    _stage_images()
    fb_snap = _feedback_snapshot()
    prev_rate = chaos_mod.get_rate()
    if reset:
        from cargomind.data import db

        db.reset_db()
    try:
        chaos_mod.set_rate(0.0)
        if warmup:
            from cargomind.runtime import ask

            try:
                await asyncio.wait_for(ask("hello", session_id=f"eval-warmup-{uuid.uuid4().hex[:6]}"), timeout_s)
            except Exception:  # noqa: BLE001
                pass
        breaker.reset_all()
        journal.clear()
        chaos_mod.seed(seed)
        chaos_mod.set_rate(chaos)

        results: list[ScenarioResult] = []
        for sc in scenarios:
            results.append(await run_scenario(sc, timeout_s=timeout_s))
        journal_stats = journal.stats()
    finally:
        chaos_mod.set_rate(prev_rate)
        _feedback_restore(fb_snap)

    mode_info = config.mode_summary()
    served_all = {m for r in results for m in r.served_by}
    online_models = sorted(m for m in served_all if m != OFFLINE_MODEL)
    effective = ("online: " + ", ".join(online_models)) if online_models else "offline rule-engine"
    if online_models and OFFLINE_MODEL in served_all:
        effective += " (+ offline fallback)"
    try:
        from importlib.metadata import version

        adk_version = version("google-adk")
    except Exception:  # noqa: BLE001
        adk_version = "?"
    cmd = f"uv run python -m cargomind.evals.runner --mode {mode}" + (f" --chaos {chaos:g}" if chaos else "")
    if out:
        cmd += f" --out {out}"
    environment = {
        "date": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z").strip(),
        "mode": mode,
        "effective_mode": effective,
        "live": {k: bool(mode_info.get(k)) for k in ("gemini", "claude", "openai", "google_maps")},
        "models": mode_info.get("models", {}),
        "seed": seed,
        "mcp": "in-process (CARGOMIND_MCP_LOCAL)" if config.FORCE_MCP_LOCAL else "stdio server (auto-fallback in-process)",
        "python": platform.python_version(),
        "adk": adk_version,
        "platform": f"{platform.system()} {platform.release()}",
        "command": cmd,
        "notes": notes,
        "only": only,
    }
    result: dict[str, Any] = {
        "summary": _summarise(results, chaos, journal_stats),
        "scenarios": [asdict(r) for r in results],
        "environment": environment,
        "report_path": None,
        "json_path": None,
    }
    if out:
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        json_path = _json_path_for(out_path)
        result["report_path"] = str(out_path)
        result["json_path"] = str(json_path)
        out_path.write_text(render_report(result), encoding="utf-8")
        json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    if verbose:
        _print_console(result)
    return result


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="cargomind eval", description="Run the CargoMind evaluation suite")
    p.add_argument("--mode", choices=["offline", "online"], default="offline")
    p.add_argument("--chaos", type=float, default=0.0, help="fault-injection rate 0..1")
    p.add_argument("--out", default="docs/EVAL_REPORT.md", help="markdown report path ('' to skip)")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--only", nargs="*", default=None, help="scenario ids or categories")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="per-scenario timeout (s)")
    args = p.parse_args(argv)
    if args.mode == "offline":  # before cargomind.config is imported by anything else
        os.environ["CARGOMIND_OFFLINE"] = "1"
    result = asyncio.run(run_eval(
        mode=args.mode, chaos=args.chaos, out=args.out or None, seed=args.seed, only=args.only,
        timeout_s=args.timeout,
    ))
    sys.exit(0 if result["summary"]["scenarios"] else 1)


if __name__ == "__main__":
    main()
