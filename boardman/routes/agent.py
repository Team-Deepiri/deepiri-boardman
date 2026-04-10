from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.agent.service import delete_agent_session, get_session_history, run_agent_chat
from boardman.database.session import get_db
from boardman.plaky.placement import plaky_placement_context
from boardman.services.direction_init import init_direction_file
from boardman.services.scan_handler import run_repo_scan

router = APIRouter()


class AgentChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    session_id: Optional[str] = None
    repo: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    allow_writes: bool = Field(
        False,
        description="When true, agent may call Plaky create/update/comment tools (guardrail).",
    )
    plaky_board_id: Optional[str] = Field(
        None,
        description="Plaky board (project) id for new items; with plaky_group_id selects placement.",
    )
    plaky_group_id: Optional[str] = Field(
        None,
        description="Plaky group (section) id — API has no separate 'table'; this is the column/section.",
    )


class ScanRequest(BaseModel):
    repo: str = Field(..., description="owner/repo")
    dry_run: bool = False
    provider: Optional[str] = None
    model: Optional[str] = None


class InitDirectionRequest(BaseModel):
    repo: str = Field(..., description="owner/repo")
    branch: Optional[str] = None
    force: bool = False


@router.post("/agent/chat")
async def agent_chat(body: AgentChatRequest, session: AsyncSession = Depends(get_db)) -> dict:
    async with plaky_placement_context(body.plaky_board_id, body.plaky_group_id):
        reply, sid = await run_agent_chat(
            session,
            message=body.message,
            session_id=body.session_id,
            repo=body.repo,
            provider=body.provider,
            model=body.model,
            allow_writes=body.allow_writes,
            plaky_board_id=body.plaky_board_id,
        )
    return {"ok": True, "reply": reply, "session_id": sid}


@router.get("/agent/sessions/{session_id}/history")
async def agent_history(session_id: str, session: AsyncSession = Depends(get_db)) -> dict:
    hist = await get_session_history(session, session_id)
    return {"ok": True, "session_id": session_id, "messages": hist}


@router.delete("/agent/sessions/{session_id}")
async def agent_session_delete(session_id: str, session: AsyncSession = Depends(get_db)) -> dict:
    gone = await delete_agent_session(session, session_id)
    return {"ok": True, "deleted": gone}


@router.post("/agent/scan")
async def agent_scan(body: ScanRequest, session: AsyncSession = Depends(get_db)) -> dict:
    result = await run_repo_scan(
        session,
        body.repo,
        dry_run=body.dry_run,
        provider=body.provider,
        model=body.model,
    )
    return result


@router.post("/agent/init-direction")
async def api_init_direction(body: InitDirectionRequest) -> dict:
    parts = body.repo.split("/")
    if len(parts) != 2:
        return {"ok": False, "message": "repo must be owner/name"}
    owner, name = parts[0], parts[1]
    return await init_direction_file(owner, name, branch=body.branch, force=body.force)
