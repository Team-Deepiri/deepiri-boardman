from boardman.planning.huddle.models import MeetingRequest
from boardman.planning.huddle.plan_output import (
    is_too_close_to_fallback,
    parse_h2_sections,
    similarity_to_fallback,
    validate_meeting_plan_markdown,
)
from boardman.planning.huddle.planner import MeetingPlanner


def test_parse_h2_sections_basic() -> None:
    md = """# Title

## Alpha
one

## Beta
two
"""
    assert parse_h2_sections(md) == {"Alpha": "one", "Beta": "two"}


def test_fallback_markdown_passes_schema() -> None:
    planner = MeetingPlanner(llm=None)  # type: ignore[arg-type]
    request = MeetingRequest(
        meeting_title="Sync",
        meeting_type="weekly-status-sync",
        team_focus="qa",
        attendees_count=12,
        objectives=["Ship"],
        week_label="next-week",
        target_date_iso="2026-05-04",
    )
    fb = planner._fallback_markdown(request)
    result = validate_meeting_plan_markdown(fb)
    assert result.ok, result.errors


def test_similarity_identical_is_high() -> None:
    s = "hello world\nfoo"
    assert similarity_to_fallback(s, s) >= 0.99


def test_similarity_divergent_is_lower() -> None:
    a = "We will open with outcomes, then deep dive into model evaluation metrics and dataset drift."
    b = "Opening, outcomes, round table on work wins blockers, then decisions and action owners."
    assert similarity_to_fallback(a, b) < 0.55


def test_echoing_fallback_triggers_rejection() -> None:
    planner = MeetingPlanner(llm=None)  # type: ignore[arg-type]
    request = MeetingRequest(
        meeting_title="Sync",
        meeting_type="weekly-status-sync",
        team_focus="qa",
        attendees_count=12,
        objectives=["Ship"],
        week_label="next-week",
        target_date_iso="2026-05-04",
    )
    fb = planner._fallback_markdown(request).strip()
    assert is_too_close_to_fallback(fb, fb)
    assert is_too_close_to_fallback(fb + "\n", fb)


class _StaticLlm:
    def __init__(self, text: str) -> None:
        self._text = text

    def generate(self, prompt: str):  # noqa: ANN001
        class R:
            pass

        r = R()
        r.text = self._text
        r.provider = "stub"
        r.model = "stub-model"
        return r


def test_planner_rejects_near_copy_of_fallback() -> None:
    planner_base = MeetingPlanner(llm=None)  # type: ignore[arg-type]
    request = MeetingRequest(
        meeting_title="Weekly",
        meeting_type="weekly-status-sync",
        team_focus="qa",
        attendees_count=10,
        objectives=["Align"],
        week_label="next-week",
        target_date_iso="2026-05-11",
    )
    fallback = planner_base._fallback_markdown(request).strip()
    planner = MeetingPlanner(llm=_StaticLlm(fallback))  # type: ignore[arg-type]
    plan = planner.plan(request)
    assert plan.provider_used == "deterministic-fallback"


def test_planner_accepts_distinct_valid_markdown() -> None:
    distinct = """
## Purpose
Anchor this weekly AI/ML sync on model quality, dataset freshness, and inference latency goals
for the coming sprint.

## Agenda Timeline
- 0:00–0:08: outcomes + safety reminders for on-call
- 0:08–0:18: team snapshot on datasets and evaluation harness
- 0:18–0:40: group round table (wins, blockers, planning next)
- 0:40–0:52: decisions on thresholds and rollout gates
- 0:52–1:00: action read-back

## Group Round Table
Each engineer shares what they are working on next, two wins from the week, and the top
blocker that needs a decision or owner today.

## Team Snapshot
Forum label: **AI/ML:** core sync. Streams: training pipeline stability, offline eval coverage,
and cross-team dependency on the feature store team for schema changes.

## Decisions Needed
Pick default rollback strategy for the new ranking model and confirm who signs off on prod.

## Risks and Blockers
Hidden dependency risk if the feature store migration slips; capacity risk on GPU pools;
escalation path if incident volume spikes mid-week.

## Action Items
- [ ] Owner: ML lead — publish eval summary (due: Wed)
- [ ] Owner: SRE — verify cache hit rate dashboard (due: Fri)

## Follow-up Checklist
- [ ] Notes posted to the shared doc
- [ ] Owners acknowledge actions in thread
"""
    planner = MeetingPlanner(llm=_StaticLlm(distinct))  # type: ignore[arg-type]
    request = MeetingRequest(
        meeting_title="AI ML Weekly",
        meeting_type="weekly-status-sync",
        team_focus="ai-ml",
        attendees_count=12,
        objectives=["Improve model quality"],
        week_label="next-week",
        target_date_iso="2026-05-11",
    )
    plan = planner.plan(request)
    assert plan.provider_used == "stub"
    assert "Group Round Table" in plan.markdown
