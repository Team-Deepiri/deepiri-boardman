#!/usr/bin/env python3
"""
Measure where time goes: Ollama, Boardman chat_complete, HTTP health, Plaky, optional nginx.

Run from repo root (uses .env like the app):
  poetry run python scripts/benchmark_stack.py

Pytest twin (opt-in live Ollama):
  BOARDMAN_STACK_BENCHMARK=1 poetry run pytest tests/test_stack_latency.py -m stack_latency -s

Env:
  OLLAMA_BASE_URL        (default http://127.0.0.1:11434)
  BOARDMAN_API_URL       (default http://127.0.0.1:8090) — health + optional agent POST
  BOARDMAN_NGINX_URL     (e.g. http://127.0.0.1:8088) — also times /api/v1/health through nginx
  SKIP_AGENT_HTTP        (1 = do not POST /agent/chat)
  PLAKY_BENCHMARK_BOARD_ID  — if set, times fetch_board_schema_bundle after list_boards
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx


def _load_dotenv() -> None:
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key:
            os.environ.setdefault(key, val)


def _fmt_ms(seconds: float) -> str:
    return f"{seconds * 1000.0:.1f} ms"


async def _time(coro) -> Tuple[Any, float]:
    t0 = time.perf_counter()
    out = await coro
    return out, time.perf_counter() - t0


async def ollama_tags(base: str) -> Tuple[Optional[dict], float]:
    url = f"{base.rstrip('/')}/api/tags"
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await _time(client.get(url))


async def ollama_chat(
    base: str,
    model: str,
    *,
    num_predict: int = 32,
) -> Tuple[Optional[dict], float]:
    url = f"{base.rstrip('/')}/api/chat"
    body: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly one word: OK"}],
        "stream": False,
        "options": {"num_predict": num_predict},
    }
    async with httpx.AsyncClient(timeout=180.0) as client:
        return await _time(client.post(url, json=body))


def _pick_model(tags_json: Optional[dict]) -> Optional[str]:
    if not tags_json:
        return None
    models = (tags_json.get("models") or []) if isinstance(tags_json, dict) else []
    names = [str(m.get("name") or "") for m in models if m.get("name")]
    if not names:
        return None
    env = (os.environ.get("LLM_MODEL") or "").strip()
    if env and env in names:
        return env
    for n in names:
        if "7b" in n.lower() or "8b" in n.lower():
            return n
    return names[0]


async def boardman_health(url: str) -> Tuple[Optional[httpx.Response], float]:
    h = url.rstrip("/")
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await _time(client.get(f"{h}/api/v1/health"))


async def boardman_agent_ping(url: str) -> Tuple[Optional[httpx.Response], float]:
    h = url.rstrip("/")
    payload = {
        "message": "Reply with exactly the word PING and nothing else.",
        "use_tools": False,
        "allow_writes": False,
    }
    async with httpx.AsyncClient(timeout=180.0) as client:
        return await _time(client.post(f"{h}/api/v1/agent/chat", json=payload))


async def amain() -> int:
    _load_dotenv()
    # Import after env so settings see .env
    from boardman.llm.completion import chat_complete
    from boardman.settings import settings

    ollama_base = (os.environ.get("OLLAMA_BASE_URL") or settings.ollama_base_url or "http://127.0.0.1:11434").rstrip(
        "/"
    )
    boardman_url = (os.environ.get("BOARDMAN_API_URL") or "http://127.0.0.1:8090").rstrip("/")
    nginx_url = (os.environ.get("BOARDMAN_NGINX_URL") or "").strip().rstrip("/")
    skip_agent = (os.environ.get("SKIP_AGENT_HTTP") or "").strip() in ("1", "true", "yes")
    bench_board = (os.environ.get("PLAKY_BENCHMARK_BOARD_ID") or "").strip()

    lines: List[str] = []
    lines.append("=== Boardman stack latency probe ===")
    lines.append(f"Ollama:     {ollama_base}")
    lines.append(f"Boardman:   {boardman_url}")
    if nginx_url:
        lines.append(f"Nginx UI:   {nginx_url}")
    lines.append("")

    # --- Ollama ---
    r_tags, t_tags = await ollama_tags(ollama_base)
    lines.append(f"[Ollama] GET /api/tags     {_fmt_ms(t_tags)}  HTTP {getattr(r_tags, 'status_code', '?')}")
    if r_tags is None or r_tags.status_code != 200:
        lines.append("  → Ollama unreachable; stop here for GPU/API checks.")
        print("\n".join(lines), flush=True)
        return 1

    try:
        tags_json = r_tags.json()
    except Exception:
        tags_json = None
    model = _pick_model(tags_json)
    lines.append(f"[Ollama] using model: {model!r}")
    if not model:
        lines.append("  → No models in /api/tags")
        print("\n".join(lines), flush=True)
        return 1

    _, t1 = await ollama_chat(ollama_base, model)
    lines.append(f"[Ollama] POST /api/chat #1 (num_predict=32)  {_fmt_ms(t1)}")
    _, t2 = await ollama_chat(ollama_base, model)
    lines.append(f"[Ollama] POST /api/chat #2 (warm)              {_fmt_ms(t2)}")
    if t1 > 0 and t2 > 0 and t1 > t2 * 2.5:
        lines.append(
            f"  → First call much slower ({t1:.2f}s vs {t2:.2f}s): likely model load / cold GPU; check OLLAMA_KEEP_ALIVE."
        )

    # --- Same path as API plain chat (shared httpx client inside completion) ---
    msgs = [{"role": "user", "content": "Reply with exactly one word: OK"}]
    _, tc1 = await _time(chat_complete(msgs, model=model))
    lines.append(f"[Boardman] chat_complete #1 (Ollama via app settings)  {_fmt_ms(tc1)}")
    _, tc2 = await _time(chat_complete(msgs, model=model))
    lines.append(f"[Boardman] chat_complete #2 (warm)                     {_fmt_ms(tc2)}")

    # --- HTTP Boardman ---
    rh, th = await boardman_health(boardman_url)
    sc = rh.status_code if rh is not None else None
    lines.append(f"[HTTP] GET {boardman_url}/api/v1/health  {_fmt_ms(th)}  HTTP {sc}")
    if sc != 200:
        lines.append("  → Boardman not up on this URL (start API or set BOARDMAN_API_URL).")

    if nginx_url:
        rn, tn = await boardman_health(nginx_url)
        sn = rn.status_code if rn is not None else None
        lines.append(f"[HTTP] GET {nginx_url}/api/v1/health (via nginx)  {_fmt_ms(tn)}  HTTP {sn}")
        if sc == 200 and sn == 200 and abs(tn - th) > 0.05:
            lines.append(f"  → Direct vs nginx health delta {_fmt_ms(abs(tn - th))} (usually tiny).")

    if not skip_agent and sc == 200:
        ra, ta = await boardman_agent_ping(boardman_url)
        sa = ra.status_code if ra is not None else None
        lines.append(f"[HTTP] POST .../agent/chat (plain, use_tools=false)  {_fmt_ms(ta)}  HTTP {sa}")
        if ra is not None and ra.status_code == 200:
            try:
                body = ra.json()
                reply = (body.get("reply") or "")[:120]
                lines.append(f"  → reply preview: {reply!r}")
            except Exception:
                pass
        elif ra is None:
            lines.append("  → request failed (timeout/refused)")
    elif skip_agent:
        lines.append("[HTTP] SKIP_AGENT_HTTP set — skipped POST /agent/chat")

    # --- Plaky ---
    key = (os.environ.get("PLAKY_API_KEY") or settings.plaky_api_key or "").strip()
    if key:
        from boardman.plaky.client import PlakyClient

        async def _list_boards():
            return await PlakyClient().list_boards()

        boards_r, tp = await _time(_list_boards())
        lines.append(f"[Plaky] list_boards  {_fmt_ms(tp)}  ok={boards_r.get('ok')}")
        if bench_board:
            from boardman.plaky.board_schema import fetch_board_schema_bundle

            _, ts = await _time(fetch_board_schema_bundle(bench_board))
            lines.append(f"[Plaky] fetch_board_schema_bundle({bench_board})  {_fmt_ms(ts)}")
    else:
        lines.append("[Plaky] PLAKY_API_KEY unset — skipped")

    lines.append("")
    lines.append("=== Readout ===")
    lines.append(
        "- 504 at the browser usually means a proxy gave up before Boardman returned; compare "
        "POST agent/chat time above to your proxy read_timeout."
    )
    lines.append(
        "- If Ollama #1 >> #2, reduce cold starts with keep_alive / avoid unloading between requests."
    )
    lines.append(
        "- If agent/chat >> chat_complete, overhead is FastAPI + DB + prompt size (not raw Ollama)."
    )

    out = "\n".join(lines)
    print(out, flush=True)
    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(amain()))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
