from boardman.planning.huddle.models import MeetingRequest
from boardman.planning.huddle.planner import MeetingPlanner


class _FailingLlm:
    def generate(self, prompt: str):  # noqa: ANN001
        raise RuntimeError("network down")


def test_planner_fallback_contains_round_table() -> None:
    planner = MeetingPlanner(llm=_FailingLlm())  # type: ignore[arg-type]
    request = MeetingRequest(
        meeting_title="Deepiri Weekly Sync",
        meeting_type="weekly-status-sync",
        team_focus="qa",
        attendees_count=15,
        objectives=["Align", "Unblock"],
        week_label="next-week",
        target_date_iso="2026-05-04",
    )
    plan = planner.plan(request)
    assert "## Group Round Table" in plan.markdown
    assert "What they are working on / planning next" in plan.markdown
    assert plan.provider_used == "deterministic-fallback"


def test_team_filter_supports_hyphenated_team_slug() -> None:
    planner = MeetingPlanner(llm=_FailingLlm())  # type: ignore[arg-type]
    request = MeetingRequest(
        meeting_title="Deepiri AI ML Sync",
        meeting_type="weekly-status-sync",
        team_focus="ai-ml",
        attendees_count=15,
        objectives=["Align", "Unblock"],
        week_label="next-week",
        target_date_iso="2026-05-04",
    )
    plan = planner.plan(request)
    assert "AI/ML: Monday, 9:30 PM EST" in plan.markdown
    assert "QA: Monday, 10:00 PM EST" not in plan.markdown


class _BadScopeLlm:
    def generate(self, prompt: str):  # noqa: ANN001
        class R:
            text = "Team focus: All teams\nQA: Monday, 10:00 PM EST"
            provider = "ollama"
            model = "x"

        return R()


def test_invalid_llm_scope_falls_back() -> None:
    planner = MeetingPlanner(llm=_BadScopeLlm())  # type: ignore[arg-type]
    request = MeetingRequest(
        meeting_title="Deepiri AI ML Sync",
        meeting_type="weekly-status-sync",
        team_focus="ai-ml",
        attendees_count=15,
        objectives=["Align", "Unblock"],
        week_label="next-week",
        target_date_iso="2026-05-04",
    )
    plan = planner.plan(request)
    assert plan.provider_used == "deterministic-fallback"
