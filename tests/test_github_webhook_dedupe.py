from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from boardman.database.session import init_db
from boardman.main import create_app


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "github"


def _sign(secret: str, payload: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@pytest.mark.asyncio
async def test_duplicate_github_delivery_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    import boardman.settings as bs

    calls = {"count": 0}

    async def _fake_issue_opened(_payload, _session):
        calls["count"] += 1
        return {"ok": True, "message": "handled issue fixture"}

    monkeypatch.setattr(bs.settings, "github_webhook_secret", "test-secret")
    monkeypatch.setattr("boardman.routes.github_events.handle_issue_opened", _fake_issue_opened)

    await init_db()

    raw = (FIXTURES_DIR / "issues_opened.json").read_bytes()
    delivery_id = f"pytest-{uuid4()}"
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": _sign("test-secret", raw),
    }

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/api/v1/webhooks/github", content=raw, headers=headers)
        second = await client.post("/api/v1/webhooks/github", content=raw, headers=headers)

    assert first.status_code == 200
    assert first.json().get("ok") is True
    assert second.status_code == 200
    second_json = second.json()
    assert second_json.get("message") == "Duplicate delivery ignored"
    assert second_json.get("delivery_id") == delivery_id
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_webhook_without_delivery_header_is_processed_each_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import boardman.settings as bs

    calls = {"count": 0}

    async def _fake_issue_opened(_payload, _session):
        calls["count"] += 1
        return {"ok": True}

    monkeypatch.setattr(bs.settings, "github_webhook_secret", "")
    monkeypatch.setattr("boardman.routes.github_events.handle_issue_opened", _fake_issue_opened)

    await init_db()

    raw = (FIXTURES_DIR / "issues_opened.json").read_bytes()
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "issues",
    }

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/api/v1/webhooks/github", content=raw, headers=headers)
        second = await client.post("/api/v1/webhooks/github", content=raw, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls["count"] == 2
