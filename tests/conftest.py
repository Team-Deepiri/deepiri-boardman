"""Shared test helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Set


def _load_dotenv_file_into_environ() -> None:
    """Load repo-root `.env` into os.environ (keys not already set) so tests see PLAKY_API_KEY etc."""
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key:
            os.environ.setdefault(key, val)


_load_dotenv_file_into_environ()

import httpx
import pytest


@pytest.fixture(autouse=True)
def _disable_agent_rate_limit_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid flaky 429s on fast multi-request agent tests."""
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_rate_limit_enabled", False)


@pytest.fixture(autouse=True)
async def _aclose_ollama_http_client_after_async_test() -> None:
    """Per-loop httpx client must close before pytest tears down the event loop."""
    yield
    from boardman.llm.completion import aclose_ollama_http_client

    await aclose_ollama_http_client()

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
    import boardman.llm.ollama_autodetect as oa
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "ollama_base_url", OLLAMA_BASE)
    monkeypatch.setattr(bs.settings, "llm_provider", "ollama")
    override = (os.environ.get("LLM_MODEL") or "").strip()
    if override:
        monkeypatch.setattr(bs.settings, "llm_model", override)
        model = override
        if not ollama_has_model(model):
            have = sorted(ollama_model_names())[:8]
            pytest.skip(f"Model {model!r} not in Ollama (have: {have}); pull it or set LLM_MODEL")
    else:
        monkeypatch.setattr(bs.settings, "llm_model", "")
        oa.clear_ollama_model_cache()
        try:
            model = oa.resolve_ollama_model(OLLAMA_BASE, None)
        except RuntimeError as e:
            pytest.skip(str(e))
        if not ollama_has_model(model):
            have = sorted(ollama_model_names())[:8]
            pytest.skip(f"Auto model {model!r} not in Ollama (have: {have})")
    return model


@pytest.fixture
def ollama_model_resolved(require_ollama, monkeypatch) -> str:
    """
    Pick an Ollama model that actually exists: honors LLM_MODEL when pulled, otherwise
    auto-resolve, otherwise first tag from `ollama list` (so .env can list a model you
    did not pull without skipping the whole agent E2E suite).
    """
    import boardman.llm.ollama_autodetect as oa
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "ollama_base_url", OLLAMA_BASE)
    monkeypatch.setattr(bs.settings, "llm_provider", "ollama")

    override = (os.environ.get("LLM_MODEL") or "").strip()
    if override and ollama_has_model(override):
        monkeypatch.setattr(bs.settings, "llm_model", override)
        return override

    if override and not ollama_has_model(override):
        have = sorted(ollama_model_names())
        if not have:
            pytest.skip(
                f"LLM_MODEL={override!r} is not pulled and no other models exist in Ollama"
            )
        monkeypatch.setattr(bs.settings, "llm_model", have[0])
        return have[0]

    monkeypatch.setattr(bs.settings, "llm_model", "")
    oa.clear_ollama_model_cache()
    try:
        model = oa.resolve_ollama_model(OLLAMA_BASE, None)
    except RuntimeError as e:
        pytest.skip(str(e))
    monkeypatch.setattr(bs.settings, "llm_model", model)
    return model


def pytest_collection_modifyitems(config, items) -> None:
    if ollama_http_reachable(timeout=1.5):
        return
    skip_live = pytest.mark.skip(reason=f"Ollama not reachable at {OLLAMA_BASE}")
    for item in items:
        if item.get_closest_marker("live_ollama") or item.get_closest_marker("agent_e2e_live"):
            item.add_marker(skip_live)
