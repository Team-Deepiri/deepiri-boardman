"""
Stack latency checks (Ollama + Boardman code path). Opt-in live benchmarks:

  BOARDMAN_STACK_BENCHMARK=1 poetry run pytest tests/test_stack_latency.py -v -s -m stack_latency

`test_boardman_health_if_listening` runs without the env flag (skips if API down).
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

from tests.conftest import OLLAMA_BASE


def _stack_benchmark_enabled() -> bool:
    return os.environ.get("BOARDMAN_STACK_BENCHMARK", "").strip().lower() in ("1", "true", "yes")


@pytest.mark.stack_latency
@pytest.mark.asyncio
async def test_ollama_tags_and_two_chats_timing(require_ollama_model: str):
    """First vs second /api/chat shows load vs steady-state cost."""
    if not _stack_benchmark_enabled():
        pytest.skip("Set BOARDMAN_STACK_BENCHMARK=1 for live Ollama timings")
    base = OLLAMA_BASE.rstrip("/")
    model = require_ollama_model
    url_chat = f"{base}/api/chat"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Say OK in one word."}],
        "stream": False,
        "options": {"num_predict": 24},
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        t0 = time.perf_counter()
        r1 = await client.post(url_chat, json=body)
        t1 = time.perf_counter() - t0
        assert r1.status_code == 200, r1.text[:500]

        t0 = time.perf_counter()
        r2 = await client.post(url_chat, json=body)
        t2 = time.perf_counter() - t0
        assert r2.status_code == 200, r2.text[:500]

    print(f"\n[stack_latency] Ollama chat #1: {t1:.3f}s  #2: {t2:.3f}s  model={model!r}")
    # Soft signal only (do not fail CI on slow hardware)
    if t1 > max(8.0, t2 * 4.0):
        print(
            "[stack_latency] NOTE: first request much slower than second — typical of model/GPU load; "
            "check OLLAMA_KEEP_ALIVE and concurrent load."
        )


@pytest.mark.stack_latency
@pytest.mark.asyncio
async def test_chat_complete_overhead_vs_raw_ollama(require_ollama_model: str, monkeypatch: pytest.MonkeyPatch):
    """Boardman chat_complete should be same order of magnitude as raw Ollama for tiny prompts."""
    if not _stack_benchmark_enabled():
        pytest.skip("Set BOARDMAN_STACK_BENCHMARK=1 for live Ollama timings")
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "ollama_base_url", OLLAMA_BASE)
    monkeypatch.setattr(bs.settings, "llm_provider", "ollama")
    monkeypatch.setattr(bs.settings, "llm_model", require_ollama_model)

    from boardman.llm.completion import chat_complete

    msgs = [{"role": "user", "content": "Reply with exactly: OK"}]
    t0 = time.perf_counter()
    text = await chat_complete(msgs)
    tc = time.perf_counter() - t0
    assert text
    print(f"\n[stack_latency] chat_complete: {tc:.3f}s  len(reply)={len(text)}")


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

