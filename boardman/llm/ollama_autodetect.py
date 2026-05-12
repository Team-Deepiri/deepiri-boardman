"""Pick an Ollama model from /api/tags when LLM_MODEL is unset (Docker-friendly)."""

from __future__ import annotations

from deepiri_ollama.runtime import check

import subprocess
import time
from typing import Optional

import httpx

_CACHE_TTL_SEC = 45.0
_cache_key: Optional[str] = None
_cache_names: Optional[list[str]] = None
_cache_expiry: float = 0.0

_gpu_available: Optional[bool] = None


def is_gpu_available() -> bool:
    """Detect if an NVIDIA GPU is available (cached)."""
    global _gpu_available
    if _gpu_available is not None:
        return _gpu_available
    try:
        # Check for NVIDIA GPU via nvidia-smi
        result = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0 and "GPU" in result.stdout:
            _gpu_available = True
            return True
    except Exception:
        pass

    # Fallback/Additional check: check if /dev/nvidia0 exists (common in Docker with --gpus)
    import os
    if os.path.exists("/dev/nvidia0"):
        _gpu_available = True
        return True

    _gpu_available = False
    return False


def list_ollama_model_names(base_url: str, *, timeout: float = 5.0) -> list[str]:
    status = check(base_url=base_url)
    if not status["ok"]:
        return []
    return status.get("models", [])


def pick_preferred_ollama_model(names: list[str], prefer_small: bool = False) -> str:
    if not names:
        raise ValueError("no Ollama models")
    uniq = sorted(set(names))

    def first(pred) -> Optional[str]:
        for n in uniq:
            low = n.lower()
            if pred(low):
                return n
        return None

    # If CPU mode (prefer_small), prioritize tiny/small models for latency
    if prefer_small:
        for pred in (
            lambda low: "0.5b" in low,
            lambda low: "1.5b" in low,
            lambda low: "1b" in low,
            lambda low: "3b" in low,
            lambda low: "phi3" in low and "mini" in low,
        ):
            hit = first(pred)
            if hit:
                return hit

    # Default order (prefer Qwen 2.5)
    for pred in (
        lambda low: "qwen2.5" in low,
        lambda low: low.startswith("qwen"),
        lambda low: "llama" in low,
        lambda low: "mistral" in low,
        lambda low: "phi" in low,
        lambda low: "gemma" in low,
    ):
        hit = first(pred)
        if hit:
            return hit
    return uniq[0]


def _cached_names(base_url: str) -> list[str]:
    global _cache_key, _cache_names, _cache_expiry
    now = time.monotonic()
    if _cache_key == base_url and _cache_names is not None and now < _cache_expiry:
        return _cache_names
    names = list_ollama_model_names(base_url)
    _cache_key = base_url
    _cache_names = names
    _cache_expiry = now + _CACHE_TTL_SEC
    return names


def clear_ollama_model_cache() -> None:
    global _cache_key, _cache_names, _cache_expiry
    _cache_key = None
    _cache_names = None
    _cache_expiry = 0.0


def resolve_ollama_model(base_url: str, explicit: Optional[str]) -> str:
    """
    If explicit is non-empty, return it (unless it's from settings.llm_model and we're on CPU).
    If no GPU, fallback to settings.llm_ollama_cpu_model.
    """
    from boardman.settings import settings

    gpu = is_gpu_available()
    want = (explicit or "").strip()

    # Priority 1: GPU is available -> use explicit or auto-pick best
    if gpu:
        if want:
            return want
        names = _cached_names(base_url)
        if not names:
            raise RuntimeError("Ollama has no models. Pull one, e.g. `ollama pull qwen2.5:7b`.")
        return pick_preferred_ollama_model(names, prefer_small=False)

    # Priority 2: CPU mode -> check if we should fallback from explicit settings.llm_model
    # We only override if 'explicit' matches the default/configured 'llm_model' 
    # and isn't a specific one-off request model.
    # Actually, to be safe and clear, we use cpu_model if no GPU.
    
    cpu_model = (settings.llm_ollama_cpu_model or "").strip()
    if not gpu:
        names = _cached_names(base_url)
        if not names:
            raise RuntimeError("Ollama has no models.")
        
        # If user explicitly set a model in request, we still respect it? 
        # For 'boardman', the typical case is settings.llm_model being the "default".
        # If settings.llm_model is set but we are on CPU, we prefer the CPU model.
        
        if cpu_model and cpu_model in names:
            return cpu_model
        
        # If explicit is already a small model, keep it
        if want and any(s in want.lower() for s in ["0.5b", "1.5b", "1b", "3b", "mini"]):
            return want

        # Otherwise, auto-pick small
        return pick_preferred_ollama_model(names, prefer_small=True)

    return want or pick_preferred_ollama_model(_cached_names(base_url))


def effective_ollama_model(request_model: Optional[str] = None) -> str:
    """API/scan `model` override, else settings.llm_model if set, else auto from /api/tags."""
    from boardman.settings import settings

    # Request-level overrides should be honored as-is. This avoids an
    # unnecessary /api/tags lookup and lets callers intentionally probe
    # specific model tags (404 handling happens on /api/chat).
    if request_model:
        return request_model.strip()

    return resolve_ollama_model(settings.ollama_base_url, settings.llm_model)
