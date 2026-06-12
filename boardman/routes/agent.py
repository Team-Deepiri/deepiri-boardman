from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.agent.service import (
    delete_agent_session,
    get_session_history,
    iter_agent_chat_sse,
    run_agent_chat,
)
from boardman.broker.job_queue import get_job_queue
from boardman.database.session import async_session, get_db
from boardman.plaky.placement import context_board_id, context_group_id, plaky_placement_context
from boardman.ratelimit.dependencies import require_agent_rate_limit
from boardman.services.direction_init import init_direction_file
from boardman.services.scan_handler import run_repo_scan
from boardman.settings import settings

router = APIRouter()


class AgentChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    session_id: str | None = None
    repo: str | None = None
    provider: str | None = None
    model: str | None = None
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
    plaky_board_id: str | None = Field(
        None,
        description="Plaky board (project) id for new items; with plaky_group_id selects placement.",
    )
    plaky_group_id: str | None = Field(
        None,
        description="Plaky group (section) id — API has no separate 'table'; this is the column/section.",
    )
    queue: bool = Field(
        False,
        description="If true, enqueue to SQLite worker; poll GET /agent/jobs/{job_id}.",
    )


class ScanRequest(BaseModel):
    repo: str = Field(..., description="owner/repo")
    dry_run: bool = False
    provider: str | None = None
    model: str | None = None


class InitDirectionRequest(BaseModel):
    repo: str = Field(..., description="owner/repo")
    branch: str | None = None
    force: bool = False


class InitDirectionResponse(BaseModel):
    ok: bool
    message: str | None = None
    skipped: bool | None = None
    url: str | None = None
    branch: str | None = None
    pr_branch: str | None = None
    actor: str | None = None


class AgentChatResponse(BaseModel):
    ok: bool = True
    reply: str = Field(..., description="Assistant reply as GitHub-flavored markdown (plain text, not HTML).")
    session_id: str
    content_format: Literal["markdown"] = "markdown"


class AgentHistoryMessage(BaseModel):
    role: str
    content: str = Field(..., description="Message body; assistant rows are GitHub-flavored markdown.")
    created_at: str | None = None
    content_format: Literal["markdown", "plain"] = Field(
        ...,
        description="How clients should render content: markdown for assistant, plain for user.",
    )


class AgentHistoryResponse(BaseModel):
    ok: bool = True
    session_id: str
    messages: list[AgentHistoryMessage]


@router.post("/agent/chat", response_model=AgentChatResponse)
async def agent_chat(
    body: AgentChatRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_agent_rate_limit(request)
    if body.queue:
        if not settings.agent_async_enqueue_enabled:
            raise HTTPException(status_code=503, detail="Async agent enqueue is disabled in settings.")
        payload = body.model_dump(exclude={"queue"}, exclude_none=True)
        q = get_job_queue()
        job = await q.enqueue_job("boardman_agent_chat_job", payload)
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
    return AgentChatResponse(reply=reply, session_id=sid)


@router.post("/agent/chat/stream")
async def agent_chat_stream(body: AgentChatRequest, request: Request) -> StreamingResponse:
    """
    SSE frames (text/event-stream) for lower perceived latency.
    Supports both plain chat and multi-step tool agent.
    """
    await require_agent_rate_limit(request)
    if body.queue:
        raise HTTPException(status_code=400, detail="queue is not supported for streaming")

    async def event_bytes() -> AsyncIterator[bytes]:
        async with plaky_placement_context(body.plaky_board_id, body.plaky_group_id):
            async with async_session() as db:
                try:
                    async for chunk in iter_agent_chat_sse(
                        db,
                        message=body.message,
                        session_id=body.session_id,
                        repo=body.repo,
                        provider=body.provider,
                        model=body.model,
                        allow_writes=body.allow_writes,
                        use_tools=body.use_tools,
                        plaky_board_id=context_board_id(),
                        plaky_group_id=context_group_id(),
                    ):
                        yield chunk
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        event_bytes(),
        media_type="text/event-stream; charset=utf-8",
        headers=headers,
    )


@router.get("/agent/jobs/{job_id}")
async def agent_job_status(job_id: str) -> dict[str, Any]:
    q = get_job_queue()
    data = await q.fetch_public_job(job_id)
    if data is None:
        return {"ok": True, "job_id": job_id, "status": "not_found"}
    out: dict[str, Any] = {"ok": True, "job_id": job_id, "status": data["status"]}
    if data["status"] == "complete":
        if "success" in data:
            out["success"] = data["success"]
        if "result" in data:
            out["result"] = data["result"]
    return out


@router.get("/agent/sessions/{session_id}/history", response_model=AgentHistoryResponse)
async def agent_history(session_id: str, session: AsyncSession = Depends(get_db)) -> AgentHistoryResponse:
    hist = await get_session_history(session, session_id)
    return AgentHistoryResponse(session_id=session_id, messages=hist)


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


@router.post("/agent/init-direction", response_model=InitDirectionResponse)
async def api_init_direction(body: InitDirectionRequest) -> dict:
    """
    Initialize DIRECTION.md by opening a PR from a temporary branch.
    Requires a signed-in `gh` user with push access to the target repo.
    """
    parts = body.repo.split("/")
    if len(parts) != 2:
        return {"ok": False, "message": "repo must be owner/name"}
    owner, name = parts[0], parts[1]
    # `init_direction_file` currently chooses the PR base/branch itself.
    return await init_direction_file(owner, name, force=body.force)
