"""Automatic Plaky priority from task content (employer requirement: no manual triage).

Conservative keyword heuristic — boards map these to their own Priority options via the
existing name matching ("High"/"Medium"/"Low" exist on every Deepiri board).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

# Signals that something is on fire or user-facing broken.
_HIGH_RE = re.compile(
    r"\b(security|vulnerab\w*|exploit|data loss|corrupt\w*|outage|down in prod|prod(uction)? (is )?down"
    r"|crash\w*|urgent|critical|blocker|blocking|broken|cannot (log ?in|start|deploy)|regression)\b",
    re.I,
)
# Routine hygiene that should never outrank real work.
_LOW_RE = re.compile(
    r"\b(typo|docs?|documentation|readme|comment[s]?|rename|cleanup|clean up|chore|formatting"
    r"|lint\w*|style|cosmetic|nit[s]?)\b",
    re.I,
)

_LABEL_PRIORITY = {
    "very important": "Very Important",
    "priority: very important": "Very Important",
    "p0 very important": "Very Important",
    "critical": "High",
    "urgent": "Very Important",
    "p0": "High",
    "p1": "High",
    "high": "High",
    "priority: high": "High",
    "p2": "Medium",
    "medium": "Medium",
    "priority: medium": "Medium",
    "low": "Low",
    "p3": "Low",
    "priority: low": "Low",
}
# A hint, not a statement of priority. `good first issue` says the work is approachable,
# and reading it as an EXPLICIT Low licensed overwriting a priority a lead had set on the
# board by hand. It still nudges the inferred value, which nothing overwrites.
_LABEL_PRIORITY_HINTS = {"good first issue": "Low"}


def priority_from_github_label(value: Any) -> str:
    """Resolve free-form GitHub priority labels to canonical Plaky values."""
    if isinstance(value, dict):
        value = value.get("name") or value.get("title") or value.get("label") or value.get("value")
    raw = str(value or "").strip().casefold()
    if not raw:
        return ""
    token = re.sub(r"[\s_/:#-]+", " ", raw).strip()
    said_priority = bool(re.search(r"(?:^(?:priority|prio)\s)|(?:\s(?:priority|prio)$)", token))
    token = re.sub(r"^(?:priority|prio)\s+", "", token).strip()
    token = re.sub(r"\s+(?:priority|prio)$", "", token).strip()
    direct = _LABEL_PRIORITY.get(token)
    if direct:
        return direct
    # A bare severity word is a priority only when the label IS that word -- which `direct`
    # already covers -- or when the label said "priority" somewhere. Searching for it
    # anywhere read `high-availability` as High, `low-code` as Low and
    # `needs-medium-review` as Medium, none of which are priorities, and each of which then
    # claimed the right to overwrite one somebody had set by hand.
    if not said_priority:
        return ""
    if re.search(r"\burgent\b", token):
        return "Very Important"
    if re.search(r"\bhigh\b", token):
        return "High"
    if re.search(r"\bmedium\b|\bmed\b", token):
        return "Medium"
    if re.search(r"\blow\b", token):
        return "Low"
    return ""


def infer_priority_from_text(
    title: str,
    body: str | None = None,
    labels: Sequence[str] | None = None,
) -> str:
    """Return a canonical Plaky bucket; explicit labels beat text keywords."""
    for raw in labels or []:
        explicit = priority_from_github_label(raw)
        if explicit:
            return explicit

    for raw in labels or []:
        hint = _LABEL_PRIORITY_HINTS.get(
            re.sub(r"[\s_/:#-]+", " ", str(raw or "").strip().casefold())
        )
        if hint:
            return hint

    text = f"{title or ''}\n{(body or '')[:2000]}"
    if _HIGH_RE.search(text):
        return "High"
    if _LOW_RE.search(text):
        return "Low"
    return "Medium"
