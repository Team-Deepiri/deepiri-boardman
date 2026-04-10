from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.assignment.config import load_team_assignments
from boardman.assignment.qa_picker import build_assignment_field_map
from boardman.database.session import get_db
from boardman.database.models import IssueTaskMap
from boardman.plaky.client import PlakyClient
from boardman.plaky.placement import plaky_placement_context


router = APIRouter()


class CreateTaskRequest(BaseModel):
    title: str
    description: str = ""
    priority: str = "medium"
    repo: Optional[str] = None
    plaky_board_id: Optional[str] = None
    plaky_group_id: Optional[str] = None
    engineer_plaky_id: Optional[str] = None
    qa_plaky_id: Optional[str] = None
    auto_assign_team: bool = True


class LinkPRRequest(BaseModel):
    pr_url: str
    task_id: str
    update_status: bool = False


@router.post("/tasks")
async def create_task(req: CreateTaskRequest, session: AsyncSession = Depends(get_db)):
    plaky = PlakyClient()
    title = f"[{req.repo}] {req.title}" if req.repo else req.title
    cfg = load_team_assignments()
    repo_full = (req.repo or "").strip() or "deepiri-org/unknown"

    field_values: dict[str, str] = {}
    if req.auto_assign_team:
        field_values = dict(build_assignment_field_map(repo_full, cfg))
    if req.engineer_plaky_id and cfg.plaky_field_engineer:
        field_values[cfg.plaky_field_engineer] = req.engineer_plaky_id.strip()
    if req.qa_plaky_id and cfg.plaky_field_qa:
        field_values[cfg.plaky_field_qa] = req.qa_plaky_id.strip()

    async with plaky_placement_context(req.plaky_board_id, req.plaky_group_id):
        result = await plaky.create_task(
            title=title,
            description=req.description,
            priority=req.priority,
            field_values=field_values or None,
        )

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