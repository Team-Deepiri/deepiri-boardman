"""LangChain chat models keyed by settings (see docs/PLAN.md)."""

from __future__ import annotations

from typing import Any

from boardman.llm.ollama_autodetect import effective_ollama_model
from boardman.settings import settings


def get_chat_model(*, request_model: str | None = None) -> Any:
    """Return a LangChain BaseChatModel for the configured provider.

    ``request_model``: optional per-request Ollama tag (e.g. UI dropdown); passed to
    ``effective_ollama_model`` so the tool agent uses the same model as plain chat.
    """
    p = (settings.llm_provider or "ollama").lower()
    if p in ("claude",):
        p = "anthropic"

    if p == "ollama":
        from langchain_ollama import ChatOllama

        ka = (settings.ollama_keep_alive or "").strip()
        rm = (request_model or "").strip() or None
        kw: dict[str, Any] = {
            "model": effective_ollama_model(rm),
            "base_url": settings.ollama_base_url.rstrip("/"),
            "temperature": 0.2,
        }
        if ka:
            kw["keep_alive"] = ka
        np = settings.ollama_num_predict
        if np is not None and int(np) > 0:
            kw["num_predict"] = int(np)
        nctx = int(settings.ollama_num_ctx)
        if nctx > 0:
            kw["num_ctx"] = nctx
        return ChatOllama(**kw)

    if p == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=(settings.llm_model or "").strip() or "claude-sonnet-4-20250514",
            api_key=settings.anthropic_api_key or None,
            temperature=0.2,
        )

    if p in ("openai", "gpt"):
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=(settings.llm_model or "").strip() or "gpt-4o-mini",
            api_key=settings.openai_api_key or None,
            temperature=0.2,
        )

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
            model=(settings.llm_model or "").strip() or "anthropic/claude-3.5-sonnet",
            api_key=settings.openrouter_api_key or None,
            base_url=settings.openrouter_base_url.rstrip("/"),
            temperature=0.2,
            default_headers=default_headers or None,
        )

    if p in ("gemini", "google"):
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=(settings.llm_model or "").strip() or "gemini-2.0-flash",
            google_api_key=settings.gemini_api_key or None,
            temperature=0.2,
        )

    raise ValueError(f"Unsupported LLM_PROVIDER for LangChain: {settings.llm_provider}")
