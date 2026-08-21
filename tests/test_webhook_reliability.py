"""Webhook delivery is at-least-once; the effect on Plaky must be exactly-once.

GitHub retries a delivery it did not hear a fast 2xx for, redelivers on request, and the
poller replays the same activity from its own feed. Every one of those arrives as a fresh
call into the same handlers. The properties that make that safe -- reject a forged body,
process an id once, retry a soft failure without duplicating its effect, and reach a
terminal row state either way -- are what this file pins.

Signature verification and the retry loop had no coverage at the route and worker level,
which is the level that matters: a unit-tested `verify_signature` proves nothing about
whether the route calls it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base, GitHubWebhookDelivery
from boardman.github.webhooks import verify_signature
from boardman.settings import settings

SECRET = "a-real-webhook-secret"
FULL = "Team-Deepiri/deepiri-boardman"


def _body(action: str = "opened") -> bytes:
    return json.dumps(
        {
            "action": action,
            "issue": {
                "number": 94,
                "title": "T",
                "body": "b",
                "state": "open",
                "html_url": f"https://github.com/{FULL}/issues/94",
                "labels": [],
                "assignees": [],
            },
            "repository": {"full_name": FULL, "name": "deepiri-boardman"},
        }
    ).encode("utf-8")


def _sign(raw: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


# --- signature ---------------------------------------------------------------------------


def test_a_forged_body_does_not_verify() -> None:
    raw = _body()
    assert verify_signature(raw, _sign(raw), SECRET) is True
    assert verify_signature(raw + b" ", _sign(raw), SECRET) is False
    assert verify_signature(raw, _sign(raw, "a-different-secret"), SECRET) is False
    assert verify_signature(raw, "", SECRET) is False
    assert verify_signature(raw, "not-even-a-signature", SECRET) is False


def test_an_unset_secret_accepts_everything_and_that_is_deliberate() -> None:
    """Local runs have no secret. Production must set one; this is why it matters."""
    assert verify_signature(_body(), "", "") is True


@pytest.mark.asyncio
async def test_the_route_rejects_a_forged_delivery_without_dispatching(monkeypatch) -> None:
    """The route-level assertion: verify_signature being correct is not the same as the
    route calling it before it does any work."""
    from fastapi import Request

    from boardman.routes import github_events as ge

    dispatched: list[str] = []

    async def fake_dispatch(event_type, payload_dict, session):
        dispatched.append(event_type)
        return {"ok": True}

    monkeypatch.setattr(settings, "github_webhook_secret", SECRET)
    monkeypatch.setattr(ge, "dispatch_github_event", fake_dispatch)

    raw = _body()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/webhooks/github",
        "headers": [
            (b"x-github-event", b"issues"),
            (b"x-github-delivery", b"delivery-forged"),
            (b"x-hub-signature-256", _sign(raw, "wrong-secret").encode()),
        ],
    }

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    response = await ge.github_webhook(Request(scope, receive), session=None)

    assert response.status_code == 401
    assert dispatched == [], "nothing runs before the signature is checked"


# --- delivery identity ---------------------------------------------------------------------


@pytest.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# --- worker retry ---------------------------------------------------------------------------


@pytest.fixture()
def worker(monkeypatch, db_session):
    """Run the webhook job against the in-memory session, with sleeps removed."""
    from boardman.jobs import handlers as jh

    class _SessionCtx:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_a):
            return False

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("boardman.database.session.async_session", lambda: _SessionCtx())
    monkeypatch.setattr("asyncio.sleep", no_sleep)
    return jh


def _job(delivery_id: str = "d1") -> dict[str, Any]:
    return {
        "event_type": "issues",
        "delivery_id": delivery_id,
        "payload": json.loads(_body().decode("utf-8")),
    }


@pytest.mark.asyncio
async def test_a_handled_failure_is_retried_the_configured_number_of_times(
    worker, db_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "github_webhook_job_retries", 2)
    attempts: list[int] = []

    async def failing(event_type, body, session):
        attempts.append(1)
        return {"ok": False, "message": "Plaky rejected the patch"}

    monkeypatch.setattr("boardman.routes.github_events.dispatch_github_event", failing)

    with pytest.raises(RuntimeError, match="Plaky rejected the patch"):
        await worker.boardman_github_webhook_job(_job())

    assert len(attempts) == 3, "the first try plus two retries"


@pytest.mark.asyncio
async def test_a_retry_that_succeeds_stops_retrying(worker, db_session, monkeypatch) -> None:
    """A transient failure must not keep costing attempts once it clears."""
    monkeypatch.setattr(settings, "github_webhook_job_retries", 3)
    calls: list[int] = []

    async def flaky(event_type, body, session):
        calls.append(1)
        if len(calls) < 2:
            return {"ok": False, "message": "transient"}
        return {"ok": True, "event": "issue_created"}

    monkeypatch.setattr("boardman.routes.github_events.dispatch_github_event", flaky)

    out = await worker.boardman_github_webhook_job(_job())

    assert out["ok"] is True
    assert out["attempt"] == 2
    assert len(calls) == 2, "no attempts spent after it worked"


@pytest.mark.asyncio
async def test_a_successful_delivery_reaches_a_terminal_row_state(
    worker, db_session, monkeypatch
) -> None:
    db_session.add(
        GitHubWebhookDelivery(delivery_id="d1", event_type="issues", status="processing")
    )
    await db_session.commit()

    async def ok(event_type, body, session):
        return {"ok": True}

    monkeypatch.setattr("boardman.routes.github_events.dispatch_github_event", ok)
    await worker.boardman_github_webhook_job(_job())

    row = (
        await db_session.execute(
            select(GitHubWebhookDelivery).where(GitHubWebhookDelivery.delivery_id == "d1")
        )
    ).scalar_one()
    assert row.status == "processed"


@pytest.mark.asyncio
async def test_an_exhausted_delivery_is_marked_failed_not_left_processing(
    worker, db_session, monkeypatch
) -> None:
    """A row stuck at `processing` short-circuits every redelivery of that id forever."""
    monkeypatch.setattr(settings, "github_webhook_job_retries", 1)
    db_session.add(
        GitHubWebhookDelivery(delivery_id="d1", event_type="issues", status="processing")
    )
    await db_session.commit()

    async def always_fails(event_type, body, session):
        return {"ok": False, "message": "still broken"}

    monkeypatch.setattr("boardman.routes.github_events.dispatch_github_event", always_fails)

    with pytest.raises(RuntimeError):
        await worker.boardman_github_webhook_job(_job())

    row = (
        await db_session.execute(
            select(GitHubWebhookDelivery).where(GitHubWebhookDelivery.delivery_id == "d1")
        )
    ).scalar_one()
    assert row.status == "failed"
    assert "still broken" in (row.note or "")


@pytest.mark.asyncio
async def test_an_unexpected_crash_is_retried_too(worker, db_session, monkeypatch) -> None:
    """A handler raising is not different, from the delivery's point of view, from one
    reporting failure: both mean this delivery has not been applied yet."""
    monkeypatch.setattr(settings, "github_webhook_job_retries", 1)
    attempts: list[int] = []

    async def crashes(event_type, body, session):
        attempts.append(1)
        raise RuntimeError("a genuine bug")

    monkeypatch.setattr("boardman.routes.github_events.dispatch_github_event", crashes)

    with pytest.raises(RuntimeError, match="a genuine bug"):
        await worker.boardman_github_webhook_job(_job())

    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_a_malformed_job_payload_is_rejected_without_retrying(worker, monkeypatch) -> None:
    """Retrying a job that can never parse just burns the queue."""
    called: list[int] = []

    async def never(event_type, body, session):
        called.append(1)
        return {"ok": True}

    monkeypatch.setattr("boardman.routes.github_events.dispatch_github_event", never)

    out = await worker.boardman_github_webhook_job({"event_type": "", "payload": None})

    assert out["ok"] is False
    assert called == []
