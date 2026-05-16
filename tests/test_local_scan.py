"""Local directory scan → Plaky task suggestions (no GitHub API for content)."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.agent.tools.repo_tools import _scan_local_repo
from boardman.database.models import Base
from boardman.database.session import get_db
from boardman.main import create_app
from boardman.services.local_scan_context import gather_local_scan_context
from boardman.services.scan_handler import run_local_path_scan


async def _memory_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def test_gather_local_scan_context_reads_direction(tmp_path) -> None:
    (tmp_path / "DIRECTION.md").write_text("# Goals\n\nShip the widget.", encoding="utf-8")
    bundle = gather_local_scan_context(str(tmp_path))
    assert bundle.get("ok") is True
    assert "Ship the widget" in (bundle.get("direction_md") or "")
    assert bundle.get("project_name") == tmp_path.name


def test_scan_local_repo_tool_uses_shared_gather(tmp_path) -> None:
    (tmp_path / "DIRECTION.md").write_text("# Tool goal\n\nShip it.", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text("# Note", encoding="utf-8")
    raw = _scan_local_repo(str(tmp_path), max_files=20)
    data = json.loads(raw)
    assert data.get("ok") is True
    assert "Ship it" in (data.get("direction_md") or "")
    assert "Tool goal" in (data.get("direction_md") or "")
    paths = {f["path"] for f in (data.get("files") or [])}
    assert "docs/note.md" in paths
    assert data.get("root") == str(tmp_path.resolve())


def test_gather_local_scan_context_skips_root_readme_in_walk(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Root readme", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "extra.md").write_text("# Extra", encoding="utf-8")
    bundle = gather_local_scan_context(str(tmp_path))
    assert bundle.get("ok") is True
    paths = {d["path"] for d in (bundle.get("doc_excerpts") or [])}
    assert "README.md" not in paths
    assert "docs/extra.md" in paths
    assert "Root readme" in (bundle.get("readme_excerpt") or "")


@pytest.mark.asyncio
async def test_run_local_path_scan_passes_name_queries_to_resolver(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "DIRECTION.md").write_text("# A", encoding="utf-8")
    captured: dict = {}

    async def fake_resolve_scan_placement(**kw: Any) -> tuple[str, str, dict]:
        captured.update(kw)
        return "b-res", "g-res", {}

    async def fake_chat(*args, **kwargs):
        return "[]"

    async def fake_titles(_board_id: str) -> str:
        return ""

    monkeypatch.setattr(
        "boardman.services.scan_handler.resolve_scan_placement",
        fake_resolve_scan_placement,
    )
    monkeypatch.setattr("boardman.services.scan_handler.chat_complete", fake_chat)
    monkeypatch.setattr("boardman.services.scan_handler._plaky_titles_on_board", fake_titles)

    _, factory = await _memory_session_factory()
    async with factory() as session:
        await run_local_path_scan(
            session,
            str(tmp_path),
            dry_run=True,
            provider="openai",
            model="gpt-4o-mini",
            plaky_board_query="  Engineering Board ",
            plaky_group_query=" Backlog ",
        )
    assert captured.get("board_name_query") == "Engineering Board"
    assert captured.get("group_name_query") == "Backlog"
    assert captured.get("board_id") == ""
    assert captured.get("group_id") == ""


@pytest.mark.asyncio
async def test_run_local_path_scan_requires_placement(tmp_path) -> None:
    (tmp_path / "DIRECTION.md").write_text("# X", encoding="utf-8")
    _, factory = await _memory_session_factory()
    async with factory() as session:
        result = await run_local_path_scan(
            session,
            str(tmp_path),
            dry_run=True,
            provider="openai",
            model="gpt-4o-mini",
        )
    assert result.get("ok") is False
    assert "Plaky placement" in (result.get("message") or "")


@pytest.mark.asyncio
async def test_run_local_path_scan_dry_run_parses_tasks(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "DIRECTION.md").write_text("# Build\n\nLocal-only project.", encoding="utf-8")

    async def fake_chat(*args, **kwargs):
        return '[{"title": "First task", "description": "Desc", "priority": "medium"}]'

    async def fake_titles(_board_id: str) -> str:
        return "(no hints)"

    monkeypatch.setattr("boardman.services.scan_handler.chat_complete", fake_chat)
    monkeypatch.setattr("boardman.services.scan_handler._plaky_titles_on_board", fake_titles)

    _, factory = await _memory_session_factory()
    async with factory() as session:
        result = await run_local_path_scan(
            session,
            str(tmp_path),
            dry_run=True,
            provider="openai",
            model="gpt-4o-mini",
            plaky_board_id="board-1",
            plaky_group_id="group-1",
        )
        await session.commit()

    assert result.get("ok") is True
    assert result.get("tasks_parsed") == 1
    assert result.get("tasks_created") == 0
    assert result.get("scan_mode") == "local_path"
    preview = result.get("preview") or []
    assert len(preview) == 1
    assert preview[0].get("title") == "First task"


@pytest.mark.asyncio
async def test_agent_scan_local_http_ok(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from httpx import ASGITransport, AsyncClient

    (tmp_path / "DIRECTION.md").write_text("# HTTP scan", encoding="utf-8")

    async def fake_chat(*args, **kwargs):
        return "[]"

    async def fake_titles(_board_id: str) -> str:
        return "(no hints)"

    monkeypatch.setattr("boardman.services.scan_handler.chat_complete", fake_chat)
    monkeypatch.setattr("boardman.services.scan_handler._plaky_titles_on_board", fake_titles)

    engine, factory = await _memory_session_factory()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/v1/agent/scan-local",
            json={
                "path": str(tmp_path),
                "dry_run": True,
                "plaky_board_id": "b",
                "plaky_group_id": "g",
                "provider": "openai",
                "model": "gpt-4o-mini",
            },
        )
    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is True
    assert data.get("scan_mode") == "local_path"
    await engine.dispose()
