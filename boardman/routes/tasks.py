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
from boardman.plaky.board_schema import fetch_board_schema_bundle


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
    filters: Optional[dict] = None


class LinkPRRequest(BaseModel):
    pr_url: str
    task_id: str
    update_status: bool = False


@router.post("/tasks")
async def create_task(req: CreateTaskRequest, session: AsyncSession = Depends(get_db)):
    plaky = PlakyClient()
    filters = req.filters if isinstance(req.filters, dict) else {}

    raw_title = (req.title or "").strip() or str(filters.get("title") or "").strip()
    raw_description = (req.description or "").strip() or str(filters.get("description") or "").strip()
    engineer_plaky_id = (req.engineer_plaky_id or "").strip() or str(filters.get("engineer_plaky_id") or "").strip()
    qa_plaky_id = (req.qa_plaky_id or "").strip() or str(filters.get("qa_plaky_id") or "").strip()

    if not raw_title:
        return {"ok": False, "status": 400, "message": "title is required"}

    title = f"[{req.repo}] {raw_title}" if req.repo else raw_title
    cfg = load_team_assignments()
    repo_full = (req.repo or "").strip() or "deepiri-org/unknown"

    engineer_field_key = (cfg.plaky_field_engineer or "").strip()
    qa_field_key = (cfg.plaky_field_qa or "").strip()
    if (engineer_plaky_id and not engineer_field_key) or (qa_plaky_id and not qa_field_key):
        board_id = (req.plaky_board_id or "").strip()
        if board_id:
            try:
                bundle = await fetch_board_schema_bundle(board_id)
                normalized = bundle.get("normalized") if isinstance(bundle, dict) else None
                fields = normalized.get("fields") if isinstance(normalized, dict) else []
                person_fields = []
                if isinstance(fields, list):
                    for f in fields:
                        if not isinstance(f, dict):
                            continue
                        key = str(f.get("key") or "").strip()
                        ftype = str(f.get("type") or "").strip().upper()
                        name = str(f.get("name") or "").strip().lower()
                        if key and ftype == "PERSON":
                            person_fields.append((key, name))
                if person_fields:
                    if not qa_field_key:
                        for k, n in person_fields:
                            if "qa" in n or "quality" in n:
                                qa_field_key = k
                                break
                    if not engineer_field_key:
                        for k, n in person_fields:
                            if k == qa_field_key:
                                continue
                            if any(tok in n for tok in ("engineer", "developer", "dev", "contributor", "owner", "assignee")):
                                engineer_field_key = k
                                break
                    if not qa_field_key and person_fields:
                        qa_field_key = person_fields[0][0]
                    if not engineer_field_key:
                        for k, _ in person_fields:
                            if k != qa_field_key:
                                engineer_field_key = k
                                break
            except Exception:
                pass

    field_values: dict[str, str] = {}
    if req.auto_assign_team:
        field_values = dict(await build_assignment_field_map(repo_full, cfg))
    if engineer_plaky_id and engineer_field_key:
        field_values[engineer_field_key] = engineer_plaky_id
    if qa_plaky_id and qa_field_key:
        field_values[qa_field_key] = qa_plaky_id

    async with plaky_placement_context(req.plaky_board_id, req.plaky_group_id):
        result = await plaky.create_task(
            title=title,
            description=raw_description,
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