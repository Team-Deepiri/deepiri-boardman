from __future__ import annotations

import asyncio

import httpx
import pytest

from boardman.github.repo_hotspots import fetch_repo_hotspots
from boardman.github.repo_metadata import fetch_repo_metadata


@pytest.mark.asyncio
async def test_metadata_and_hotspots_share_identity_and_tree_fetches(monkeypatch) -> None:
    import boardman.github.repo_metadata as metadata
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_pat", "token")
    calls: list[str] = []

    async def fake_request(_client, path: str) -> httpx.Response:
        calls.append(path)
        if path == "/repos/o/r":
            return httpx.Response(
                200,
                json={
                    "full_name": "o/r",
                    "default_branch": "main",
                    "language": "Python",
                    "pushed_at": "2026-08-18T00:00:00Z",
                },
            )
        if path == "/repos/o/r/git/trees/main?recursive=1":
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"path": "boardman/main.py", "type": "blob", "size": 1000},
                        {"path": "tests/test_main.py", "type": "blob", "size": 500},
                    ]
                },
            )
        return httpx.Response(404, json={})

    monkeypatch.setattr(metadata, "github_request", fake_request)
    client = object()
    meta, hotspots = await asyncio.gather(
        fetch_repo_metadata(client, "o", "r"),
        fetch_repo_hotspots(client, "o", "r"),
    )

    assert meta is not None
    assert hotspots is not None
    assert calls.count("/repos/o/r") == 1
    assert calls.count("/repos/o/r/git/trees/main?recursive=1") == 1
