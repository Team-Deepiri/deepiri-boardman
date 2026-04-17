"""Internal QA pick for workers / automation (Bearer WORKER_INTERNAL_SECRET)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from boardman.assignment.qa_picker import pick_engineer_for_repo, pick_qa_for_repo
from boardman.settings import settings

router = APIRouter()


class PickQaBody(BaseModel):
    repo: str


class PickQaResponse(BaseModel):
    ok: bool
    qa_plaky_id: Optional[str] = None
    engineer_plaky_id: Optional[str] = None
    reason_qa: str = ""
    reason_engineer: str = ""


def _require_internal(authorization: Optional[str]) -> None:
    secret = (settings.worker_internal_secret or "").strip()
    if not secret:
        raise HTTPException(status_code=404, detail="assignment internal API not configured")
    auth = (authorization or "").strip()
    if auth != f"Bearer {secret}":
        raise HTTPException(status_code=401, detail="invalid authorization")


@router.post("/assignment/pick-qa", response_model=PickQaResponse)
async def pick_qa_internal(body: PickQaBody, authorization: Optional[str] = Header(None)) -> PickQaResponse:
    _require_internal(authorization)
    qid, rq = await pick_qa_for_repo(body.repo)
    eid, re = pick_engineer_for_repo(body.repo)
    return PickQaResponse(
        ok=True,
        qa_plaky_id=qid,
        engineer_plaky_id=eid,
        reason_qa=rq,
        reason_engineer=re,
    )
