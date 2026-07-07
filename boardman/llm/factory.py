"""LangChain chat models keyed by settings (see docs/PLAN.md)."""

from __future__ import annotations

from typing import Any

from boardman.llm.ollama_autodetect import effective_ollama_model
from boardman.settings import settings


def get_chat_model() -> Any:
    """Return a LangChain BaseChatModel for the configured provider."""
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
        )

    if p in ("openai", "gpt"):
        from langchain_openai import ChatOpenAI

        model = (settings.llm_model or "").strip() or "gpt-4.1"
        kw = {"model": model, "api_key": settings.openai_api_key or None}
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
