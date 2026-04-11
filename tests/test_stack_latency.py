"""
Stack latency (opt-in). Fast path: warm-up + tiny num_predict + Boardman settings aligned.

  BOARDMAN_STACK_BENCHMARK=1 poetry run pytest tests/test_stack_latency.py -m stack_latency -s

Cold-load diagnostic (slow, ~model load to GPU):

  BOARDMAN_STACK_BENCHMARK=1 BOARDMAN_STACK_COLD_START=1 poetry run pytest tests/test_stack_latency.py -m stack_latency -s

`test_boardman_health_if_listening` runs without BOARDMAN_STACK_BENCHMARK (skips if API down).
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

from tests.conftest import OLLAMA_BASE

# Tiny generation for benchmark turns (Ollama options.num_predict)
_STACK_NUM_PREDICT = max(4, min(64, int(os.environ.get("STACK_LATENCY_NUM_PREDICT", "12"))))


def _stack_benchmark_enabled() -> bool:
    return os.environ.get("BOARDMAN_STACK_BENCHMARK", "").strip().lower() in ("1", "true", "yes")


def _cold_start_diagnostic() -> bool:
    return os.environ.get("BOARDMAN_STACK_COLD_START", "").strip().lower() in ("1", "true", "yes")


@pytest.mark.stack_latency
@pytest.mark.asyncio
async def test_ollama_and_chat_complete_latency(require_ollama_model: str, monkeypatch: pytest.MonkeyPatch):
    """
    One warm-up (unless cold-start mode), then timed warm steady-state:
    raw Ollama x2 + chat_complete x1 with num_predict capped.
    """
    if not _stack_benchmark_enabled():
        pytest.skip("Set BOARDMAN_STACK_BENCHMARK=1 for live Ollama timings")

    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "ollama_base_url", OLLAMA_BASE)
    monkeypatch.setattr(bs.settings, "llm_provider", "ollama")
    monkeypatch.setattr(bs.settings, "llm_model", require_ollama_model)
    monkeypatch.setattr(bs.settings, "ollama_num_predict", _STACK_NUM_PREDICT)
    ka = (os.environ.get("OLLAMA_KEEP_ALIVE") or bs.settings.ollama_keep_alive or "30m").strip()
    monkeypatch.setattr(bs.settings, "ollama_keep_alive", ka)

    from boardman.llm.completion import chat_complete

    base = OLLAMA_BASE.rstrip("/")
    model = require_ollama_model
    url_chat = f"{base}/api/chat"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Say OK in one word."}],
        "stream": False,
        "options": {"num_predict": _STACK_NUM_PREDICT},
        "keep_alive": ka,
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        if _cold_start_diagnostic():
            t0 = time.perf_counter()
            r1 = await client.post(url_chat, json=body)
            t_cold = time.perf_counter() - t0
            assert r1.status_code == 200, r1.text[:500]
            t0 = time.perf_counter()
            r2 = await client.post(url_chat, json=body)
            t_warm_raw = time.perf_counter() - t0
            assert r2.status_code == 200, r2.text[:500]
            print(
                f"\n[stack_latency] COLD_START raw #1: {t_cold:.3f}s  #2: {t_warm_raw:.3f}s  "
                f"model={model!r} num_predict={_STACK_NUM_PREDICT}"
            )
        else:
            wr = await client.post(url_chat, json=body)
            assert wr.status_code == 200, wr.text[:500]
            print(f"\n[stack_latency] warmup (uncounted) done  model={model!r}")

            t0 = time.perf_counter()
            a = await client.post(url_chat, json=body)
            t_a = time.perf_counter() - t0
            assert a.status_code == 200, a.text[:500]

            t0 = time.perf_counter()
            b = await client.post(url_chat, json=body)
            t_b = time.perf_counter() - t0
            assert b.status_code == 200, b.text[:500]
            print(
                f"[stack_latency] raw Ollama x2: {t_a:.3f}s, {t_b:.3f}s  "
                f"num_predict={_STACK_NUM_PREDICT} keep_alive={ka!r}"
            )

        msgs = [{"role": "user", "content": "Reply with exactly: OK"}]
        t0 = time.perf_counter()
        text = await chat_complete(msgs)
        t_cc = time.perf_counter() - t0
        assert text
        print(f"[stack_latency] chat_complete (num_predict={_STACK_NUM_PREDICT}): {t_cc:.3f}s  len={len(text)}")


def test_boardman_health_if_listening():
    """If Boardman runs locally, health should be fast (<2s)."""
    url = (os.environ.get("BOARDMAN_API_URL") or "http://127.0.0.1:8090").rstrip("/")
    try:
        t0 = time.perf_counter()
        r = httpx.get(f"{url}/api/v1/health", timeout=3.0)
        dt = time.perf_counter() - t0
    except (httpx.ConnectError, httpx.TimeoutException):
        pytest.skip(f"Boardman not reachable at {url}")
    assert r.status_code == 200, r.text
    print(f"\n[stack_latency] GET /health {dt*1000:.1f} ms @ {url}")
    assert dt < 2.0, f"health unexpectedly slow: {dt:.2f}s"
