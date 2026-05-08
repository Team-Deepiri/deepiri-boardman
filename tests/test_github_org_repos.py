from __future__ import annotations

import httpx
import pytest

from boardman.github.org_repos import fetch_org_repository_full_names


@pytest.mark.asyncio
async def test_fetch_org_repos_falls_back_to_users_on_404(monkeypatch):
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_pat", "token")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/orgs/deepiri-org/repos":
            return httpx.Response(404, request=request, text='{"message":"Not Found"}')
        if request.url.path == "/users/deepiri-org/repos":
            return httpx.Response(
                200,
                request=request,
                json=[
                    {"full_name": "deepiri-org/repo-a", "archived": False},
                    {"full_name": "deepiri-org/repo-b", "archived": True},
                ],
            )
        return httpx.Response(500, request=request, text="unexpected")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        repos = await fetch_org_repository_full_names(client, "deepiri-org", skip_archived=True)

    assert repos == ["deepiri-org/repo-a"]


@pytest.mark.asyncio
async def test_fetch_org_repos_raises_non_404_errors(monkeypatch):
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_pat", "token")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request, text='{"message":"server error"}')

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_org_repository_full_names(client, "deepiri-org", skip_archived=True)


@pytest.mark.asyncio
async def test_fetch_org_repos_falls_back_to_discovered_orgs_when_owner_not_found(monkeypatch):
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_pat", "token")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/orgs/deepiri-org/repos":
            return httpx.Response(404, request=request, text='{"message":"Not Found"}')
        if request.url.path == "/users/deepiri-org/repos":
            return httpx.Response(404, request=request, text='{"message":"Not Found"}')
        if request.url.path == "/user/orgs":
            return httpx.Response(200, request=request, json=[{"login": "Team-Deepiri"}])
        if request.url.path == "/orgs/Team-Deepiri/repos":
            return httpx.Response(
                200,
                request=request,
                json=[{"full_name": "Team-Deepiri/deepiri-boardman", "archived": False}],
            )
        return httpx.Response(500, request=request, text="unexpected")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        repos = await fetch_org_repository_full_names(client, "deepiri-org", skip_archived=True)

    assert repos == ["Team-Deepiri/deepiri-boardman"]
