"""Cognition evidence model: serialization, storage overlay, cap eviction."""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.agent.repo_context import (
    load_cognition_state,
    merge_planning_snapshot,
    save_cognition_state,
    save_planning_snapshot,
)
from boardman.cognition.evidence import Evidence, evidence_from_dict, evidence_to_dict
from boardman.database.models import Base, ProjectContext


@pytest.fixture
async def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def test_evidence_round_trip():
    e = Evidence(
        kind="fact",
        subject="test_subject",
        value="test_value",
        source_type="code",
        source_ref="boardman/test.py:42",
        computed_at="2026-08-24T00:00:00Z",
    )
    d = evidence_to_dict(e)
    assert d["kind"] == "fact"
    assert d["source_ref"] == "boardman/test.py:42"
    restored = evidence_from_dict(d)
    assert restored == e


def test_merge_planning_snapshot_preserves_cognition():
    """A partial writer that does not set cognition must not blank it."""
    existing = json.dumps(
        {
            "ok": True,
            "repo": "team/repo",
            "structure": {"language": "Python"},
            "cognition": {"cognition_state": "fresh", "verdicts": [{"x": 1}]},
        }
    )
    incoming = {"ok": True, "repo": "team/repo", "DIRECTION_md": "Ship fast."}
    merged = merge_planning_snapshot(existing, incoming)
    assert merged["cognition"] == {"cognition_state": "fresh", "verdicts": [{"x": 1}]}
    assert merged["DIRECTION_md"] == "Ship fast."


@pytest.mark.asyncio
async def test_save_cognition_does_not_destroy_briefing(db_factory, monkeypatch):
    """save_cognition_state splices cognition without touching the L1 briefing."""
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_repo_context_cache_ttl_seconds", 900.0)

    async with db_factory() as session:
        await save_planning_snapshot(
            session,
            "team/repo",
            {"ok": True, "repo": "team/repo", "DIRECTION_md": "Keep shipping."},
            source_revision="abc123",
        )
        await session.commit()

    async with db_factory() as session:
        await save_cognition_state(
            session,
            "team/repo",
            {
                "cognition_state": "fresh",
                "verdicts": [{"behavior_key": "test", "conclusion": "ALIGNED"}],
            },
        )
        await session.commit()

    async with db_factory() as session:
        row = (
            await session.execute(
                __import__("sqlalchemy")
                .select(ProjectContext)
                .where(ProjectContext.repo == "team/repo")
            )
        ).scalar_one()
        payload = json.loads(row.context_json)
        assert payload["DIRECTION_md"] == "Keep shipping."
        assert payload["cognition"]["cognition_state"] == "fresh"
        assert payload["cognition"]["verdicts"][0]["conclusion"] == "ALIGNED"


@pytest.mark.asyncio
async def test_save_planning_snapshot_does_not_destroy_cognition(db_factory, monkeypatch):
    """save_planning_snapshot merges and preserves existing cognition data."""
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_repo_context_cache_ttl_seconds", 900.0)

    async with db_factory() as session:
        await save_cognition_state(
            session,
            "team/repo",
            {
                "cognition_state": "fresh",
                "verdicts": [{"behavior_key": "x", "conclusion": "BROKEN"}],
            },
        )
        await session.commit()

    async with db_factory() as session:
        await save_planning_snapshot(
            session,
            "team/repo",
            {"ok": True, "repo": "team/repo", "DIRECTION_md": "New direction."},
            source_revision="def456",
        )
        await session.commit()

    async with db_factory() as session:
        cognition = await load_cognition_state(session, "team/repo")
        assert cognition is not None
        assert cognition["cognition_state"] == "fresh"
        assert cognition["verdicts"][0]["conclusion"] == "BROKEN"


@pytest.mark.asyncio
async def test_cognition_cap_eviction(db_factory):
    """Evidence list is capped at the configured maximum."""
    big_evidence = [{"kind": "fact", "subject": f"s{i}"} for i in range(100)]
    async with db_factory() as session:
        await save_cognition_state(
            session,
            "team/repo",
            {
                "cognition_state": "fresh",
                "evidence": big_evidence,
            },
        )
        await session.commit()

    async with db_factory() as session:
        state = await load_cognition_state(session, "team/repo")
        assert state is not None
        assert len(state["evidence"]) <= 50


@pytest.mark.asyncio
async def test_load_cognition_state_returns_none_on_miss(db_factory):
    async with db_factory() as session:
        result = await load_cognition_state(session, "nonexistent/repo")
        assert result is None
