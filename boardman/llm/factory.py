"""LangChain chat models keyed by settings (see docs/PLAN.md)."""

from __future__ import annotations

from typing import Any

from boardman.llm.ollama_autodetect import effective_ollama_model
from boardman.settings import settings

# One chat-model instance per (provider, resolved model id). Rebuilding per turn
# constructs a new OpenAI SDK client with its own connection pool, so every answer paid a
# fresh TLS handshake to the LLM API on top of the GitHub ones. Chat models are stateless
# across calls; per-request provider/model overrides resolve BEFORE this cache key.
#
# The key uses the RESOLVED model id, not the raw setting — so Ollama's autodetect
# produces the real model name and a setting change from "gpt-4.1" to "gpt-4o" produces a
# different key rather than serving the cached gpt-4.1 instance. Settings are env-loaded
# at process start and do not change at runtime in production, but this is correct even if
# they did: the key always reflects what was built.
_model_cache: dict[tuple[str, str], Any] = {}


def clear_chat_model_cache() -> None:
    """Drop all cached model instances. Called by tests and on provider reconfiguration."""
    _model_cache.clear()


def get_chat_model() -> Any:
    """Return a LangChain BaseChatModel for the configured provider (cached per resolved model).

    The cache key is ``(provider, resolved_model_id)`` — never ``(provider, None)`` — so a
    setting change at runtime (or Ollama autodetecting a different model after a pull)
    produces a new instance rather than serving a stale one. In practice settings are
    env-loaded once per process, so this is a correctness guard, not a hot path.
    """
    prov = (settings.llm_provider or "ollama").lower()
    model_id = (settings.llm_model or "").strip()
    if prov == "ollama" and not model_id:
        model_id = effective_ollama_model(None)
    # Resolve the effective model for other providers too, so the key is never (prov, "")
    # while _build_chat_model defaults to a specific model internally.
    if not model_id:
        _PROVIDER_DEFAULTS = {
            "anthropic": "claude-sonnet-4-20250514",
            "claude": "claude-sonnet-4-20250514",
            "openai": "gpt-4.1",  # real OpenAI model (released April 2025)
            "gpt": "gpt-4.1",
            # Free-tier, tool-calling-capable — must never default to a paid model.
            "openrouter": "minimax/minimax-m3:free",
            "or": "minimax/minimax-m3:free",
            "gemini": "gemini-2.0-flash",
            "google": "gemini-2.0-flash",
        }
        model_id = _PROVIDER_DEFAULTS.get(prov, "")
    key = (prov, model_id)
    hit = _model_cache.get(key)
    if hit is not None:
        return hit
    model = _build_chat_model()
    _model_cache[key] = model
    return model


def _build_chat_model() -> Any:
    p = (settings.llm_provider or "ollama").lower()
    if p in ("claude",):
        p = "anthropic"

    if p == "ollama":
        from langchain_ollama import ChatOllama

        ka = (settings.ollama_keep_alive or "").strip()
        kw: dict[str, Any] = {
            "model": effective_ollama_model(None),
            "base_url": settings.ollama_base_url.rstrip("/"),
            "temperature": 0.2,
        }
        if ka:
            kw["keep_alive"] = ka
        np = settings.ollama_num_predict
        if np is not None and int(np) > 0:
            kw["num_predict"] = int(np)
        return ChatOllama(**kw)

    if p == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=(settings.llm_model or "").strip() or "claude-sonnet-4-20250514",
            api_key=settings.anthropic_api_key or None,
            temperature=0.2,
            max_retries=max(0, int(settings.llm_max_retries)),
        )

    if p in ("openai", "gpt"):
        from langchain_openai import ChatOpenAI

        # gpt-4.1: real OpenAI model (released April 2025), not a typo
        model = (settings.llm_model or "").strip() or "gpt-4.1"
        kw = {
            "model": model,
            "api_key": settings.openai_api_key or None,
            "max_retries": max(0, int(settings.llm_max_retries)),
        }
        # gpt-5* and o-series reasoning models reject non-default temperature.
        if not (model.startswith("gpt-5") or model.startswith("o")):
            kw["temperature"] = 0.2
        return ChatOpenAI(**kw)

    if p in ("openrouter", "or"):
        from langchain_openai import ChatOpenAI

        default_headers: dict[str, str] = {}
        referer = (settings.openrouter_referer or "").strip()
        app_title = (settings.openrouter_app_title or "").strip()
        if referer:
            default_headers["HTTP-Referer"] = referer
        if app_title:
            default_headers["X-Title"] = app_title

        return ChatOpenAI(
            model=(settings.llm_model or "").strip() or "minimax/minimax-m3:free",
            api_key=settings.openrouter_api_key or None,
            base_url=settings.openrouter_base_url.rstrip("/"),
            temperature=0.2,
            default_headers=default_headers or None,
            max_retries=max(0, int(settings.llm_max_retries)),
        )

    if p in ("gemini", "google"):
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=(settings.llm_model or "").strip() or "gemini-2.0-flash",
            google_api_key=settings.gemini_api_key or None,
            temperature=0.2,
            max_retries=max(0, int(settings.llm_max_retries)),
        )

    raise ValueError(f"Unsupported LLM_PROVIDER for LangChain: {settings.llm_provider}")
