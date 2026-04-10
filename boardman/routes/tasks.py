from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.session import get_db
from boardman.database.models import IssueTaskMap
from boardman.plaky.client import PlakyClient


router = APIRouter()


class CreateTaskRequest(BaseModel):
    title: str
    description: str = ""
    priority: str = "medium"
    repo: Optional[str] = None


class LinkPRRequest(BaseModel):
    pr_url: str
    task_id: str
    update_status: bool = False


@router.post("/tasks")
async def create_task(req: CreateTaskRequest, session: AsyncSession = Depends(get_db)):
    plaky = PlakyClient()
    title = f"[{req.repo}] {req.title}" if req.repo else req.title
    result = await plaky.create_task(title=title, description=req.description, priority=req.priority)

    if not result.get("ok"):
        return result

    return result


@router.get("/tasks")
async def list_tasks(status: str = "open", session: AsyncSession = Depends(get_db)):
    plaky = PlakyClient()
    result = await plaky.get_tasks(status=status)
    return result


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, session: AsyncSession = Depends(get_db)):
    plaky = PlakyClient()
    result = await plaky.get_task(task_id)
    return result


@router.post("/tasks/{task_id}/link-pr")
async def link_pr(task_id: str, req: LinkPRRequest, session: AsyncSession = Depends(get_db)):
    plaky = PlakyClient()
    comment = f"**PR Linked:** [View PR]({req.pr_url})"
    result = await plaky.add_comment(task_id, comment)

    if not result.get("ok"):
        return result

    if req.update_status:
        from boardman.settings import settings
        await plaky.update_task_status(task_id, settings.plaky_pr_merge_status)

    return result