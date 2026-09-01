"""Resolve which org repo a chat message is about, from the message text alone.

No hardcoded repo list: the candidate set is the live `org_repos` listing (already
TTL-cached), and matching is keyword/fuzzy overlap between the message and each repo's
short name. Lets the UI skip the "pick a repo on the left" step for messages that
clearly name a project ("in deepiri-calliope, I made a task...").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import httpx

from boardman.github.org_repos import fetch_org_repository_full_names
from boardman.settings import settings

_WORD_RE = re.compile(r"[a-z0-9]+")

# Confident enough to auto-scope without asking.
_AUTO_MATCH_THRESHOLD = 0.95
# Worth asking the user to confirm/choose from.
_CANDIDATE_THRESHOLD = 0.55
# How far ahead of the runner-up the top score must be to auto-pick instead of asking.
_CLEAR_WINNER_MARGIN = 0.2


def _tokenize(name: str) -> set[str]:
    return set(_WORD_RE.findall(name.lower().replace("-", " ").replace("_", " ")))


@dataclass
class RepoMatchResult:
    matched: str | None = None
    candidates: list[str] = field(default_factory=list)

    @property
    def is_ambiguous(self) -> bool:
        return self.matched is None and len(self.candidates) > 0


def _score_repo(short_name: str, text_low: str, text_tokens: set[str]) -> float | None:
    short_low = short_name.lower()
    if short_low in text_low:
        return 1.0

    short_tokens = _tokenize(short_name)
    if not short_tokens:
        return None
    overlap = short_tokens & text_tokens
    if not overlap:
        return None

    coverage = len(overlap) / len(short_tokens)
    if coverage < 0.5:
        return None
    fuzz = SequenceMatcher(None, short_low, " ".join(sorted(overlap))).ratio()
    return coverage * 0.7 + fuzz * 0.3


async def resolve_repo_from_text(client: httpx.AsyncClient, text: str) -> RepoMatchResult:
    """Match `text` against the live org repo list. Never guesses past the threshold —
    returns candidates instead so the caller can ask the user which repo they meant."""
    if not settings.github_pat or not (text or "").strip():
        return RepoMatchResult()

    try:
        repo_names = await fetch_org_repository_full_names(
            client, settings.github_org, skip_archived=settings.github_skip_archived
        )
    except Exception:
        return RepoMatchResult()
    if not repo_names:
        return RepoMatchResult()

    text_low = text.lower()
    text_tokens = _tokenize(text)

    scored: list[tuple[float, str]] = []
    for full_name in repo_names:
        short = full_name.split("/", 1)[-1]
        score = _score_repo(short, text_low, text_tokens)
        if score is not None:
            scored.append((score, full_name))

    if not scored:
        return RepoMatchResult()

    scored.sort(key=lambda t: -t[0])
    top_score, top_name = scored[0]
    runner_up_score = scored[1][0] if len(scored) > 1 else 0.0

    if top_score >= _AUTO_MATCH_THRESHOLD:
        return RepoMatchResult(matched=top_name)
    if top_score >= _CANDIDATE_THRESHOLD and (top_score - runner_up_score) >= _CLEAR_WINNER_MARGIN:
        return RepoMatchResult(matched=top_name)
    if top_score >= _CANDIDATE_THRESHOLD:
        return RepoMatchResult(candidates=[name for _, name in scored[:5]])
    return RepoMatchResult()
