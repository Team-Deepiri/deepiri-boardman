import pytest
from httpx import ASGITransport, AsyncClient

from boardman.main import create_app


@pytest.mark.asyncio
async def test_health():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/v1/health")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_openapi_has_agent_routes():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/openapi.json")
        assert r.status_code == 200
        paths = r.json().get("paths") or {}
        assert any("agent" in p for p in paths)


@pytest.mark.asyncio
async def test_openapi_has_plaky_board_match():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/openapi.json")
        assert r.status_code == 200
        paths = r.json().get("paths") or {}
        assert "/api/v1/plaky/boards/match" in paths
        assert "/api/v1/plaky/boards/{board_id}/schema" in paths


@pytest.mark.asyncio
async def test_plaky_boards_match_request_ok_without_key():
    """HTTP shape: endpoint responds; Plaky list may fail but route is wired."""
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/v1/plaky/boards/match", params={"query": "test"})
        assert r.status_code == 200
        body = r.json()
        assert "ok" in body
        assert "matches" in body
        assert "boards" in body


@pytest.mark.asyncio
async def test_plaky_board_schema_route_without_key():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/v1/plaky/boards/test-board-id/schema")
        assert r.status_code == 200
        body = r.json()
        assert "ok" in body
        assert "normalized" in body
        assert "markdown" in body


@pytest.mark.asyncio
async def test_assignment_sync_field_keys_route(monkeypatch):
    app = create_app()

    async def _fake_sync(_board_id: str):
        return {"ok": True, "updated": {"repo": "repo_key"}, "path": "/tmp/ta.yml"}

    monkeypatch.setattr("boardman.routes.assignment.sync_team_assignment_field_keys_from_board", _fake_sync)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/assignment/sync-field-keys", params={"board_id": "board-123"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["updated"].get("repo") == "repo_key"
        assert body["board_id"] == "board-123"


def test_import_tools():
    from boardman.agent.tools import build_all_tools

    assert len(build_all_tools(allow_writes=False)) == 17
    assert len(build_all_tools(allow_writes=True)) == 23
