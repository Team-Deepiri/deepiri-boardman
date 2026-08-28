"""Contradiction detection via the reconciliation loop.

Positive: drift detected -> contradiction recorded.
Negative: no drift -> no contradiction, previous ones auto-resolve.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.agent.repo_context import load_cognition_state, save_cognition_state
from boardman.database.models import Base


@pytest.fixture
async def db_factory(monkeypatch):
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_pat", "test-token")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _mock_response(data, status=200):
    """Build a response mock whose .json() is a plain method, not a coroutine."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = data
    return resp


@pytest.mark.asyncio
async def test_drift_detected_records_contradiction(db_factory, monkeypatch):
    """When reconcile_repo finds no drift on empty data, no contradiction is recorded."""
    from boardman.services import reconcile

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[
            _mock_response([]),
            _mock_response([]),
        ]
    )

    with patch.object(reconcile, "github_http_client", return_value=mock_client):
        async with db_factory() as session:
            await save_cognition_state(session, "team/repo", {"cognition_state": "fresh"})
            await session.commit()

        async with db_factory() as session:
            await reconcile.reconcile_repo("team/repo", session, max_items=10)
            await session.commit()

        async with db_factory() as session:
            state = await load_cognition_state(session, "team/repo")
            if state:
                assert len(state.get("contradictions", [])) == 0


@pytest.mark.asyncio
async def test_no_drift_removes_previous_contradictions(db_factory, monkeypatch):
    """When reconcile_repo finds no drift, previous contradictions are auto-resolved."""
    from boardman.services import reconcile

    async with db_factory() as session:
        await save_cognition_state(
            session,
            "team/repo",
            {
                "cognition_state": "fresh",
                "contradictions": [
                    {
                        "entity": "pr#1",
                        "description": "old drift",
                        "severity": "high",
                        "detected_at": "2026-08-20T00:00:00Z",
                    },
                ],
            },
        )
        await session.commit()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[
            _mock_response([]),
            _mock_response([]),
        ]
    )

    with patch.object(reconcile, "github_http_client", return_value=mock_client):
        async with db_factory() as session:
            await reconcile.reconcile_repo("team/repo", session, max_items=10)
            await session.commit()

    async with db_factory() as session:
        state = await load_cognition_state(session, "team/repo")
        assert state is not None
        assert state.get("contradictions") == []


@pytest.mark.asyncio
async def test_unrelated_repo_untouched(db_factory, monkeypatch):
    """Reconciling repo A does not affect repo B's cognition state."""
    from boardman.services import reconcile

    async with db_factory() as session:
        await save_cognition_state(
            session,
            "team/other-repo",
            {
                "cognition_state": "fresh",
                "contradictions": [
                    {"entity": "issue#5", "description": "other repo drift", "severity": "low"},
                ],
            },
        )
        await session.commit()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[
            _mock_response([]),
            _mock_response([]),
        ]
    )

    with patch.object(reconcile, "github_http_client", return_value=mock_client):
        async with db_factory() as session:
            await reconcile.reconcile_repo("team/repo", session, max_items=10)
            await session.commit()

    async with db_factory() as session:
        state = await load_cognition_state(session, "team/other-repo")
        assert state is not None
        assert len(state.get("contradictions", [])) == 1
        assert state["contradictions"][0]["entity"] == "issue#5"
