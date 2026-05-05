from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.session import get_db
from boardman.plaky.client import PlakyClient
from boardman.services.pr_link_comment import collect_pr_urls, format_pr_link_comment
from boardman.settings import settings
from boardman.services.task_mutations import (
    CreateTaskInput,
    UpdateTaskInput,
    create_task_internal,
    update_task_internal,
)


router = APIRouter()


class CreateTaskRequest(BaseModel):
    title: str
    description: str = ""
    priority: str = "Medium"
    status: str = "In Progress"
    task_type: str = Field(
        default="Feature",
        validation_alias=AliasChoices("type", "task_type"),
    )
    github_repos: Optional[List[str]] = None  # owner/repo strings; deduped
    plaky_board_id: Optional[str] = None
    plaky_group_id: Optional[str] = None
    engineer_plaky_id: Optional[str] = None
    qa_plaky_id: Optional[str] = None
    # When True (default), empty qa_plaky_id is filled from team_assignments.yml (repo roster).
    # Engineer/contributor is never roster-filled; set engineer_plaky_id to assign dev.
    # Explicit qa_plaky_id always wins over the roster pick.
    auto_assign_team: bool = True
    filters: Optional[dict] = None


class LinkPRRequest(BaseModel):
    """Link one or more GitHub PRs to the task. Task id is the URL path param `{task_id}`."""

    model_config = ConfigDict(extra="ignore")

    # Backward compatible single URL; combine with pr_urls when both sent.
    pr_url: Optional[str] = None
    pr_urls: Optional[List[str]] = None
    update_status: bool = False


class UpdateTaskRequest(BaseModel):
    status: Optional[str] = None
    task_type: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("type", "task_type"),
    )
    priority: Optional[str] = None
    qa_plaky_id: Optional[str] = None
    auto_assign_qa: bool = False
    github_repo: Optional[str] = None
    plaky_board_id: Optional[str] = None


@router.post("/tasks")
async def create_task(req: CreateTaskRequest, session: AsyncSession = Depends(get_db)):
    return await create_task_internal(
        CreateTaskInput(
            title=req.title,
            description=req.description,
            priority=req.priority,
            status=req.status,
            task_type=req.task_type,
            github_repos=req.github_repos,
            plaky_board_id=req.plaky_board_id,
            plaky_group_id=req.plaky_group_id,
            engineer_plaky_id=req.engineer_plaky_id,
            qa_plaky_id=req.qa_plaky_id,
            auto_assign_team=req.auto_assign_team,
            filters=req.filters,
        )
    )


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


@router.patch("/tasks/{task_id}")
async def update_task(task_id: str, req: UpdateTaskRequest, session: AsyncSession = Depends(get_db)):
    return await update_task_internal(
        task_id,
        UpdateTaskInput(
            status=req.status,
            task_type=req.task_type,
            priority=req.priority,
            qa_plaky_id=req.qa_plaky_id,
            auto_assign_qa=req.auto_assign_qa,
            github_repo=req.github_repo,
            plaky_board_id=req.plaky_board_id,
        ),
    )


@router.post("/tasks/{task_id}/link-pr")
async def link_pr(task_id: str, req: LinkPRRequest, session: AsyncSession = Depends(get_db)):
    urls = collect_pr_urls(pr_url=req.pr_url, pr_urls=req.pr_urls)
    if not urls:
        return {"ok": False, "status": 400, "message": "Provide pr_url and/or pr_urls with at least one PR URL"}

    plaky = PlakyClient()
    comment = format_pr_link_comment(urls)
    result = await plaky.add_comment(task_id, comment)

    if not result.get("ok"):
        return result

    if req.update_status:
        await update_task_internal(
            task_id,
            UpdateTaskInput(status=settings.plaky_pr_merge_status),
        )

    return result
