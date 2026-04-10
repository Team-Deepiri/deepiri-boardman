"""Shared test helpers."""

from __future__ import annotations

import os
from typing import Set

import httpx
import pytest

OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")


def ollama_http_reachable(timeout: float = 3.0) -> bool:
    try:
        r = httpx.get(f"{OLLAMA_BASE}/api/tags", timeout=timeout)
        return r.status_code == 200
    except (httpx.HTTPError, OSError):
        return False


def ollama_model_names() -> Set[str]:
    try:
        r = httpx.get(f"{OLLAMA_BASE}/api/tags", timeout=3.0)
        r.raise_for_status()
        data = r.json()
        out: set[str] = set()
        for m in data.get("models") or []:
            n = m.get("name") or m.get("model")
            if n:
                out.add(str(n))
        return out
    except (httpx.HTTPError, OSError, ValueError):
        return set()


def ollama_has_model(model: str) -> bool:
    return model in ollama_model_names()


@pytest.fixture(scope="session")
def live_ollama_ok() -> bool:
    return ollama_http_reachable()


@pytest.fixture
def require_ollama(live_ollama_ok: bool):
    if not live_ollama_ok:
        pytest.skip(f"Ollama not reachable at {OLLAMA_BASE} (set OLLAMA_BASE_URL if needed)")


@pytest.fixture
def require_ollama_model(require_ollama, monkeypatch) -> str:
    import boardman.settings as bs

    model = os.environ.get("LLM_MODEL") or bs.settings.llm_model
    if not ollama_has_model(model):
        have = sorted(ollama_model_names())[:8]
        pytest.skip(f"Model {model!r} not in Ollama (have: {have}); pull it or set LLM_MODEL")
    monkeypatch.setattr(bs.settings, "ollama_base_url", OLLAMA_BASE)
    monkeypatch.setattr(bs.settings, "llm_provider", "ollama")
    monkeypatch.setattr(bs.settings, "llm_model", model)
    return model


def pytest_collection_modifyitems(config, items) -> None:
    if ollama_http_reachable(timeout=1.5):
        return
    skip_live = pytest.mark.skip(reason=f"Ollama not reachable at {OLLAMA_BASE}")
    for item in items:
        if item.get_closest_marker("live_ollama"):
            item.add_marker(skip_live)
