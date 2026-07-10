import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.models import GitHubWebhookDelivery
from boardman.database.session import get_db
from boardman.github.webhooks import (
    IssueCommentEventPayload,
    IssueEventPayload,
    PullRequestEventPayload,
    PullRequestReviewCommentEventPayload,
    PullRequestReviewEventPayload,
    parse_webhook_payload,
    verify_signature,
)
from boardman.services.issue_handler import (
    handle_issue_closed,
    handle_issue_opened,
    handle_issue_reopened,
)
from boardman.services.pr_handler import (
    handle_pr_closed_without_merge,
    handle_pr_converted_to_draft,
    handle_pr_edited,
    handle_pr_merged,
    handle_pr_opened,
    handle_pr_ready_for_review,
    handle_pr_review_comment,
    handle_pr_review_requested,
    handle_pr_synchronized,
)
from boardman.services.pr_review_handler import handle_issue_comment_on_pr, handle_pull_request_review
from boardman.settings import settings


router = APIRouter()


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> Response:
    raw_body = await request.body()
    event_type = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "").strip()

    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(raw_body, signature, settings.github_webhook_secret):
        body = json.dumps({"ok": False, "message": "Invalid signature"})
        return Response(content=body, status_code=401)

    async def _mark_delivery(status: str, note: str) -> None:
        if not delivery_id:
            return
        row = await session.get(GitHubWebhookDelivery, delivery_id)
        if row:
            row.status = status
            row.note = note

    if delivery_id:
        already = (
            await session.execute(
                select(GitHubWebhookDelivery).where(
                    GitHubWebhookDelivery.delivery_id == delivery_id
                )
            )
        ).scalar_one_or_none()
        if already and already.status == "processed":
            body = json.dumps(
                {
                    "ok": True,
                    "message": "Duplicate delivery ignored",
                    "delivery_id": delivery_id,
                    "event_type": already.event_type,
                }
            )
            return Response(content=body)
        if not already:
            session.add(
                GitHubWebhookDelivery(
                    delivery_id=delivery_id,
                    event_type=event_type or "unknown",
                    status="processing",
                )
            )
            await session.flush()

    try:
        payload_dict = json.loads(raw_body.decode("utf-8"))
    except Exception:
        await _mark_delivery("processed", "invalid_json")
        return Response(content=json.dumps({"ok": False, "message": "Invalid JSON"}), status_code=400)

    if event_type == "ping":
        await _mark_delivery("processed", "pong")
        return Response(content=json.dumps({"ok": True, "message": "pong"}))

    payload = parse_webhook_payload(event_type, payload_dict)
    if not payload:
        await _mark_delivery("processed", "unsupported_event")
        body = json.dumps({"ok": False, "message": "Unsupported event type"})
        return Response(content=body, status_code=400)

    result: Optional[dict[str, Any]] = None

    if isinstance(payload, IssueEventPayload):
        if payload.action == "opened":
            result = await handle_issue_opened(payload, session)
        elif payload.action == "closed":
            result = await handle_issue_closed(payload, session)
        elif payload.action == "reopened":
            result = await handle_issue_reopened(payload, session)

    elif isinstance(payload, PullRequestReviewEventPayload):
        result = await handle_pull_request_review(payload, session)

    elif isinstance(payload, PullRequestReviewCommentEventPayload):
        if payload.action == "created":
            result = await handle_pr_review_comment(payload, session)

    elif isinstance(payload, IssueCommentEventPayload):
        result = await handle_issue_comment_on_pr(payload, session)

    elif isinstance(payload, PullRequestEventPayload):
        if payload.action == "opened":
            result = await handle_pr_opened(payload, session)
        elif payload.action == "ready_for_review":
            result = await handle_pr_ready_for_review(payload, session)
        elif payload.action == "review_requested":
            result = await handle_pr_review_requested(payload, session)
        elif payload.action == "closed" and payload.pull_request.merged:
            result = await handle_pr_merged(payload, session)
        elif payload.action == "closed" and not payload.pull_request.merged:
            result = await handle_pr_closed_without_merge(payload, session)
        elif payload.action == "synchronize":
            result = await handle_pr_synchronized(payload, session)
        elif payload.action == "reopened":
            result = await handle_pr_opened(payload, session)
        elif payload.action == "edited":
            result = await handle_pr_edited(payload, session)
        elif payload.action == "converted_to_draft":
            result = await handle_pr_converted_to_draft(payload, session)

    if result is not None:
        await _mark_delivery("processed", "handled")
        return Response(content=json.dumps(result))

    await _mark_delivery("processed", "ignored")
    return Response(content=json.dumps({"ok": True, "message": "Event ignored"}))
