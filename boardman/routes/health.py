from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.session import async_session
from boardman.database.models import IssueTaskMap, SyncLog


router = APIRouter()


@router.get("/health")
async def health():
    return {"ok": True, "service": "deepiri-boardman"}


@router.get("/mappings")
async def list_mappings(session: AsyncSession = Depends(async_session)):
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
async def list_logs(limit: int = 50, session: AsyncSession = Depends(async_session)):
    result = await session.execute(select(SyncLog).order_by(SyncLog.created_at.desc()).limit(limit))
    logs = result.scalars().all()
    return {
        "ok": True,
        "logs": [
            {
                "action": l.action,
                "github_repo": l.github_repo,
                "github_ref": l.github_ref,
                "plaky_task_id": l.plaky_task_id,
                "detail": l.detail,
                "created_at": l.created_at.isoformat() if l.created_at else None,
            }
            for l in logs
        ],
    }