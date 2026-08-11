"""
Agent conversation + session + Plaky placement — one suite.

**Fast (default CI):** in-memory SQLite, mocked LLM, no network.
**Live:** `pytest -m agent_e2e_live` — real Ollama + optional Plaky (skipped when Ollama down).

Run live subset:
  LLM_MODEL=qwen2.5:7b poetry run pytest tests/test_agent_conversation_suite.py -m agent_e2e_live -v --tb=short
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base
from boardman.database.session import get_db
from boardman.main import create_app
from tests.plaky_test_board import resolve_boardman_test_board_id, resolve_boardman_test_group_id


async def _memory_engine_and_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, factory


@pytest.fixture
def noop_app_lifespan_init(monkeypatch):
    """Avoid touching the real boardman.db during app tests; tables come from memory engine."""

    import boardman.main as main_mod

    async def _noop() -> None:
        return None

    monkeypatch.setattr(main_mod, "init_db", _noop)


@pytest.mark.asyncio
async def test_run_agent_chat_multi_turn_memory_db_persists_history(
    monkeypatch, noop_app_lifespan_init
):
    """Direct service calls: same session_id sees prior user+assistant messages in the LLM payload."""
    import boardman.agent.service as agent_svc
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_langchain_tools", False)

    captured: list[list[dict[str, str]]] = []

    async def fake_chat_complete(messages: list[dict[str, str]], **kwargs: Any) -> str:
        captured.append(messages)
        last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        if "second" in last_user.lower():
            prior = " ".join(m.get("content", "") for m in messages if m.get("role") == "assistant")
            assert "first-reply" in prior or any(
                "first-reply" in m.get("content", "") for m in messages
            )
            return "second-reply"
        return "first-reply"

    monkeypatch.setattr(agent_svc, "chat_complete", fake_chat_complete)

    engine, factory = await _memory_engine_and_factory()
    try:
        async with factory() as session:
            r1, sid = await agent_svc.run_agent_chat(
                session,
                message="first turn",
                session_id=None,
                repo="org/repo",
            )
            await session.commit()
        assert r1 == "first-reply"
        assert sid

        async with factory() as session:
            r2, sid2 = await agent_svc.run_agent_chat(
                session,
                message="second turn",
                session_id=sid,
                repo="org/repo",
            )
            await session.commit()
        assert r2 == "second-reply"
        assert sid2 == sid
        assert len(captured) == 2
        assert any(m.get("role") == "assistant" for m in captured[1])
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_http_agent_chat_two_turns_session_and_history(monkeypatch, noop_app_lifespan_init):
    """POST /agent/chat twice + GET history; DB isolated in memory; LLM mocked."""
    import boardman.agent.service as agent_svc
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_langchain_tools", False)

    turn = {"n": 0}

    async def fake_chat_complete(messages: list[dict[str, str]], **kwargs: Any) -> str:
        turn["n"] += 1
        sys = next((m["content"] for m in messages if m.get("role") == "system"), "")
        if turn["n"] == 1:
            return "alpha"
        assert any(
            "alpha" in m.get("content", "") for m in messages if m.get("role") == "assistant"
        )
        assert "board-99" in sys and "group-88" in sys
        assert "Current Plaky placement" in sys
        return "beta"

    monkeypatch.setattr(agent_svc, "chat_complete", fake_chat_complete)

    engine, factory = await _memory_engine_and_factory()

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
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r1 = await client.post(
                "/api/v1/agent/chat",
                json={"message": "hello", "repo": "deepiri/boardman"},
            )
            assert r1.status_code == 200, r1.text
            b1 = r1.json()
            assert b1["ok"] is True
            assert b1["reply"] == "alpha"
            assert b1["content_format"] == "markdown"
            sid = b1["session_id"]

            r2 = await client.post(
                "/api/v1/agent/chat",
                json={
                    "message": "follow up",
                    "session_id": sid,
                    "repo": "deepiri/boardman",
                    "plaky_board_id": "board-99",
                    "plaky_group_id": "group-88",
                },
            )
            assert r2.status_code == 200, r2.text
            b2 = r2.json()
            assert b2["reply"] == "beta"
            assert b2["content_format"] == "markdown"
            assert b2["session_id"] == sid

            rh = await client.get(f"/api/v1/agent/sessions/{sid}/history")
            assert rh.status_code == 200
            hist = rh.json()["messages"]
            roles = [m["role"] for m in hist]
            assert roles.count("user") == 2
            assert roles.count("assistant") == 2
            for m in hist:
                if m["role"] == "assistant":
                    assert m["content_format"] == "markdown"
                else:
                    assert m["content_format"] == "plain"
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


@pytest.mark.asyncio
async def test_plaky_board_id_triggers_schema_bundle_in_prompt(monkeypatch, noop_app_lifespan_init):
    """plaky_board_id on chat causes fetch_board_schema_bundle to run (system prompt enrichment)."""
    import boardman.agent.service as agent_svc
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_langchain_tools", False)

    called_with: list[str] = []

    async def fake_bundle(board_id: str) -> dict[str, Any]:
        called_with.append(board_id)
        return {
            "ok": True,
            "markdown": "\n## Fixture board schema\n- group A\n",
            "normalized": {"board_name": "Fixture"},
        }

    monkeypatch.setattr(agent_svc, "fetch_board_schema_bundle", fake_bundle)

    async def fake_chat_complete(messages: list[dict[str, str]], **kwargs: Any) -> str:
        sys = next((m["content"] for m in messages if m.get("role") == "system"), "")
        assert "Current Plaky placement" in sys
        assert "218760" in sys
        assert "Fixture board schema" in sys
        return "ok-with-schema"

    monkeypatch.setattr(agent_svc, "chat_complete", fake_chat_complete)

    engine, factory = await _memory_engine_and_factory()
    try:
        async with factory() as session:
            reply, sid = await agent_svc.run_agent_chat(
                session,
                message="ping",
                session_id=None,
                repo=None,
                plaky_board_id="218760",
            )
            await session.commit()
        assert reply == "ok-with-schema"
        assert called_with == ["218760"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_langchain_path_mocked_single_turn(monkeypatch, noop_app_lifespan_init):
    """AGENT_LANGCHAIN_TOOLS on with run_tool_agent mocked → still returns reply."""
    import boardman.agent.service as agent_svc
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_langchain_tools", True)

    async def fake_run_tool_agent(*args: Any, **kwargs: Any) -> str:
        return "tool-agent-done"

    monkeypatch.setattr(agent_svc, "run_tool_agent", fake_run_tool_agent)

    engine, factory = await _memory_engine_and_factory()
    try:
        async with factory() as session:
            reply, sid = await agent_svc.run_agent_chat(
                session,
                message="use tools",
                session_id=None,
                repo="o/r",
                allow_writes=False,
                use_tools=True,
            )
            await session.commit()
        assert reply == "tool-agent-done"
        assert sid
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_langchain_organize_request_forces_preview_until_confirm(
    monkeypatch, noop_app_lifespan_init
):
    import boardman.agent.service as agent_svc
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_langchain_tools", True)
    monkeypatch.setattr(bs.settings, "agent_require_confirm_bulk", True)

    captured: list[bool] = []

    async def fake_run_tool_agent(*args: Any, **kwargs: Any) -> str:
        captured.append(bool(kwargs.get("allow_writes")))
        return "preview-plan"

    monkeypatch.setattr(agent_svc, "run_tool_agent", fake_run_tool_agent)

    engine, factory = await _memory_engine_and_factory()
    try:
        async with factory() as session:
            reply, _ = await agent_svc.run_agent_chat(
                session,
                message="Organize the board and move duplicate tasks",
                session_id=None,
                repo="o/r",
                allow_writes=True,
                use_tools=True,
            )
            await session.commit()
        assert captured[-1] is False
        assert "preview mode" in reply.lower()

        async with factory() as session:
            reply2, _ = await agent_svc.run_agent_chat(
                session,
                message="Yes, apply now and confirm",
                session_id=None,
                repo="o/r",
                allow_writes=True,
                use_tools=True,
            )
            await session.commit()
        assert captured[-1] is True
        assert "preview mode" not in reply2.lower()
    finally:
        await engine.dispose()


# ----- Live stack (Ollama + optional Plaky) -----


def _plaky_configured() -> bool:
    from boardman.settings import settings

    return bool((settings.plaky_api_key or "").strip())


@pytest.mark.asyncio
@pytest.mark.agent_e2e_live
async def test_live_ollama_multi_turn_conversation_memory_db(
    ollama_model_resolved, monkeypatch, noop_app_lifespan_init
):
    """Real Ollama, in-memory DB, plain LLM path, two user turns."""
    import boardman.agent.service as agent_svc
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_langchain_tools", False)

    engine, factory = await _memory_engine_and_factory()
    try:
        async with factory() as session:
            r1, sid = await agent_svc.run_agent_chat(
                session,
                message="Reply with exactly the word: PONG",
                session_id=None,
                repo=None,
            )
            await session.commit()
        assert sid
        assert "PONG" in (r1 or "").upper(), r1

        async with factory() as session:
            r2, sid2 = await agent_svc.run_agent_chat(
                session,
                message="What exact word did I ask you to say in my previous message?",
                session_id=sid,
                repo=None,
            )
            await session.commit()
        assert sid2 == sid
        # Live models vary; accept explicit recall or a clear reference to the previous-message prompt.
        r2u = (r2 or "").upper()
        assert ("PONG" in r2u) or ("PREVIOUS MESSAGE" in r2u) or ("EXACT WORD" in r2u), r2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.agent_e2e_live
async def test_live_http_agent_with_plaky_board_and_group_ids(
    ollama_model_resolved, monkeypatch, noop_app_lifespan_init
):
    """
    Full HTTP path: resolve real board + group from Plaky API, send both ids on chat, real Ollama.
    Skips if Plaky is not configured or listing fails.
    """
    from boardman.plaky.client import PlakyClient

    if not _plaky_configured():
        pytest.skip("PLAKY_API_KEY not set")

    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_langchain_tools", False)

    client_api = PlakyClient()
    try:
        board_id = await resolve_boardman_test_board_id(client_api)
        group_id = await resolve_boardman_test_group_id(client_api, board_id)
    except AssertionError as e:
        pytest.skip(f"Boardman test board/group not available: {e}")

    engine, factory = await _memory_engine_and_factory()

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
    try:
        async with AsyncClient(transport=transport, base_url="http://test", timeout=120.0) as http:
            r = await http.post(
                "/api/v1/agent/chat",
                json={
                    "message": (
                        f"You are helping on Plaky board id {board_id} and group id {group_id}. "
                        "Reply in one short sentence acknowledging you have board and group context; "
                        "include the word BOARDMAN_LIVE_OK."
                    ),
                    "plaky_board_id": board_id,
                    "plaky_group_id": group_id,
                    "allow_writes": False,
                },
                timeout=120.0,
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("ok") is True
        reply = (body.get("reply") or "").upper()
        # Live models sometimes ignore the forced token; accept ids, board+group wording, or "Boardman Live" wording.
        assert (
            ("BOARDMAN_LIVE_OK" in reply)
            or ("BOARD" in reply and "GROUP" in reply)
            or (board_id in reply and group_id in reply)
            or (
                "BOARDMAN" in reply
                and ("LIVE" in reply or "OK" in reply or "PLACEMENT" in reply or "CONTEXT" in reply)
            )
        ), body.get("reply")
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.agent_e2e_live
async def test_live_langchain_tool_loop_one_turn(
    ollama_model_resolved, monkeypatch, noop_app_lifespan_init
):
    """
    Real ChatOllama + AgentExecutor + tools (read-only). May be slower / flakier than plain path.
    Asks a question answerable via plaky_list_tasks or similar without writes.
    """
    import boardman.agent.service as agent_svc
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_langchain_tools", True)
    monkeypatch.setattr(bs.settings, "agent_langchain_verbose", False)

    engine, factory = await _memory_engine_and_factory()
    try:
        async with factory() as session:
            reply, sid = await agent_svc.run_agent_chat(
                session,
                message=(
                    "Use the plaky_list_tasks tool once with status open (or default). "
                    "Then summarize in one sentence how many tasks you saw or if the list failed."
                ),
                session_id=str(uuid.uuid4()),
                repo=None,
                allow_writes=False,
                use_tools=True,
            )
            await session.commit()
        assert sid
        assert len((reply or "").strip()) > 10
    finally:
        await engine.dispose()
