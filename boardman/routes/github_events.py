"""GitHub webhook receiver and event dispatch."""

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.models import GitHubWebhookDelivery
from boardman.database.session import get_db
from boardman.github.change_signal import note_repo_changed, repo_full_name_from_payload
from boardman.github.webhooks import (
    DeploymentStatusEventPayload,
    IssueCommentEventPayload,
    IssueEventPayload,
    PullRequestEventPayload,
    PullRequestReviewCommentEventPayload,
    PullRequestReviewEventPayload,
    parse_webhook_payload,
    verify_signature_any,
)
from boardman.observability.degradation import log_degraded
from boardman.services.issue_handler import (
    handle_issue_closed,
    handle_issue_edited,
    handle_issue_labels_changed,
    handle_issue_opened,
    handle_issue_reopened,
)
from boardman.services.pr_handler import (
    handle_deployment_status,
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
from boardman.services.pr_review_handler import (
    handle_issue_comment_on_pr,
    handle_pull_request_review,
)
from boardman.settings import settings

_log = logging.getLogger(__name__)

# How long a delivery may sit in "processing" before it is treated as abandoned. Long
# enough that a slow handler is never stolen from itself, short enough that GitHub's own
# redelivery (which is the only recovery there is) still finds the door open.
_PROCESSING_STALE_SECONDS = 900
# Event types with no payload model, which still have cache work to do. See the dispatch.
_EVENTS_WITHOUT_A_PAYLOAD_MODEL = ("repository", "create", "delete", "push")

router = APIRouter()


async def dispatch_github_event(
    event_type: str,
    payload_dict: dict[str, Any],
    session: AsyncSession,
) -> dict[str, Any]:
    """Dispatch a parsed GitHub event for both HTTP and worker execution paths."""
    # A repo appearing, disappearing or being renamed changes the org listing, which is
    # cached for ten minutes and has no other way to learn about it — a new repo was
    # invisible to the assistant until the TTL happened to lapse. No handler owns this
    # event; the only correct response is to forget what we listed.
    if event_type in ("repository", "create", "delete"):
        from boardman.github.org_repos import clear_org_repos_cache

        clear_org_repos_cache()
        full_name = repo_full_name_from_payload(payload_dict)
        note_repo_changed(full_name, event=event_type)
        return {
            "ok": True,
            "message": "org repository listing invalidated",
            "event": event_type,
            "repo": full_name,
        }

    # A push is the one event that changes the CODE, and it had no branch here: it fell
    # through to the payload-model gate, which has no model for it, so file trees,
    # hotspots, structure and defect scans cached for that repo went on serving pre-push
    # content for the whole TTL while the tools reported it as current. The poller path
    # always invalidated; the webhook path -- the production one -- did not.
    if event_type == "push":
        full_name = repo_full_name_from_payload(payload_dict)
        note_repo_changed(full_name, event=event_type)
        return {
            "ok": True,
            "message": "repo read cache invalidated",
            "event": event_type,
            "repo": full_name,
        }

    payload = parse_webhook_payload(event_type, payload_dict)
    if not payload:
        return {"ok": False, "message": "Unsupported event type"}

    result: dict[str, Any] | None = None
    if isinstance(payload, IssueEventPayload):
        if payload.action == "opened":
            result = await handle_issue_opened(payload, session)
        elif payload.action == "closed":
            result = await handle_issue_closed(payload, session)
        elif payload.action == "reopened":
            result = await handle_issue_reopened(payload, session)
        elif payload.action in (
            "edited",
            "labeled",
            "unlabeled",
            "assigned",
            "unassigned",
            "typed",
            "untyped",
            "milestoned",
            "demilestoned",
        ):
            if payload.action == "edited":
                result = await handle_issue_edited(payload, session)
            else:
                result = await handle_issue_labels_changed(payload, session)

    elif isinstance(payload, PullRequestReviewEventPayload):
        result = await handle_pull_request_review(payload, session)

    elif isinstance(payload, DeploymentStatusEventPayload):
        result = await handle_deployment_status(payload, session)

    elif isinstance(payload, PullRequestReviewCommentEventPayload):
        if payload.action in ("created", "edited"):
            result = await handle_pr_review_comment(payload, session)

    elif isinstance(payload, IssueCommentEventPayload):
        if payload.action in ("created", "edited"):
            result = await handle_issue_comment_on_pr(payload, session)

    elif isinstance(payload, PullRequestEventPayload):
        if payload.action == "opened":
            result = await handle_pr_opened(payload, session)
        elif payload.action == "ready_for_review":
            result = await handle_pr_ready_for_review(payload, session)
        elif payload.action in ("review_requested", "review_request_removed"):
            result = await handle_pr_review_requested(payload, session)
        elif payload.action == "closed" and payload.pull_request.merged:
            result = await handle_pr_merged(payload, session)
        elif payload.action == "closed" and not payload.pull_request.merged:
            result = await handle_pr_closed_without_merge(payload, session)
        elif payload.action == "synchronize":
            result = await handle_pr_synchronized(payload, session)
        elif payload.action == "reopened":
            # handle_pr_opened revives the withdrawn links itself, so the poller's
            # dispatch gets the same behaviour as this one.
            result = await handle_pr_opened(payload, session)
        elif payload.action in ("labeled", "unlabeled"):
            from boardman.services.pr_handler import handle_pr_labels_changed

            result = await handle_pr_labels_changed(payload, session)
        elif payload.action in ("edited", "assigned", "unassigned"):
            result = await handle_pr_edited(payload, session)
        elif payload.action == "converted_to_draft":
            result = await handle_pr_converted_to_draft(payload, session)

    if result is not None:
        # The sync is done and committed. Anything cached about this repo is now one
        # event out of date, so drop it here rather than waiting out a TTL.
        note_repo_changed(repo_full_name_from_payload(payload_dict), event=event_type)
        return result
    return {"ok": True, "message": "Event ignored"}


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> Response:
    raw_body = await request.body()
    event_type = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "").strip()

    signature = request.headers.get("X-Hub-Signature-256", "")
    # Accept either the org-level webhook secret or the GitHub App's own webhook secret —
    # both delivery paths are live during the App cutover (docs/GITHUB_APP_MIGRATION.md).
    if not verify_signature_any(
        raw_body,
        signature,
        [settings.github_webhook_secret, settings.github_app_webhook_secret],
    ):
        body = json.dumps({"ok": False, "message": "Invalid signature"})
        return Response(content=body, status_code=401)

    async def _mark_delivery(status: str, note: str) -> None:
        if not delivery_id:
            return
        row = await session.get(GitHubWebhookDelivery, delivery_id)
        if row:
            row.status = status
            row.note = note
            # Commit BEFORE the response goes out. Riding on session teardown leaves a
            # window where GitHub's immediate redelivery reads "processing" and the
            # duplicate is handled twice — caught live by edge guard E1 under load.
            await session.commit()

    if delivery_id:
        already = (
            await session.execute(
                select(GitHubWebhookDelivery).where(
                    GitHubWebhookDelivery.delivery_id == delivery_id
                )
            )
        ).scalar_one_or_none()
        # "processing" means another worker has it RIGHT NOW. Nothing resets that status
        # if the process dies mid-delivery, so treating it as final made an interrupted
        # event unrecoverable -- GitHub's redelivery, the one mechanism that exists for
        # this, was answered "already queued" forever. After the stale window it is
        # treated as abandoned and processed again; the handlers are idempotent, which is
        # what makes that the safe direction.
        stale_before = datetime.utcnow() - timedelta(seconds=_PROCESSING_STALE_SECONDS)
        in_flight = (
            already is not None
            and already.status == "processing"
            and ((already.created_at or stale_before) > stale_before)
        )
        if already and (already.status == "processed" or in_flight):
            body = json.dumps(
                {
                    "ok": True,
                    "message": (
                        "Duplicate delivery ignored"
                        if already.status == "processed"
                        else "Delivery already queued"
                    ),
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
    except Exception:  # noqa: BLE001 - the delivery row MUST reach a terminal state
        # A body that is not UTF-8 JSON is a malformed delivery, not a Boardman failure:
        # 400 and record it as processed so GitHub stops retrying it. Deliberately broad:
        # a 500 escaping here would leave the row at "processing" and every redelivery of
        # that id would short-circuit on "already queued" forever. log_degraded keeps the
        # ValueError/UnicodeDecodeError case quiet and gives anything else a traceback.
        log_degraded(_log, "github_webhook: parsing the delivery body")
        await _mark_delivery("processed", "invalid_json")
        return Response(
            content=json.dumps({"ok": False, "message": "Invalid JSON"}), status_code=400
        )

    if event_type == "ping":
        await _mark_delivery("processed", "pong")
        return Response(content=json.dumps({"ok": True, "message": "pong"}))

    # Events with no payload model of their own still have work to do: a repo appearing,
    # disappearing or being renamed invalidates the org listing, and a push invalidates
    # what is cached about that repo. Sending them to the 400 gate made that branch dead
    # code on the real webhook path -- only the tests, which call the dispatcher directly,
    # ever reached it.
    if event_type in _EVENTS_WITHOUT_A_PAYLOAD_MODEL:
        result = await dispatch_github_event(event_type, payload_dict, session)
        await _mark_delivery("processed", str(result.get("message") or "")[:200])
        return Response(content=json.dumps(result))

    if not parse_webhook_payload(event_type, payload_dict):
        await _mark_delivery("processed", "unsupported_event")
        body = json.dumps({"ok": False, "message": "Unsupported event type"})
        return Response(content=body, status_code=400)

    # Invalidate HERE as well as inside the dispatcher. Under the async setting the
    # handler runs in the worker process, and the read cache is a plain in-process dict --
    # so the API process, the one that answers the assistant, kept serving its pre-event
    # snapshot for the whole TTL while the worker purged a cache nobody reads.
    note_repo_changed(repo_full_name_from_payload(payload_dict), event=event_type)

    if settings.github_webhook_async_enabled:
        from boardman.broker.job_queue import get_job_queue

        # Commit the processing marker before enqueueing.  A concurrent GitHub retry
        # must see that this delivery already has a durable worker job.
        await session.commit()
        try:
            job = await get_job_queue().enqueue_job(
                "boardman_github_webhook_job",
                {
                    "delivery_id": delivery_id,
                    "event_type": event_type,
                    "payload": payload_dict,
                },
            )
        except Exception as exc:  # noqa: BLE001 - sync failure must not crash the service
            row = await session.get(GitHubWebhookDelivery, delivery_id) if delivery_id else None
            if row:
                row.status = "failed"
                row.note = f"enqueue failed: {str(exc)[:400]}"
                await session.commit()
            raise
        return Response(
            content=json.dumps(
                {"ok": True, "queued": True, "job_id": job.job_id, "delivery_id": delivery_id}
            ),
            status_code=202,
        )

    try:
        result = await dispatch_github_event(event_type, payload_dict, session)
    except Exception:  # noqa: BLE001 - a handler crash must still mark the delivery
        # Roll back FIRST. A handler that failed at the database level leaves the session
        # needing one, and `_mark_delivery` commits: without this the bookkeeping raises
        # PendingRollbackError, which replaces the real exception on the way out and loses
        # the failed note too.
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 - nothing better to do if even that fails
            log_degraded(_log, "github_webhook: rolling back after a handler failure")
        await _mark_delivery("failed", "unhandled exception in dispatch")
        raise

    if result.get("ok", True) is False:
        message = str(result.get("message") or "synchronization failed")
        await _mark_delivery("failed", message)
        # 500 asks GitHub to retry, which is right for a Plaky blip and wrong for a
        # payload that will never parse -- a comment from a deleted account redelivers on
        # the retry schedule and fails identically every time, burning the worker's
        # attempts too. A handler that says the payload is unusable gets a 200: recorded
        # as failed, not asked for again.
        retryable = not bool(result.get("unretryable"))
        return Response(content=json.dumps(result), status_code=500 if retryable else 200)

    if result is not None:
        await _mark_delivery("processed", "handled")
        return Response(content=json.dumps(result))

    await _mark_delivery("processed", "ignored")
    return Response(content=json.dumps({"ok": True, "message": "Event ignored"}))
