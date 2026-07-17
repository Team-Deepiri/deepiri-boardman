"""Automatic Plaky priority from task content (employer requirement: no manual triage).

Conservative keyword heuristic — boards map these to their own Priority options via the
existing name matching ("High"/"Medium"/"Low" exist on every Deepiri board).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

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
    "critical": "High",
    "urgent": "High",
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
    "good first issue": "Low",
}


def infer_priority_from_text(
    title: str,
    body: str | None = None,
    labels: Sequence[str] | None = None,
) -> str:
    """Return "High" | "Medium" | "Low". Explicit labels beat text keywords; default Medium."""
    for raw in labels or []:
        name = str(raw or "").strip().lower()
        if name in _LABEL_PRIORITY:
            return _LABEL_PRIORITY[name]

    text = f"{title or ''}\n{(body or '')[:2000]}"
    if _HIGH_RE.search(text):
        return "High"
    if _LOW_RE.search(text):
        return "Low"
    return "Medium"
