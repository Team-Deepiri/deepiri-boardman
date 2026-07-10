"""Pure signal extraction from a GitHub PR for Plaky automation.

These are deterministic, dependency-free helpers used by the PR webhook handlers:
  - ``infer_task_type_from_pr`` — Plaky Type from the branch convention
    (``feat/...``, ``fix/...``) or the PR's labels.
  - ``comment_requests_pause`` — a PR comment that pauses the work.
  - ``comment_mentions_qa_or_support`` — dev pinged the QA / support team (→ Needs QA again).

No network, no Plaky/GitHub calls — trivial to unit test.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

# Branch middle segment / label token -> canonical Plaky Type (see task_tag_vocab.TASK_TYPE_TAGS).
_TYPE_BY_TOKEN: dict[str, str] = {
    "feat": "Feature",
    "feature": "Feature",
    "fep": "Feature",
    "fix": "Bug",
    "bug": "Bug",
    "bugfix": "Bug",
    "hotfix": "Bug",
    "patch": "Bug",
    "docs": "Documentation",
    "doc": "Documentation",
    "documentation": "Documentation",
    "chore": "Chore",
    "refactor": "Refactoring",
    "refactoring": "Refactoring",
    "perf": "Refactoring",
    "test": "Tests",
    "tests": "Tests",
    "testing": "Tests",
    "research": "Research",
    "spike": "Research",
    "story": "Story",
    "issue": "Issue",
}

# "pause" intent in a comment: pause / paused / pauses / pausing / on hold / on-hold.
# Word-boundaried so "unpause"/"pause-menu" don't match, but the gerund "pausing" does.
_PAUSE_RE = re.compile(r"\b(paus(?:e|es|ed|ing)|on[\s-]?hold)\b", re.IGNORECASE)

# Captures @user and @org/team-slug mentions.
_MENTION_RE = re.compile(r"@([A-Za-z0-9][A-Za-z0-9-]{0,38}(?:/[A-Za-z0-9._-]+)?)")


def _branch_type_token(head_ref: str) -> str:
    """Type token from a branch like ``feat/foo`` or ``feature/123-bar`` or ``refs/heads/fix/x``."""
    ref = (head_ref or "").strip()
    if not ref:
        return ""
    ref = ref.replace("refs/heads/", "")
    # The convention is "<type>/<desc>"; the type is the first segment.
    first = ref.split("/", 1)[0].strip().lower()
    # tolerate "feat-123" style where there is no slash
    first = re.split(r"[^a-z]", first, maxsplit=1)[0] if first else first
    return first


def infer_task_type_from_pr(
    head_ref: str | None = None,
    labels: Sequence[str] | None = None,
) -> str:
    """Canonical Plaky Type for a PR, or "" if no convention matched.

    Branch convention wins (it is the most explicit per the team's workflow); labels are the
    fallback. ``labels`` may be raw GitHub label name strings.
    """
    token = _branch_type_token(head_ref or "")
    if token and token in _TYPE_BY_TOKEN:
        return _TYPE_BY_TOKEN[token]

    for raw in labels or []:
        name = str(raw or "").strip().lower()
        if not name:
            continue
        if name in _TYPE_BY_TOKEN:
            return _TYPE_BY_TOKEN[name]
        # label like "type: bug" / "kind/feature"
        for sep in (":", "/"):
            if sep in name:
                tail = name.split(sep, 1)[1].strip()
                if tail in _TYPE_BY_TOKEN:
                    return _TYPE_BY_TOKEN[tail]
        for tok, canon in _TYPE_BY_TOKEN.items():
            if re.search(rf"\b{re.escape(tok)}\b", name):
                return canon
    return ""


def pr_label_names(labels: Iterable[object] | None) -> list[str]:
    """Extract label name strings from a GitHub ``labels`` array (list of dicts or strings)."""
    out: list[str] = []
    for raw in labels or []:
        if isinstance(raw, dict):
            n = str(raw.get("name") or "").strip()
        else:
            n = str(raw or "").strip()
        if n:
            out.append(n)
    return out


def comment_requests_pause(text: str | None) -> bool:
    """True if a PR comment asks to pause the work (says "pause" / "paused" / "on hold")."""
    return bool(_PAUSE_RE.search(text or ""))


def comment_mentions_qa_or_support(
    text: str | None,
    support_logins: Iterable[str],
    qa_logins: Iterable[str] = (),
) -> bool:
    """True if the comment @-mentions a QA member or the support team.

    Matches @-mentions of any support/QA login. A bare "@support-team" style team handle is
    also matched when ``support`` appears as a mentioned token.
    """
    body = text or ""
    if not body:
        return False
    mentioned = {m.group(1).casefold() for m in _MENTION_RE.finditer(body)}
    if not mentioned:
        return False
    targets = {str(x).strip().casefold() for x in support_logins if str(x).strip()}
    targets |= {str(x).strip().casefold() for x in qa_logins if str(x).strip()}
    if mentioned & targets:
        return True
    # team-style mention: @<org>/support-team or @support / @qa
    for tok in mentioned:
        tail = tok.split("/")[-1]
        if "support" in tail or tail == "qa" or tail.endswith("-qa") or tail.startswith("qa-"):
            return True
    return False
