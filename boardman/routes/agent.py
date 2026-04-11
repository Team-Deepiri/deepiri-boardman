from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.agent.service import delete_agent_session, get_session_history, run_agent_chat
from boardman.broker.arq_pool import get_arq_pool
from boardman.database.session import get_db
from boardman.plaky.placement import context_board_id, context_group_id, plaky_placement_context
from boardman.ratelimit.dependencies import require_agent_rate_limit
from boardman.services.direction_init import init_direction_file
from boardman.services.scan_handler import run_repo_scan
from boardman.settings import settings

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
    use_tools: bool = Field(
        False,
        description=(
            "When true (and AGENT_LANGCHAIN_TOOLS), run the multi-step LangChain tool agent. "
            "When false (default), one LLM call only — much faster for normal chat."
        ),
    )
    plaky_board_id: Optional[str] = Field(
        None,
        description="Plaky board (project) id for new items; with plaky_group_id selects placement.",
    )
    plaky_group_id: Optional[str] = Field(
        None,
        description="Plaky group (section) id — API has no separate 'table'; this is the column/section.",
    )
    queue: bool = Field(
        False,
        description="If true and REDIS_URL is set, enqueue to arq worker; poll GET /agent/jobs/{job_id}.",
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
async def agent_chat(
    body: AgentChatRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_agent_rate_limit(request)
    if body.queue:
        if not (settings.redis_url or "").strip():
            raise HTTPException(
                status_code=503,
                detail="queue=true requires REDIS_URL and a running arq worker (see docker-compose).",
            )
        if not settings.agent_async_enqueue_enabled:
            raise HTTPException(status_code=503, detail="Async agent enqueue is disabled in settings.")
        payload = body.model_dump(exclude={"queue"}, exclude_none=True)
        pool = await get_arq_pool()
        job = await pool.enqueue_job("boardman_agent_chat_job", payload)
        if job is None:
            raise HTTPException(status_code=409, detail="Could not enqueue job (duplicate id or Redis conflict).")
        return {"ok": True, "queued": True, "job_id": job.job_id}

    async with plaky_placement_context(body.plaky_board_id, body.plaky_group_id):
        reply, sid = await run_agent_chat(
            session,
            message=body.message,
            session_id=body.session_id,
            repo=body.repo,
            provider=body.provider,
            model=body.model,
            allow_writes=body.allow_writes,
            use_tools=body.use_tools,
            plaky_board_id=context_board_id(),
            plaky_group_id=context_group_id(),
        )
    return {"ok": True, "reply": reply, "session_id": sid}


@router.get("/agent/jobs/{job_id}")
async def agent_job_status(job_id: str) -> dict[str, Any]:
    if not (settings.redis_url or "").strip():
        raise HTTPException(status_code=503, detail="REDIS_URL is not configured.")
    from arq.jobs import Job, JobStatus

    redis = await get_arq_pool()
    job = Job(job_id, redis)
    st = await job.status()
    out: dict[str, Any] = {"ok": True, "job_id": job_id, "status": st.value}
    if st == JobStatus.complete:
        info = await job.result_info()
        if info is not None:
            out["success"] = info.success
            out["result"] = info.result
    return out


@router.get("/agent/sessions/{session_id}/history")
async def agent_history(session_id: str, session: AsyncSession = Depends(get_db)) -> dict:
    hist = await get_session_history(session, session_id)
    return {"ok": True, "session_id": session_id, "messages": hist}


@router.delete("/agent/sessions/{session_id}")
async def agent_session_delete(session_id: str, session: AsyncSession = Depends(get_db)) -> dict:
    gone = await delete_agent_session(session, session_id)
    return {"ok": True, "deleted": gone}


@router.post("/agent/scan")
async def agent_scan(
    body: ScanRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> dict:
    await require_agent_rate_limit(request)
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
