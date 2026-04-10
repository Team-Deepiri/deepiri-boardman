"""Multi-provider chat completion (Ollama, Anthropic, OpenAI, Gemini) via httpx."""

from __future__ import annotations

import json
import re
from typing import Any, List, Optional

import httpx

from boardman.settings import settings

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
    mdl = model or settings.llm_model

    async with httpx.AsyncClient(timeout=timeout) as client:
        if prov == "ollama":
            return await _ollama_chat(client, mdl, messages)
        if prov == "anthropic":
            return await _anthropic_messages(client, mdl, messages)
        if prov in ("openai", "gpt"):
            return await _openai_chat(client, mdl, messages)
        if prov in ("gemini", "google"):
            return await _gemini_generate(client, mdl, messages)
        raise ValueError(f"Unknown LLM_PROVIDER: {prov}")


async def _ollama_chat(client: httpx.AsyncClient, model: str, messages: List[dict[str, str]]) -> str:
    url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
    body = {"model": model, "messages": messages, "stream": False}
    r = await client.post(url, json=body)
    r.raise_for_status()
    data = r.json()
    msg = data.get("message") or {}
    return (msg.get("content") or data.get("response") or "").strip()


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
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {settings.openai_api_key}", "content-type": "application/json"}
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
