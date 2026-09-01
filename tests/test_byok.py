"""Bring-your-own-key: encrypted, time-limited, per-session LLM key override.
See boardman/security/byok.py and boardman/agent/service.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import AgentSession, Base
from boardman.security import byok


async def _memory_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def test_disabled_when_no_encryption_key(monkeypatch):
    monkeypatch.setattr(byok.settings, "byok_encryption_key", "")
    assert byok.is_configured() is False
    with pytest.raises(RuntimeError):
        byok.encrypt_key("openai", "sk-whatever")


def test_encrypt_decrypt_round_trip(monkeypatch):
    monkeypatch.setattr(byok.settings, "byok_encryption_key", "test-secret-123")
    ct = byok.encrypt_key("openai", "sk-my-real-key")
    assert "sk-my-real-key" not in ct  # never stored in plaintext
    assert byok.decrypt_key(ct) == "sk-my-real-key"


def test_decrypt_fails_closed_after_secret_rotation(monkeypatch):
    monkeypatch.setattr(byok.settings, "byok_encryption_key", "secret-a")
    ct = byok.encrypt_key("openai", "sk-my-real-key")
    monkeypatch.setattr(byok.settings, "byok_encryption_key", "secret-b")
    assert byok.decrypt_key(ct) is None  # treated as "no key", never raises


def test_normalize_provider_rejects_unknown():
    assert byok.normalize_provider("openai") == "openai"
    assert byok.normalize_provider("OpenRouter") == "openrouter"
    assert byok.normalize_provider("not-a-provider") is None


def test_is_expired():
    assert byok.is_expired(None) is True
    assert byok.is_expired(datetime.now(UTC) - timedelta(hours=1)) is True
    assert byok.is_expired(datetime.now(UTC) + timedelta(hours=1)) is False


@pytest.mark.asyncio
async def test_set_get_clear_session_byok_key(monkeypatch):
    monkeypatch.setattr(byok.settings, "byok_encryption_key", "test-secret-123")
    from boardman.agent.service import (
        clear_session_byok_key,
        get_session_byok_status,
        set_session_byok_key,
    )

    engine, factory = await _memory_session_factory()
    async with factory() as session:
        result = await set_session_byok_key(
            session, "sess-1", provider="openrouter", api_key="sk-or-real-key"
        )
        assert result["ok"] is True
        assert result["provider"] == "openrouter"
        assert "sk-or-real-key" not in str(result)  # never echoed back

    async with factory() as session:
        status = await get_session_byok_status(session, "sess-1")
        assert status["configured"] is True
        assert status["provider"] == "openrouter"
        assert "api_key" not in status and "sk-or-real-key" not in str(status)

    async with factory() as session:
        row = (
            await session.execute(
                __import__("sqlalchemy")
                .select(AgentSession)
                .where(AgentSession.session_id == "sess-1")
            )
        ).scalar_one()
        from boardman.agent.service import _resolve_session_llm_override

        provider, key = await _resolve_session_llm_override(row)
        assert provider == "openrouter"
        assert key == "sk-or-real-key"

    async with factory() as session:
        cleared = await clear_session_byok_key(session, "sess-1")
        assert cleared is True

    async with factory() as session:
        status = await get_session_byok_status(session, "sess-1")
        assert status["configured"] is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_set_byok_key_refused_when_not_configured(monkeypatch):
    monkeypatch.setattr(byok.settings, "byok_encryption_key", "")
    from boardman.agent.service import set_session_byok_key

    engine, factory = await _memory_session_factory()
    async with factory() as session:
        result = await set_session_byok_key(
            session, "sess-2", provider="openai", api_key="sk-whatever"
        )
        assert result["ok"] is False
    await engine.dispose()
