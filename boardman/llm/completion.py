"""Multi-provider chat completion (Ollama, Anthropic, OpenAI, Gemini) via httpx."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import Any, List, Optional

import httpx

from boardman.llm.ollama_autodetect import effective_ollama_model
from boardman.settings import settings

# One AsyncClient per running event loop (uvicorn: single loop; pytest-asyncio: many loops).
_ollama_by_loop: dict[int, httpx.AsyncClient] = {}


def _ollama_http_client() -> httpx.AsyncClient:
    """Shared keep-alive client for Ollama (reduces TCP setup per agent turn)."""
    global _ollama_by_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()
    lid = id(loop)
    c = _ollama_by_loop.get(lid)
    if c is None or getattr(c, "is_closed", False):
        read_timeout = float(getattr(settings, "ollama_read_timeout_seconds", 300.0) or 300.0)
        c = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=read_timeout, write=60.0, pool=60.0),
            limits=httpx.Limits(max_keepalive_connections=24, max_connections=48),
        )
        _ollama_by_loop[lid] = c
    return c


async def aclose_ollama_http_client() -> None:
    global _ollama_by_loop
    try:
        loop = asyncio.get_running_loop()
        lid = id(loop)
        c = _ollama_by_loop.pop(lid, None)
        if c is not None and not getattr(c, "is_closed", False):
            fn = getattr(c, "aclose", None)
            if callable(fn):
                await fn()
    except RuntimeError:
        for c in list(_ollama_by_loop.values()):
            if not getattr(c, "is_closed", False):
                fn = getattr(c, "aclose", None)
                if callable(fn):
                    await fn()
        _ollama_by_loop.clear()


def _extract_system(messages: List[dict[str, str]]) -> tuple[Optional[str], List[dict[str, str]]]:
    system_parts: List[str] = []
    rest: List[dict[str, str]] = []
    for m in messages:
        if m.get("role") == "system":
            system_parts.append(m.get("content", ""))
        else:
            rest.append(m)
    system = "\n\n".join(system_parts) if system_parts else None
    return system, rest


async def chat_complete(
    messages: List[dict[str, str]],
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    timeout: float = 120.0,
) -> str:
    prov = (provider or settings.llm_provider or "ollama").lower()
    if prov == "ollama":
        mdl = effective_ollama_model(model)
    else:
        mdl = (model or settings.llm_model or "").strip()
        if prov == "anthropic":
            mdl = mdl or "claude-sonnet-4-20250514"
        elif prov in ("openai", "gpt"):
            mdl = mdl or "gpt-4.1"
        elif prov in ("openrouter", "or"):
            mdl = mdl or "anthropic/claude-3.5-sonnet"
        elif prov in ("gemini", "google"):
            mdl = mdl or "gemini-2.0-flash"

    if prov == "ollama":
        return await _ollama_chat(_ollama_http_client(), mdl, messages)

    async with httpx.AsyncClient(timeout=timeout) as client:
        if prov == "anthropic":
            return await _anthropic_messages(client, mdl, messages)
        if prov in ("openai", "gpt"):
            return await _openai_chat(client, mdl, messages)
        if prov in ("openrouter", "or"):
            return await _openrouter_chat(client, mdl, messages)
        if prov in ("gemini", "google"):
            return await _gemini_generate(client, mdl, messages)
        raise ValueError(f"Unknown LLM_PROVIDER: {prov}")


async def _ollama_chat(client: httpx.AsyncClient, model: str, messages: List[dict[str, str]]) -> str:
    url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
    body: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
    ka = (settings.ollama_keep_alive or "").strip()
    if ka:
        body["keep_alive"] = ka
    n = settings.ollama_num_predict
    if n is not None and int(n) > 0:
        body["options"] = {"num_predict": int(n)}
    r = await client.post(url, json=body)
    r.raise_for_status()
    data = r.json()
    msg = data.get("message") or {}
    return (msg.get("content") or data.get("response") or "").strip()


async def _ollama_chat_stream(
    client: httpx.AsyncClient, model: str, messages: List[dict[str, str]]
) -> AsyncIterator[str]:
    """Yield assistant content deltas from Ollama NDJSON stream (stream=true)."""
    url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
    body: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
    ka = (settings.ollama_keep_alive or "").strip()
    if ka:
        body["keep_alive"] = ka
    n = settings.ollama_num_predict
    if n is not None and int(n) > 0:
        body["options"] = {"num_predict": int(n)}
    async with client.stream("POST", url, json=body) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            raw = (line or "").strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg = data.get("message") or {}
            piece = msg.get("content") or ""
            if piece:
                yield piece
            if data.get("done") is True:
                break


async def chat_complete_stream(
    messages: List[dict[str, str]],
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> AsyncIterator[str]:
    """
    Stream completion chunks. Ollama uses native streaming; other providers emit one chunk (full text).
    """
    prov = (provider or settings.llm_provider or "ollama").lower()
    if prov == "ollama":
        mdl = effective_ollama_model(model)
        async for part in _ollama_chat_stream(_ollama_http_client(), mdl, messages):
            yield part
        return

    text = await chat_complete(messages, provider=provider, model=model)
    if text:
        yield text


async def _anthropic_messages(
    client: httpx.AsyncClient, model: str, messages: List[dict[str, str]]
) -> str:
    if not settings.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set")
    system, rest = _extract_system(messages)
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": m["role"], "content": m["content"]} for m in rest if m["role"] != "system"],
    }
    if system:
        body["system"] = system
    r = await client.post(url, headers=headers, json=body)
    r.raise_for_status()
    data = r.json()
    parts = data.get("content") or []
    texts: List[str] = []
    for p in parts:
        if isinstance(p, dict) and p.get("type") == "text":
            texts.append(p.get("text", ""))
    return "".join(texts).strip()


async def _openai_chat(client: httpx.AsyncClient, model: str, messages: List[dict[str, str]]) -> str:
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is not set")
    return await _openai_compat_chat(
        client,
        base_url="https://api.openai.com/v1",
        api_key=settings.openai_api_key,
        model=model,
        messages=messages,
    )


async def _openrouter_chat(client: httpx.AsyncClient, model: str, messages: List[dict[str, str]]) -> str:
    if not settings.openrouter_api_key:
        raise ValueError("OPENROUTER_API_KEY is not set")
    extra_headers: dict[str, str] = {}
    referer = (settings.openrouter_referer or "").strip()
    app_title = (settings.openrouter_app_title or "").strip()
    if referer:
        extra_headers["HTTP-Referer"] = referer
    if app_title:
        extra_headers["X-Title"] = app_title
    return await _openai_compat_chat(
        client,
        base_url=settings.openrouter_base_url,
        api_key=settings.openrouter_api_key,
        model=model,
        messages=messages,
        extra_headers=extra_headers,
    )


async def _openai_compat_chat(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: List[dict[str, str]],
    extra_headers: Optional[dict[str, str]] = None,
) -> str:
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    body = {"model": model, "messages": messages}
    r = await client.post(url, headers=headers, json=body)
    r.raise_for_status()
    data = r.json()
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    return (msg.get("content") or "").strip()


async def _gemini_generate(
    client: httpx.AsyncClient, model: str, messages: List[dict[str, str]]
) -> str:
    if not settings.gemini_api_key:
        raise ValueError("GEMINI_API_KEY is not set")
    system, rest = _extract_system(messages)
    parts: List[str] = []
    if system:
        parts.append(f"System:\n{system}\n\n")
    for m in rest:
        parts.append(f"{m['role']}:\n{m['content']}\n\n")
    prompt = "".join(parts).strip()
    mid = model if "/" in model or model.startswith("gemini") else f"models/{model}"
    if not mid.startswith("models/"):
        mid = f"models/{mid}"
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/{mid}:generateContent"
        f"?key={settings.gemini_api_key}"
    )
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    r = await client.post(url, json=body)
    r.raise_for_status()
    data = r.json()
    cands = data.get("candidates") or []
    if not cands:
        return ""
    content = cands[0].get("content") or {}
    prts = content.get("parts") or []
    texts = [p.get("text", "") for p in prts if isinstance(p, dict)]
    return "".join(texts).strip()


def parse_json_tasks(text: str) -> Any:
    """Best-effort parse of JSON array from LLM output."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError("Model did not return valid JSON array")
