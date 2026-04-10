"""Pick an Ollama model from /api/tags when LLM_MODEL is unset (Docker-friendly)."""

from __future__ import annotations

import time
from typing import Optional

import httpx

_CACHE_TTL_SEC = 45.0
_cache_key: Optional[str] = None
_cache_names: Optional[list[str]] = None
_cache_expiry: float = 0.0


def list_ollama_model_names(base_url: str, *, timeout: float = 5.0) -> list[str]:
    url = f"{base_url.rstrip('/')}/api/tags"
    r = httpx.get(url, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    out: list[str] = []
    for m in data.get("models") or []:
        n = m.get("name") or m.get("model")
        if n:
            out.append(str(n))
    return out


def pick_preferred_ollama_model(names: list[str]) -> str:
    if not names:
        raise ValueError("no Ollama models")
    uniq = sorted(set(names))

    def first(pred) -> Optional[str]:
        for n in uniq:
            low = n.lower()
            if pred(low):
                return n
        return None

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
    """If explicit is non-empty, return it; else choose from /api/tags (cached)."""
    want = (explicit or "").strip()
    if want:
        return want
    names = _cached_names(base_url)
    if not names:
        raise RuntimeError(
            "Ollama returned no models at /api/tags. Pull one, e.g. "
            "`docker compose exec ollama ollama pull qwen2.5:7b`."
        )
    return pick_preferred_ollama_model(names)


def effective_ollama_model(request_model: Optional[str] = None) -> str:
    """API/scan `model` override, else settings.llm_model if set, else auto from /api/tags."""
    from boardman.settings import settings

    explicit = (request_model or "").strip() or (settings.llm_model or "").strip() or None
    return resolve_ollama_model(settings.ollama_base_url, explicit)
