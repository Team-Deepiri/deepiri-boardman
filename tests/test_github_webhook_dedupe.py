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


@pytest.mark.asyncio
async def test_webhook_fixture_dispatch_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    import boardman.settings as bs

    calls = {
        "issues": 0,
        "pull_request": 0,
        "pull_request_review": 0,
        "issue_comment": 0,
    }

    async def _fake_issue_opened(_payload, _session):
        calls["issues"] += 1
        return {"ok": True}

    async def _fake_pr_opened(_payload, _session):
        calls["pull_request"] += 1
        return {"ok": True}

    async def _fake_pr_review(_payload, _session):
        calls["pull_request_review"] += 1
        return {"ok": True}

    async def _fake_issue_comment(_payload, _session):
        calls["issue_comment"] += 1
        return {"ok": True}

    monkeypatch.setattr(bs.settings, "github_webhook_secret", "fixture-secret")
    monkeypatch.setattr("boardman.routes.github_events.handle_issue_opened", _fake_issue_opened)
    monkeypatch.setattr("boardman.routes.github_events.handle_pr_opened", _fake_pr_opened)
    monkeypatch.setattr("boardman.routes.github_events.handle_pull_request_review", _fake_pr_review)
    monkeypatch.setattr("boardman.routes.github_events.handle_issue_comment_on_pr", _fake_issue_comment)

    await init_db()
    app = create_app()

    cases = [
        ("issues", "issues_opened.json"),
        ("pull_request", "pull_request_opened.json"),
        ("pull_request_review", "pull_request_review_submitted.json"),
        ("issue_comment", "issue_comment_created.json"),
        ("ping", "ping.json"),
    ]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for event_type, fixture_name in cases:
            raw = (FIXTURES_DIR / fixture_name).read_bytes()
            headers = {
                "Content-Type": "application/json",
                "X-GitHub-Event": event_type,
                "X-GitHub-Delivery": f"dispatch-{event_type}-{uuid4()}",
                "X-Hub-Signature-256": _sign("fixture-secret", raw),
            }
            resp = await client.post("/api/v1/webhooks/github", content=raw, headers=headers)
            assert resp.status_code == 200
            if event_type == "ping":
                assert resp.json().get("message") == "pong"
            else:
                assert resp.json().get("ok") is True

    assert calls == {
        "issues": 1,
        "pull_request": 1,
        "pull_request_review": 1,
        "issue_comment": 1,
    }
