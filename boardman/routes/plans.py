from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from boardman.planning.huddle.models import MeetingRequest
from boardman.planning.huddle.team_repos import TEAM_CHOICES
from boardman.planning.service import default_plan_output_path, generate_plan, week_anchor
from boardman.settings import settings

router = APIRouter(prefix="/plans", tags=["plans"])


class GeneratePlanRequest(BaseModel):
    meeting_title: str
    meeting_type: str
    team_focus: str = "all-teams"
    week_label: str = "next-week"
    target_date_iso: str | None = None
    attendees_count: int = Field(default=15, ge=2, le=100)
    objectives: list[str] = Field(default_factory=list)
    notes: str | None = None
    write_to_disk: bool = True
    output_path: str | None = None
    provider: str | None = None
    model: str | None = None


class GeneratePlanResponse(BaseModel):
    ok: bool = True
    markdown: str
    provider_used: str
    model_used: str
    generated_at_iso: str
    output_path: str | None = None


def _resolve_target_date(body: GeneratePlanRequest) -> str:
    if body.target_date_iso:
        return body.target_date_iso
    week = "next" if "next" in body.week_label.lower() else "current"
    return week_anchor(week).isoformat()


def _confine_to_output_dir(raw: str) -> Path:
    """Confine a client-supplied output path to the planning output directory.

    Prevents path traversal: the request could otherwise write markdown to an
    arbitrary location. The normalized target must stay within
    ``settings.planning_output_dir``.

    Normalization uses pure string operations (``abspath``/``normpath``) instead
    of ``Path.resolve``/``os.path.realpath`` so the untrusted value never reaches
    a filesystem-touching call, and confinement is enforced with a ``startswith``
    prefix check.
    """
    base = os.path.normpath(os.path.abspath(settings.planning_output_dir))
    candidate = os.path.normpath(os.path.join(base, raw))
    if not candidate.startswith(base + os.sep):
        raise HTTPException(
            status_code=422,
            detail="output_path must stay within the planning output directory",
        )
    return Path(candidate)


def _resolve_output_path(body: GeneratePlanRequest) -> Path | None:
    if not body.write_to_disk:
        return None
    if body.output_path:
        return _confine_to_output_dir(body.output_path)
    week = "next" if "next" in body.week_label.lower() else "current"
    return default_plan_output_path(body.team_focus, body.meeting_type, week)


@router.post("/generate", response_model=GeneratePlanResponse)
def generate_plan_route(body: GeneratePlanRequest) -> GeneratePlanResponse:
    if body.team_focus not in TEAM_CHOICES:
        raise HTTPException(
            status_code=422,
            detail=f"team_focus must be one of: {', '.join(TEAM_CHOICES)}",
        )
    objectives = body.objectives or [
        "Align weekly priorities",
        "Surface wins and blockers",
        "Assign ownership for action items",
    ]
    request = MeetingRequest(
        meeting_title=body.meeting_title,
        meeting_type=body.meeting_type,
        team_focus=body.team_focus,
        attendees_count=body.attendees_count,
        objectives=objectives,
        week_label=body.week_label,
        target_date_iso=_resolve_target_date(body),
        notes=body.notes,
    )
    output_path = _resolve_output_path(body)
    plan = generate_plan(
        request,
        output_path=output_path,
        provider=body.provider,
        model=body.model,
    )
    return GeneratePlanResponse(
        markdown=plan.markdown,
        provider_used=plan.provider_used,
        model_used=plan.model_used,
        generated_at_iso=plan.generated_at_iso,
        output_path=str(output_path) if output_path else None,
    )
