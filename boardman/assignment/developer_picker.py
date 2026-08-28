"""Pick the best-fit developer for a task based on GitHub contribution profiles.

Same scoring engine as the QA picker (cosine similarity over contribution profiles,
language overlap, repo-token overlap) but filtered for developer-eligible members
instead of QA-role members.
"""

from __future__ import annotations

import asyncio
import logging

from boardman.assignment.config import (
    TeamAssignmentsConfig,
    TeamMember,
    load_team_assignments,
)
from boardman.assignment.qa_picker import (
    _NO_FIT_DETAIL,
    FIT_BASE_SCORE,
    FIT_SCORING_TIMEOUT_SECONDS,
    FitDetail,
    _confidence_pct,
    _github_fit_scores,
    _ranked_choice,
    _strength_phrases,
    ensure_github_owner_repo,
)
from boardman.observability.degradation import log_unexpected
from boardman.settings import settings

_log = logging.getLogger(__name__)


def _developer_eligible(m: TeamMember, cfg: TeamAssignmentsConfig) -> bool:
    """A member is developer-eligible unless they only have QA role or are excluded."""
    excluded_norm = {" ".join(e.split()).casefold() for e in (cfg.developer_excluded or [])}
    display = " ".join((m.display or "").split()).casefold()
    login = (m.github_login or "").strip().casefold()
    if display in excluded_norm or (login and login in excluded_norm):
        return False
    roles = {r.lower() for r in m.roles}
    if roles == {"qa"}:
        return False
    return True


async def pick_developer_for_repo(
    full_name: str,
    cfg: TeamAssignmentsConfig | None = None,
) -> tuple[str | None, str, list[dict]]:
    """Returns (display_name, reason, ranked_list).

    ranked_list is [{name, confidence, summary}, ...] for the top candidates.
    """
    cfg = cfg or load_team_assignments()
    fn = ensure_github_owner_repo((full_name or "").strip())
    if not fn:
        return None, "empty repo", []

    if not cfg.members:
        return None, "no team members loaded", []

    candidates = [m for m in cfg.members if _developer_eligible(m, cfg)]
    if not candidates:
        return None, "no developer-eligible members (all are QA-only or excluded)", []

    fits = None
    try:
        fits = await asyncio.wait_for(
            _github_fit_scores(candidates, fn),
            timeout=float(
                getattr(settings, "qa_fit_scoring_timeout_seconds", 0)
                or FIT_SCORING_TIMEOUT_SECONDS
            ),
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("developer_picker: GitHub fit scoring unavailable for %s: %s", fn, e)
        log_unexpected(_log, f"pick_developer_for_repo({fn})", e)

    if fits:
        entries: list[tuple[float, str, float, FitDetail]] = []
        for m in candidates:
            fit, detail = fits.get(m.id, (0.0, _NO_FIT_DETAIL))
            score = (FIT_BASE_SCORE + fit) * max(0.05, m.weight)
            entries.append((score, m.display, fit, detail))
        entries.sort(key=lambda e: -e[0])
        ranked = [
            {
                "name": name,
                "confidence": f"{_confidence_pct(fit)}%",
                "summary": ", ".join(_strength_phrases(detail)),
            }
            for _, name, fit, detail in entries[:5]
        ]

        chosen, reason = _ranked_choice(candidates, cfg, fits, role="developer")
        if chosen:
            _log.info("pick_developer: %s candidates=%d", chosen.display, len(candidates))
            return chosen.display, reason, ranked

    if candidates:
        top = candidates[0]
        return (
            top.display,
            f"We assigned {top.display} as developer based on team eligibility. "
            "Detailed scoring was not available for this pick. Confidence: 20%",
            [
                {"name": m.display, "confidence": "20%", "summary": "no scoring available"}
                for m in candidates[:5]
            ],
        )

    return None, "no eligible developers", []
