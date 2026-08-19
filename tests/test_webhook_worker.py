from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from types import SimpleNamespace
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
async def test_async_webhook_marks_delivery_and_enqueues_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import boardman.settings as bs

    queued: list[tuple[str, dict]] = []

    class FakeQueue:
        async def enqueue_job(self, kind: str, payload: dict) -> SimpleNamespace:
            queued.append((kind, payload))
            return SimpleNamespace(job_id="job-async-webhook")

    calls = {"opened": 0}

    async def fake_opened(_payload, _session):
        calls["opened"] += 1
        return {"ok": True}

    monkeypatch.setattr(bs.settings, "github_webhook_secret", "async-webhook-secret")
    monkeypatch.setattr(bs.settings, "github_webhook_async_enabled", True)
    monkeypatch.setattr("boardman.routes.github_events.handle_issue_opened", fake_opened)
    monkeypatch.setattr("boardman.broker.job_queue.get_job_queue", lambda: FakeQueue())

    await init_db()
    raw = (FIXTURES_DIR / "issues_opened.json").read_bytes()
    delivery_id = f"async-{uuid4()}"
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": _sign("async-webhook-secret", raw),
    }

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/webhooks/github", content=raw, headers=headers)

    assert response.status_code == 202
    assert response.json()["job_id"] == "job-async-webhook"
    assert calls["opened"] == 0
    assert queued and queued[0][0] == "boardman_github_webhook_job"
    assert queued[0][1]["delivery_id"] == delivery_id


@pytest.mark.asyncio
async def test_worker_marks_false_handler_result_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    import boardman.sqlite_worker as worker

    finished: list[dict] = []

    async def fake_handler(_payload: dict) -> dict:
        return {"ok": False, "message": "temporary synchronization failure"}

    async def fake_mark(job_id: str, *, success: bool, status: str, result: dict) -> None:
        finished.append(
            {"job_id": job_id, "success": success, "status": status, "result": result}
        )

    monkeypatch.setitem(worker.JOB_HANDLERS, "test_false_result", fake_handler)
    monkeypatch.setattr(worker, "mark_job_finished", fake_mark)

    await worker._run_one("job-1", "test_false_result", {})

    assert finished == [
        {
            "job_id": "job-1",
            "success": False,
            "status": "incomplete",
            "result": {"ok": False, "message": "temporary synchronization failure"},
        }
    ]

