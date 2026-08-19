"""Deterministic repository resolution before an LLM turn starts."""

from __future__ import annotations

import re
from dataclasses import dataclass

from boardman.assignment.qa_picker import ensure_github_owner_repo
from boardman.repos_config import list_registered_repos


@dataclass(frozen=True)
class RepoResolution:
    repo: str | None
    source: str
    candidates: tuple[str, ...] = ()


def canonical_repo(value: str | None) -> str | None:
    raw = (value or "").strip().strip("`'\"")
    if not raw:
        return None
    if "/" in raw:
        owner, name = (part.strip() for part in raw.split("/", 1))
        return f"{owner}/{name}" if owner and name else None
    return ensure_github_owner_repo(raw)


def _mention_matches(message: str, candidate: str) -> bool:
    text = (message or "").casefold()
    short = candidate.rsplit("/", 1)[-1].casefold()
    if not short:
        return False
    # Repo names contain punctuation; use a boundary around the whole slug so
    # "boardman" does not accidentally match "boardman-old".
    return re.search(rf"(?<![a-z0-9]){re.escape(short)}(?![a-z0-9])", text) is not None


def resolve_repo(
    *,
    explicit_repo: str | None,
    session_repo: str | None,
    message: str,
) -> RepoResolution:
    """Resolve explicit input, then session state, then an unambiguous configured mention."""

    explicit = canonical_repo(explicit_repo)
    if explicit:
        return RepoResolution(explicit, "explicit")

    # A repo explicitly named in the new message outranks the previous session repo.
    # This prevents a user switching from Boardman to another configured repo from
    # silently receiving an answer grounded in the old session.
    direct = re.search(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b", message or "")
    if direct:
        return RepoResolution(canonical_repo(direct.group(1)), "message")

    registered = list_registered_repos()
    candidates: list[str] = []
    for key in registered:
        repo = canonical_repo(key)
        if repo and _mention_matches(message, repo) and repo not in candidates:
            candidates.append(repo)
    # Prefer the longest match if a configured slug contains another slug.
    if len(candidates) == 1:
        return RepoResolution(candidates[0], "message")
    if len(candidates) > 1:
        candidates.sort(key=len, reverse=True)
        if len(candidates[0]) > len(candidates[1]):
            return RepoResolution(candidates[0], "message", tuple(candidates))
        return RepoResolution(None, "ambiguous", tuple(candidates))

    # If the user used an unmistakable repo-introducing phrase for an unknown slug,
    # do not substitute the one configured repo's routing into that request.
    named = re.search(
        r"\b(?:for|from|in|repo|repository|project)\s+([A-Za-z0-9_.-]+)",
        message or "",
        re.IGNORECASE,
    )
    if named:
        mentioned = named.group(1).casefold()
        if mentioned in {
            "this",
            "that",
            "my",
            "our",
            "the",
            "current",
            "these",
            "those",
            "am",
            "are",
            "is",
            "do",
            "does",
            "should",
            "can",
            "will",
        }:
            mentioned = ""
        if mentioned.isdigit():
            mentioned = ""
        configured_short = {
            (canonical_repo(key) or "").rsplit("/", 1)[-1].casefold() for key in registered
        }
        if mentioned and mentioned not in configured_short:
            return RepoResolution(None, "unknown-mentioned")

    session = canonical_repo(session_repo)
    if session:
        return RepoResolution(session, "session")

    # A single configured repo is a safe default for generic Boardman-local requests.
    # Never apply this fallback when the user named an unconfigured repo; that would
    # silently file work in the wrong Plaky group.
    configured = sorted({canonical_repo(key) for key in registered} - {None})
    if len(configured) == 1:
        return RepoResolution(configured[0], "single-configured")
    return RepoResolution(None, "unknown", tuple(configured))
