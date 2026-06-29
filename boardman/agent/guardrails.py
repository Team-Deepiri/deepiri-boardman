"""Tool risk classes — mutating tools only when API/CLI allows writes.

Write vs read tool sets are derived from :func:`boardman.agent.tools.build_all_tools`
so new tools stay classified without editing a duplicate list.

Bulk-organize preview gating uses lightweight, non-LLM heuristics on user text
(scoring + negation guards) for predictable latency.
"""

from __future__ import annotations

import re
import unicodedata

from boardman.agent.tools import build_all_tools


def _write_tool_names() -> frozenset[str]:
    ro = frozenset(t.name for t in build_all_tools(allow_writes=False))
    rw = frozenset(t.name for t in build_all_tools(allow_writes=True))
    return frozenset(rw - ro)


WRITE_TOOLS: frozenset[str] = _write_tool_names()


def is_write_tool(name: str) -> bool:
    return name in WRITE_TOOLS


# Strong signals: bulk / mass task moves (usually need preview before writes).
_STRONG_ORGANIZE_PATTERNS: tuple[str, ...] = (
    r"\bbulk\s+(update|move|archive|close|delete|merge)\b",
    r"\bmove\b.+\btask",
    r"\barchive\s+(the|all|every|these|my|our)\b",
)
# Softer verbs: require another point from context or a second soft verb.
_SOFT_ORGANIZE_PATTERNS: tuple[str, ...] = (
    r"\b(re)?organi[sz]e\b",
    r"\breorder\b",
    r"\bcleanup\b",
    r"\bclean up\b",
)
_CONTEXT_PATTERN = re.compile(
    r"\b("
    r"board|boards|plaky|tasks?|items?|tickets?|columns?|swimlanes?|backlog|"
    r"sections?|groups?|workspace|sprint|epics?|stories|milestones?|"
    r"everything|\ball\b|\bevery\s+task\b|\bevery\s+item\b"
    r")\b",
    re.IGNORECASE,
)

# Direct "do not organize / never bulk …" — avoids false positives like "don't worry, organize …".
_NEGATED_VERB = re.compile(
    r"\b(don'?t|do not|never)\s+((re)?organi[sz]e|reorder|clean\s*up|cleanup)\b",
    re.IGNORECASE,
)
_NEGATED_BULK = re.compile(
    r"\b(don'?t|do not|never)\s+bulk\s+(update|move|archive|close|delete|merge)\b",
    re.IGNORECASE,
)
_NEGATED_AVOID = re.compile(
    r"\b(avoid|skip)\s+((re)?organi[sz]e|reorder|bulk\s+(update|move))\b",
    re.IGNORECASE,
)

_CONFIRM_PATTERN = re.compile(
    r"\b("
    r"confirm(ed)?|"
    r"approve(d)?|"
    r"apply(\s+(now|changes))?|"
    r"go\s+ahead|"
    r"do\s+it|"
    r"yes,?\s*(apply|go|do\s+it|please)"
    r")\b",
    re.IGNORECASE,
)


def _normalize_for_heuristics(message: str) -> str:
    s = unicodedata.normalize("NFKC", (message or "").strip())
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def looks_like_board_organize_request(message: str) -> bool:
    m = _normalize_for_heuristics(message).lower()
    if not m:
        return False
    if _NEGATED_VERB.search(m) or _NEGATED_BULK.search(m) or _NEGATED_AVOID.search(m):
        return False

    score = 0
    if any(re.search(p, m, re.IGNORECASE) for p in _STRONG_ORGANIZE_PATTERNS):
        score += 2
    soft_hits = sum(1 for p in _SOFT_ORGANIZE_PATTERNS if re.search(p, m, re.IGNORECASE))
    if soft_hits:
        score += 1
        if soft_hits >= 2:
            score += 1
    if _CONTEXT_PATTERN.search(m):
        score += 1

    return score >= 2


def has_confirm_token(message: str) -> bool:
    s = _normalize_for_heuristics(message)
    return _CONFIRM_PATTERN.search(s) is not None
