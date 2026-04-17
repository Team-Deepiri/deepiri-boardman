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
from boardman.plaky.board_schema import fetch_board_schema_bundle


router = APIRouter()


def _extract_created_task_id(result: dict) -> str:
    task = result.get("task") if isinstance(result, dict) and isinstance(result.get("task"), dict) else {}
    candidates = [
        result.get("task_id") if isinstance(result, dict) else None,
        task.get("id"),
        task.get("itemId"),
        task.get("taskId"),
        task.get("_id"),
    ]
    for raw in candidates:
        val = str(raw or "").strip()
        if val:
            return val
    for key in ("item", "data", "result", "task"):
        nested = task.get(key)
        if isinstance(nested, dict):
            for nk in ("id", "itemId", "taskId", "_id"):
                val = str(nested.get(nk) or "").strip()
                if val:
                    return val
    return ""


async def _run_post_create_assignments(
    plaky: PlakyClient,
    *,
    result: dict,
    board_id: str,
    group_id: str,
    title: str,
    field_values: dict[str, str],
) -> dict:
    """
    Inspect the JSON body of POST /tasks: this object is returned as `post_create_assignment`.
    It includes `field_values_attempted` (what we sent to Plaky PATCH …/items/{id}/fields) and
    the `patch_item_field_values` result (`ok`, `mode`, `failed` with HTTP snippets on error).
    """
    if not field_values:
        return {"ok": True, "skipped": True, "message": "No assignment fields provided"}
    if not board_id:
        return {"ok": False, "skipped": True, "message": "Cannot patch assignments without board_id"}

    item_id = _extract_created_task_id(result)
    id_source = "create_response" if item_id else ""
    if not item_id:
        listed = await plaky.list_board_items(board_id, max_pages=2)
        rows = listed.get("items") if isinstance(listed, dict) else []
        if isinstance(rows, list):
            title_norm = title.strip().lower()
            group_norm = group_id.strip()
            for row in reversed(rows):
                if not isinstance(row, dict):
                    continue
                rid = str(row.get("id") or row.get("itemId") or row.get("taskId") or row.get("_id") or "").strip()
                if not rid:
                    continue
                row_group = str(
                    row.get("groupId")
                    or row.get("group_id")
                    or ((row.get("group") or {}).get("id") if isinstance(row.get("group"), dict) else "")
                    or ""
                ).strip()
                row_title = str(row.get("name") or row.get("title") or "").strip().lower()
                if group_norm and row_group and row_group != group_norm:
                    continue
                if title_norm and row_title and title_norm not in row_title:
                    continue
                item_id = rid
                id_source = "list_match_title_group"
                break
            if not item_id:
                for row in reversed(rows):
                    if not isinstance(row, dict):
                        continue
                    rid = str(
                        row.get("id") or row.get("itemId") or row.get("taskId") or row.get("_id") or ""
                    ).strip()
                    if rid:
                        item_id = rid
                        id_source = "list_latest_fallback"
                        break

    if not item_id:
        return {
            "ok": False,
            "message": "Task created but post-create assignment could not resolve item id",
            "attempted_fields": sorted(field_values.keys()),
            "field_values_attempted": dict(field_values),
        }

    patched = await plaky.patch_item_field_values(board_id, item_id, field_values)
    if isinstance(patched, dict):
        patched["item_id"] = item_id
        patched["item_id_source"] = id_source
        patched["field_values_attempted"] = dict(field_values)
    return patched if isinstance(patched, dict) else {"ok": False, "message": "Unexpected patch response"}


def _extract_created_task_id(result: dict) -> str:
    task = result.get("task") if isinstance(result, dict) and isinstance(result.get("task"), dict) else {}
    candidates = [
        result.get("task_id") if isinstance(result, dict) else None,
        task.get("id"),
        task.get("itemId"),
        task.get("taskId"),
        task.get("_id"),
    ]
    for raw in candidates:
        val = str(raw or "").strip()
        if val:
            return val
    for key in ("item", "data", "result", "task"):
        nested = task.get(key)
        if isinstance(nested, dict):
            for nk in ("id", "itemId", "taskId", "_id"):
                val = str(nested.get(nk) or "").strip()
                if val:
                    return val
    return ""


async def _run_post_create_assignments(
    plaky: PlakyClient,
    *,
    result: dict,
    board_id: str,
    group_id: str,
    title: str,
    field_values: dict[str, str],
) -> dict:
    """
    Inspect the JSON body of POST /tasks: this object is returned as `post_create_assignment`.
    It includes `field_values_attempted` (what we sent to Plaky PATCH …/items/{id}/fields) and
    the `patch_item_field_values` result (`ok`, `mode`, `failed` with HTTP snippets on error).
    """
    if not field_values:
        return {"ok": True, "skipped": True, "message": "No assignment fields provided"}
    if not board_id:
        return {"ok": False, "skipped": True, "message": "Cannot patch assignments without board_id"}

    item_id = _extract_created_task_id(result)
    id_source = "create_response" if item_id else ""
    if not item_id:
        listed = await plaky.list_board_items(board_id, max_pages=2)
        rows = listed.get("items") if isinstance(listed, dict) else []
        if isinstance(rows, list):
            title_norm = title.strip().lower()
            group_norm = group_id.strip()
            for row in reversed(rows):
                if not isinstance(row, dict):
                    continue
                rid = str(row.get("id") or row.get("itemId") or row.get("taskId") or row.get("_id") or "").strip()
                if not rid:
                    continue
                row_group = str(
                    row.get("groupId")
                    or row.get("group_id")
                    or ((row.get("group") or {}).get("id") if isinstance(row.get("group"), dict) else "")
                    or ""
                ).strip()
                row_title = str(row.get("name") or row.get("title") or "").strip().lower()
                if group_norm and row_group and row_group != group_norm:
                    continue
                if title_norm and row_title and title_norm not in row_title:
                    continue
                item_id = rid
                id_source = "list_match_title_group"
                break
            if not item_id:
                for row in reversed(rows):
                    if not isinstance(row, dict):
                        continue
                    rid = str(
                        row.get("id") or row.get("itemId") or row.get("taskId") or row.get("_id") or ""
                    ).strip()
                    if rid:
                        item_id = rid
                        id_source = "list_latest_fallback"
                        break

    if not item_id:
        return {
            "ok": False,
            "message": "Task created but post-create assignment could not resolve item id",
            "attempted_fields": sorted(field_values.keys()),
            "field_values_attempted": dict(field_values),
        }

    patched = await plaky.patch_item_field_values(board_id, item_id, field_values)
    if isinstance(patched, dict):
        patched["item_id"] = item_id
        patched["item_id_source"] = id_source
        patched["field_values_attempted"] = dict(field_values)
    return patched if isinstance(patched, dict) else {"ok": False, "message": "Unexpected patch response"}


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

    title = raw_title
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
        field_values = dict(
            await build_assignment_field_map(
                repo_full,
                cfg,
                repo_value=repo_display,
                plaky_field_repo_key=(repo_field_key or None),
            )
        )
    elif repo_field_key and repo_display:
        field_values[repo_field_key] = repo_display
    if engineer_plaky_id and engineer_field_key:
        field_values[engineer_field_key] = engineer_plaky_id
    if qa_plaky_id and qa_field_key:
        field_values[qa_field_key] = qa_plaky_id

    async with plaky_placement_context(req.plaky_board_id, req.plaky_group_id):
        result = await plaky.create_task(
            title=title,
            description=raw_description,
            priority=req.priority,
            field_values=None,
        )

    if not result.get("ok"):
        return result

    post_assign = await _run_post_create_assignments(
        plaky,
        result=result,
        board_id=(req.plaky_board_id or "").strip(),
        group_id=(req.plaky_group_id or "").strip(),
        title=title,
        field_values=field_values,
    )
    result["post_create_assignment"] = post_assign

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