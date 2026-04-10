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


def test_import_tools():
    from boardman.agent.tools import build_all_tools

    assert len(build_all_tools(allow_writes=False)) == 4
    assert len(build_all_tools(allow_writes=True)) == 8
