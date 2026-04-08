import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.session import async_session
from boardman.github.webhooks import IssueEventPayload, PullRequestEventPayload, PingEventPayload, parse_webhook_payload, verify_signature
from boardman.services.issue_handler import handle_issue_opened
from boardman.services.pr_handler import handle_pr_opened, handle_pr_merged
from boardman.settings import settings


router = APIRouter()


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    session: AsyncSession = Depends(async_session),
) -> Response:
    raw_body = await request.body()

    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(raw_body, signature, settings.github_webhook_secret):
        return Response(content=json.dumps({"ok": False, "message": "Invalid signature"}), status_code=401)

    event_type = request.headers.get("X-GitHub-Event", "")
    try:
        payload_dict = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return Response(content=json.dumps({"ok": False, "message": "Invalid JSON"}), status_code=400)

    if event_type == "ping":
        return Response(content=json.dumps({"ok": True, "message": "pong"}))

    payload = parse_webhook_payload(event_type, payload_dict)
    if not payload:
        return Response(content=json.dumps({"ok": False, "message": "Unsupported event type"}), status_code=400)

    if isinstance(payload, IssueEventPayload) and payload.action == "opened":
        result = await handle_issue_opened(payload, session)
        return Response(content=json.dumps(result))

    if isinstance(payload, PullRequestEventPayload):
        if payload.action == "opened":
            result = await handle_pr_opened(payload, session)
            return Response(content=json.dumps(result))
        elif payload.action == "closed" and payload.pull_request.merged:
            result = await handle_pr_merged(payload, session)
            return Response(content=json.dumps(result))

    return Response(content=json.dumps({"ok": True, "message": "Event ignored"}))