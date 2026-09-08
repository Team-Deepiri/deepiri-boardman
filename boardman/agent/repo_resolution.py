"""Deterministic repository resolution before an LLM turn starts."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from boardman.assignment.qa_picker import ensure_github_owner_repo
from boardman.repos_config import list_registered_repos
from boardman.settings import settings


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
    known_repo_keys: Iterable[str] | None = None,
) -> RepoResolution:
    """Resolve explicit input, then session state, then an unambiguous mention.

    ``known_repo_keys`` is the candidate repo set to match against — pass the live
    org repo listing (``list_workspace_repos`` / ``fetch_org_repository_full_names``)
    so matching tracks what actually exists on GitHub instead of only what's been
    manually added to repos.yml. Falls back to the yaml-only registry when the caller
    has no live list on hand (e.g. GITHUB_PAT unset, or a synchronous test call).
    """

    explicit = canonical_repo(explicit_repo)
    if explicit:
        return RepoResolution(explicit, "explicit")

    registered = (
        list(known_repo_keys) if known_repo_keys is not None else list(list_registered_repos())
    )

    # A repo explicitly named in the new message outranks the previous session repo.
    # This prevents a user switching from Boardman to another configured repo from
    # silently receiving an answer grounded in the old session.
    # Any `word/word` token matched, so "boardman/agent/service.py" resolved to the repo
    # `boardman/agent` -- and the answer is persisted to the session, so every later
    # question in the conversation was grounded in a repo that does not exist. A path has
    # more slashes or a file extension on the last segment; a repo mention has neither.
    # The answer is PERSISTED to the session, so a wrong one grounds every later turn in
    # the conversation. Rejecting only filenames was not enough -- `tests/test_smoke`,
    # `boardman/services` and `.github/workflows` all read as repos. An owner/repo pair
    # taken from prose has to be confirmed by something: the org this deployment watches,
    # or a repo somebody configured.
    known = {r.casefold() for r in (canonical_repo(k) or "" for k in registered) if r}
    org = (getattr(settings, "github_org", "") or "").strip().casefold()
    # Match owner/repo mentions; negative lookarounds exclude embedded file paths.
    for token in re.findall(
        r"(?<![\w./-])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?![\w/-])", message or ""
    ):
        if "." in token.rsplit("/", 1)[-1]:
            continue  # a filename, not a repo
        named = canonical_repo(token)
        if not named:
            continue
        if named.casefold() in known or (org and named.partition("/")[0].casefold() == org):
            return RepoResolution(named, "message")

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
        # Only when there is no session repo to fall back on. The pattern behind
        # `mentioned` matches ordinary English -- "what is in progress right now?" gives
        # `progress`, "anything for QA?" gives `QA` -- so returning None here dropped the
        # conversation's repo on any follow-up phrased that way, and the turn ran with no
        # repo context at all. Refusing to GUESS is right; forgetting what we were talking
        # about is not.
        if mentioned and mentioned not in configured_short and not canonical_repo(session_repo):
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
