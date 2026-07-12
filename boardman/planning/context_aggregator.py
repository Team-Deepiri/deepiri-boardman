from __future__ import annotations

import logging
from typing import Protocol

from boardman.planning.huddle.context_direction import DirectionPlanningContext
from boardman.planning.huddle.context_github import GitHubPlanningContext
from boardman.planning.huddle.context_plaky import PlakyPlanningContext
from boardman.planning.huddle.context_sync import SyncPlanningContext

log = logging.getLogger(__name__)


class _PlanningContext(Protocol):
    def context_markdown(self, team_focus: str) -> str: ...


class ContextAggregator:
    """Merge planning context sources in a stable order for LLM prompts."""

    def __init__(
        self,
        github_context: _PlanningContext | None = None,
        plaky_context: _PlanningContext | None = None,
        sync_context: _PlanningContext | None = None,
        direction_context: _PlanningContext | None = None,
    ) -> None:
        self._sources: list[tuple[str, _PlanningContext]] = [
            ("GitHub", github_context or GitHubPlanningContext()),
            ("Plaky", plaky_context or PlakyPlanningContext()),
            ("Boardman sync", sync_context or SyncPlanningContext()),
            ("Repo direction", direction_context or DirectionPlanningContext()),
        ]

    def context_markdown(self, team_focus: str) -> str:
        parts: list[str] = []
        for label, provider in self._sources:
            block = self._safe_context(label, provider, team_focus)
            if block:
                parts.append(block)
        if not parts:
            return "No organizational context available."
        return "\n\n".join(parts)

    @staticmethod
    def _safe_context(
        label: str,
        provider: _PlanningContext,
        team_focus: str,
    ) -> str:
        try:
            return provider.context_markdown(team_focus).strip()
        except Exception as exc:
            log.warning(
                "planning_context_failed label=%r team_focus=%r error_type=%s error=%s",
                label,
                team_focus,
                type(exc).__name__,
                str(exc)[:400],
                exc_info=log.isEnabledFor(logging.DEBUG),
            )
            return f"## {label}\n- unavailable ({type(exc).__name__})."
