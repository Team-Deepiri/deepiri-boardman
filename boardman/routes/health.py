from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.models import IssueTaskMap, SyncLog
from boardman.database.session import get_db

router = APIRouter()


@router.get("/health")
async def health():
    return {"ok": True, "service": "deepiri-boardman"}


@router.get("/mappings")
async def list_mappings(session: AsyncSession = Depends(get_db)):
    result = await session.execute(select(IssueTaskMap))
    mappings = result.scalars().all()
    return {
        "ok": True,
        "mappings": [
            {
                "github_repo": m.github_repo,
                "github_issue_number": m.github_issue_number,
                "plaky_task_id": m.plaky_task_id,
                "plaky_task_url": m.plaky_task_url,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in mappings
        ],
    }


@router.get("/sync-logs")
async def list_logs(limit: int = 50, session: AsyncSession = Depends(get_db)):
    result = await session.execute(select(SyncLog).order_by(SyncLog.created_at.desc()).limit(limit))
    logs = result.scalars().all()
    return {
        "ok": True,
        "logs": [
            {
                "action": log_entry.action,
                "github_repo": log_entry.github_repo,
                "github_ref": log_entry.github_ref,
                "plaky_task_id": log_entry.plaky_task_id,
                "detail": log_entry.detail,
                "created_at": log_entry.created_at.isoformat() if log_entry.created_at else None,
            }
            for log_entry in logs
        ],
    }
