"""SQLite-backed job queue (enqueue from API; worker in `boardman.sqlite_worker`)."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import text, update

from boardman.database.models import BackgroundJob
from boardman.database.session import async_session

logger = logging.getLogger(__name__)

# Values returned by GET /agent/jobs/{id} (arq-compatible)
JobApiStatus = Literal["deferred", "queued", "in_progress", "complete", "incomplete", "not_found"]


@dataclass
class EnqueuedJob:
    job_id: str


class SqliteJobQueue:
    async def enqueue_job(self, function_name: str, payload: dict[str, Any]) -> EnqueuedJob:
        jid = uuid.uuid4().hex
        async with async_session() as session:
            session.add(
                BackgroundJob(
                    id=jid,
                    kind=function_name,
                    payload_json=json.dumps(payload, default=str),
                    status="pending",
                )
            )
            await session.commit()
        return EnqueuedJob(job_id=jid)

    async def fetch_public_job(self, job_id: str) -> dict[str, Any] | None:
        async with async_session() as session:
            row = await session.get(BackgroundJob, job_id)
            if row is None:
                return None
            st = _db_status_to_api(row.status)
            out: dict[str, Any] = {
                "ok": True,
                "job_id": job_id,
                "status": st,
            }
            if st == "complete":
                out["success"] = row.success if row.success is not None else True
                if row.result_json:
                    try:
                        out["result"] = json.loads(row.result_json)
                    except json.JSONDecodeError:
                        out["result"] = row.result_json
            return out


_queue: SqliteJobQueue | None = None


def get_job_queue() -> SqliteJobQueue:
    global _queue
    if _queue is None:
        _queue = SqliteJobQueue()
    return _queue


async def close_job_queue() -> None:
    """Symmetry with old Redis pool teardown (no-op for SQLite)."""
    global _queue
    _queue = None


def reset_job_queue_for_tests() -> None:
    global _queue
    _queue = None


def _db_status_to_api(db_status: str) -> JobApiStatus:
    if db_status == "pending":
        return "queued"
    if db_status == "running":
        return "in_progress"
    if db_status == "complete":
        return "complete"
    if db_status == "incomplete":
        return "incomplete"
    return "queued"


CLAIM_SQL = text(
    """
WITH picked AS (
  SELECT id FROM background_jobs
  WHERE status = 'pending'
  ORDER BY created_at ASC
  LIMIT 1
)
UPDATE background_jobs
SET status = 'running', started_at = :started
WHERE id IN (SELECT id FROM picked)
RETURNING id, kind, payload_json
"""
)


async def claim_next_job_row() -> tuple[str, str, dict[str, Any]] | None:
    """Atomically claim one pending job. Returns (id, kind, payload) or None."""
    now = datetime.utcnow()
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(CLAIM_SQL, {"started": now})
            row = result.first()
            if row is None:
                return None
            jid, kind, raw = row[0], row[1], row[2]
        try:
            payload = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError:
            logger.warning("job %s: bad payload JSON", jid)
            payload = {}
    return jid, kind, payload


async def mark_job_finished(
    job_id: str,
    *,
    success: bool,
    status: str,
    result: dict[str, Any] | None,
) -> None:
    now = datetime.utcnow()
    body = json.dumps(result, default=str) if result is not None else None
    async with async_session() as session:
        await session.execute(
            update(BackgroundJob)
            .where(BackgroundJob.id == job_id)
            .values(
                status=status,
                success=success,
                result_json=body,
                finished_at=now,
            )
        )
        await session.commit()


async def fail_stale_running_jobs(stale_after_seconds: int = 7200) -> int:
    """Mark `running` jobs older than threshold as incomplete (worker crash)."""
    from datetime import timedelta

    cutoff = datetime.utcnow() - timedelta(seconds=stale_after_seconds)
    now = datetime.utcnow()
    err = json.dumps({"error": "worker lost job (stale running)"})
    async with async_session() as session:
        r = await session.execute(
            update(BackgroundJob)
            .where(
                BackgroundJob.status == "running",
                BackgroundJob.started_at.is_not(None),
                BackgroundJob.started_at < cutoff,
            )
            .values(
                status="incomplete",
                success=False,
                result_json=err,
                finished_at=now,
            )
        )
        await session.commit()
        return int(r.rowcount or 0)
