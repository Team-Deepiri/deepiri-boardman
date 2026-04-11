"""
Ollama performance benchmarking.

Measures: latency, throughput, TTFT, memory, concurrent handling.

Run:
  pytest tests/test_ollama_performance.py -v -s

Environment:
  OLLAMA_BASE_URL=http://localhost:11434
  LLM_MODEL=qwen2.5:7b (optional, auto-detects)
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx
import pytest

OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")


def ollama_models() -> List[str]:
    """Get list of available Ollama models."""
    try:
        r = httpx.get(f"{OLLAMA_BASE}/api/tags", timeout=5.0)
        r.raise_for_status()
        return [m.get("name", "") for m in r.json().get("models", [])]
    except Exception:
        return []


def require_ollama_or_skip():
    """Skip test if Ollama not reachable."""
    try:
        r = httpx.get(f"{OLLAMA_BASE}/api/tags", timeout=3.0)
        if r.status_code != 200:
            pytest.skip("Ollama not reachable")
    except Exception as e:
        pytest.skip(f"Ollama not reachable: {e}")


def require_model_or_skip(model: str):
    """Skip test if model not available."""
    available = ollama_models()
    if not any(model in m for m in available):
        pytest.skip(f"Model {model!r} not available. Have: {available[:5]}")


@dataclass
class BenchmarkResult:
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_time_sec: float
    ttft_sec: float
    tokens_per_sec: float
    status_code: int


async def _chat_complete_async(
    model: str,
    messages: List[Dict[str, str]],
    timeout: float = 120.0,
    stream: bool = False,
) -> BenchmarkResult:
    """Make a single chat request and measure timing."""
    url = f"{OLLAMA_BASE}/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }

    start = time.monotonic()

    if stream:
        first_token_time = None
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=payload) as resp:
                content = b""
                async for line in resp.aiter_lines():
                    if first_token_time is None:
                        first_token_time = time.monotonic() - start
                    if line.strip():
                        content += line.encode("utf-8") + b"\n"
                total_time = time.monotonic() - start

        try:
            result = httpx.Response(200, content=content).json()
            completion = result.get("message", {}).get("content", "")
        except Exception:
            completion = ""
        ttft = first_token_time if first_token_time else 0
    else:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, json=payload)
            total_time = time.monotonic() - start
        r.raise_for_status()
        result = r.json()
        completion = result.get("message", {}).get("content", "")
        ttft = 0

    # Estimate tokens (rough: ~4 chars per token)
    prompt_tokens = sum(len(m.get("content", "")) // 4 for m in messages)
    completion_tokens = len(completion) // 4

    return BenchmarkResult(
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_time_sec=total_time,
        ttft_sec=ttft,
        tokens_per_sec=completion_tokens / total_time if total_time > 0 else 0,
        status_code=r.status_code if stream else 200,
    )


def _chat_complete_sync(model: str, messages: List[Dict[str, str]], timeout: float = 120.0) -> BenchmarkResult:
    """Sync version for simpler benchmarking."""
    url = f"{OLLAMA_BASE}/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
    }

    start = time.monotonic()
    with httpx.Client(timeout=timeout) as client:
        r = client.post(url, json=payload)
        total_time = time.monotonic() - start

    r.raise_for_status()
    result = r.json()
    completion = result.get("message", {}).get("content", "")

    prompt_tokens = sum(len(m.get("content", "")) // 4 for m in messages)
    completion_tokens = len(completion) // 4

    return BenchmarkResult(
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_time_sec=total_time,
        ttft_sec=0,  # not measured in sync mode
        tokens_per_sec=completion_tokens / total_time if total_time > 0 else 0,
        status_code=r.status_code,
    )


@pytest.mark.live_ollama
class TestOllamaPerformance:
    """Benchmark Ollama performance metrics."""

    @pytest.fixture(autouse=True)
    def setup(self):
        require_ollama_or_skip()

    def test_ollama_reachable_and_has_models(self):
        """Sanity check: Ollama is running with at least one model."""
        models = ollama_models()
        assert len(models) > 0, "No models available in Ollama"
        print(f"\nAvailable models: {models[:5]}")

    @pytest.mark.asyncio
    async def test_single_request_latency(self, monkeypatch):
        """Measure single request latency and throughput."""
        import boardman.settings as bs
        from boardman.llm.ollama_autodetect import effective_ollama_model

        model = effective_ollama_model(None)
        require_model_or_skip(model)
        
        monkeypatch.setattr(bs.settings, "ollama_base_url", OLLAMA_BASE)

        messages = [{"role": "user", "content": "Write a short one-sentence response."}]

        result = await _chat_complete_async(model, messages, timeout=120.0)

        print(f"\n--- Single Request Benchmark ---")
        print(f"Model: {result.model}")
        print(f"Total time: {result.total_time_sec:.2f}s")
        print(f"Completion tokens (est): {result.completion_tokens}")
        print(f"Throughput: {result.tokens_per_sec:.1f} tokens/sec")

        assert result.status_code == 200
        assert result.total_time_sec < 120, "Request took too long"

    @pytest.mark.asyncio
    async def test_concurrent_requests_throughput(self, monkeypatch):
        """Test throughput with concurrent requests."""
        import boardman.settings as bs
        from boardman.llm.ollama_autodetect import effective_ollama_model

        model = effective_ollama_model(None)
        require_model_or_skip(model)
        
        monkeypatch.setattr(bs.settings, "ollama_base_url", OLLAMA_BASE)

        messages = [{"role": "user", "content": "Reply with: OK"}]

        num_requests = 4
        start = time.monotonic()

        tasks = [
            _chat_complete_async(model, messages, timeout=120.0)
            for _ in range(num_requests)
        ]
        results = await asyncio.gather(*tasks)

        total_time = time.monotonic() - start

        successful = [r for r in results if r.status_code == 200]
        total_tokens = sum(r.completion_tokens for r in successful)

        print(f"\n--- Concurrent {num_requests} Requests ---")
        print(f"Total wall time: {total_time:.2f}s")
        print(f"Successful: {len(successful)}/{num_requests}")
        print(f"Total tokens: {total_tokens}")
        print(f"Throughput: {total_tokens / total_time:.1f} tokens/sec")
        print(f"Avg per request: {total_time / num_requests:.2f}s")

        assert len(successful) == num_requests, "Some requests failed"

    @pytest.mark.asyncio
    async def test_streaming_ttft(self, monkeypatch):
        """Measure Time To First Token with streaming."""
        import boardman.settings as bs
        from boardman.llm.ollama_autodetect import effective_ollama_model

        model = effective_ollama_model(None)
        require_model_or_skip(model)
        
        monkeypatch.setattr(bs.settings, "ollama_base_url", OLLAMA_BASE)

        url = f"{OLLAMA_BASE}/api/chat"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Count from 1 to 10."}],
            "stream": True,
        }

        ttft = None
        start = time.monotonic()

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", url, json=payload) as resp:
                async for line in resp.aiter_lines():
                    if line.strip():
                        if ttft is None:
                            ttft = time.monotonic() - start
                        if "content" in line:
                            break

        print(f"\n--- Streaming TTFT ---")
        print(f"Time to first token: {ttft:.3f}s")

        assert ttft is not None, "Never received response"
        assert ttft < 30, "TTFT too slow"

    def test_model_size_performance_comparison(self):
        """Compare performance across different model sizes."""
        models = ollama_models()
        
        if len(models) < 2:
            pytest.skip("Need at least 2 models for comparison")

        results = []
        for model in models[:3]:  # Test up to 3 models
            try:
                result = _chat_complete_sync(
                    model,
                    [{"role": "user", "content": "Say hi."}],
                    timeout=60.0,
                )
                results.append((model, result))
            except Exception as e:
                print(f"Skipping {model}: {e}")

        print(f"\n--- Model Comparison ---")
        for model, r in results:
            print(f"{model}: {r.total_time_sec:.2f}s, {r.tokens_per_sec:.1f} t/s")

        assert len(results) > 0, "No models succeeded"


@pytest.mark.live_ollama
class TestOllamaOptimizationSettings:
    """Test and document Ollama optimization settings."""

    def test_ollama_server_config(self):
        """Check Ollama server configuration via API."""
        # Ollama doesn't expose all config, but we can check running state
        try:
            r = httpx.get(f"{OLLAMA_BASE}/api/tags", timeout=5.0)
            assert r.status_code == 200
        except Exception as e:
            pytest.skip(f"Ollama not available: {e}")

        print("\n--- Ollama Config Info ---")
        print("To optimize Ollama, set environment variables:")
        print("  OLLAMA_NUM_PARALLEL=4      # concurrent requests")
        print("  OLLAMA_MAX_LOADED_MODELS=2 # keep models in memory")
        print("  OLLAMA_FLASH_ATTENTION=1   # enable flash attention")
        print("  OLLAMA_CONTEXT_LENGTH=4096 # reduce if memory limited")

    def test_memory_estimation(self):
        """Estimate VRAM usage for current model."""
        models = ollama_models()
        if not models:
            pytest.skip("No models available")

        # Rough estimates based on model size
        model_mem = {}
        for m in models:
            if "7b" in m.lower():
                model_mem[m] = "~8GB FP16, ~4GB Q4_K_M"
            elif "3b" in m.lower():
                model_mem[m] = "~6GB FP16, ~2GB Q4_K_M"
            elif "70b" in m.lower():
                model_mem[m] = "~140GB FP16 (needs multiple GPUs)"
            else:
                model_mem[m] = "~4GB"

        print("\n--- Estimated VRAM Usage ---")
        for m, mem in model_mem.items():
            print(f"  {m}: {mem}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])