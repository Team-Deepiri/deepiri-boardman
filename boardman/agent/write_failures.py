"""Surface board writes that failed after Boardman already said they were done.

Task creation is reported as finished because it lands within seconds and the person
asking does not care about the queue. That is only safe if the rare failure is not
silent — hedging every sentence would be the wrong trade, so instead the next turn
checks whether anything Boardman claimed actually failed, and says so unprompted.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from boardman.database.models import BackgroundJob

logger = logging.getLogger(__name__)

WATCHED_KINDS = ("plaky_create_tasks_job",)


def _titles(payload_json: str | None, result_json: str | None) -> list[str]:
    out: list[str] = []
    try:
        payload = json.loads(payload_json or "{}")
        for row in payload.get("tasks") or []:
            if isinstance(row, dict) and row.get("title"):
                out.append(str(row["title"]).strip())
    except (TypeError, ValueError):
        pass
    if out:
        return out
    try:
        result = json.loads(result_json or "{}")
        for row in result.get("results") or []:
            if isinstance(row, dict) and not row.get("ok") and row.get("title"):
                out.append(str(row["title"]).strip())
    except (TypeError, ValueError):
        pass
    return out


async def recent_failed_task_writes(session: Any, *, minutes: int = 30) -> str:
    """Markdown telling the model to correct itself, or '' when nothing failed."""
    cutoff = datetime.utcnow() - timedelta(minutes=minutes)
    try:
        rows = (
            (
                await session.execute(
                    select(BackgroundJob)
                    .where(
                        BackgroundJob.kind.in_(WATCHED_KINDS),
                        BackgroundJob.finished_at.is_not(None),
                        BackgroundJob.finished_at >= cutoff,
                        BackgroundJob.success.is_(False),
                    )
                    .order_by(BackgroundJob.finished_at.desc())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
    except Exception:
        logger.exception("could not read recent background write failures")
        return ""
    if not rows:
        return ""

    lines = [
        "",
        "## A board write you already reported did NOT land",
        "",
        "You told the user these tasks were created. The write failed afterwards, so "
        "OPEN your reply by correcting that plainly before anything else — name them, "
        "say they are not on the board, and offer to retry.",
        "",
    ]
    for row in rows:
        names = _titles(row.payload_json, row.result_json)
        detail = ""
        try:
            detail = str((json.loads(row.result_json or "{}") or {}).get("error") or "")[:160]
        except (TypeError, ValueError):
            detail = ""
        for name in names[:6]:
            lines.append(f"- **{name}** — not created" + (f" ({detail})" if detail else ""))
    lines.append("")
    return "\n".join(lines)
