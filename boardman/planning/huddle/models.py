from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(slots=True)
class TeamMeeting:
    team_name: str
    day_of_week: str
    time_est: str
    time_cst: str
    time_mst: str
    time_pst: str


@dataclass(slots=True)
class MeetingRequest:
    meeting_title: str
    meeting_type: str
    team_focus: str
    attendees_count: int
    objectives: list[str]
    week_label: str
    target_date_iso: str
    notes: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(slots=True)
class MeetingPlan:
    markdown: str
    provider_used: str
    model_used: str
    generated_at_iso: str
