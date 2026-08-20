"""Canonical status / type / priority labels for Plaky task creation and field matching."""

from __future__ import annotations

TASK_STATUS_TAGS: tuple[str, ...] = (
    "Available",
    "Claimed",
    "STUCK / NEEDS HELP",
    "Paused / Taking a break",
    "In Progress",
    "Revisions in Progress",
    "Needs QA",
    "Needs QA AGAIN",
    "In QA",
    "QA rejected",
    "QA verified",
    "Completed",
    "Deployed",
    "Continuous",
    "Future Works",
    "Multiple Sub-Items",
)

TASK_TYPE_TAGS: tuple[str, ...] = (
    "Issue",
    "Story",
    "Feature",
    # "Task" is a live option on the boards AND a native GitHub issue Type; without it
    # here, a GitHub Type of "Task" canonicalized to the Feature default and landed as
    # Story on boards with no Feature option (live issue #82).
    "Task",
    "Bug",
    "Refactoring",
    "Documentation",
    "Chore",
    "Tests",
    "Research",
)

TASK_PRIORITY_TAGS: tuple[str, ...] = ("High", "Low", "Medium", "Very Important")


def _unique_cf(seq: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        s = (x or "").strip()
        if not s:
            continue
        cf = s.casefold()
        if cf in seen:
            continue
        seen.add(cf)
        out.append(s)
    return tuple(out)


def _canonical_from_vocab(raw: str, vocab: tuple[str, ...], default: str) -> str:
    t = (raw or "").strip()
    if not t:
        return default
    tcf = t.casefold()
    for v in vocab:
        if v.casefold() == tcf:
            return v
    return default


def canonical_task_status(raw: str, *, default: str = "In Progress") -> str:
    return _canonical_from_vocab(raw, TASK_STATUS_TAGS, default)


def canonical_task_type(raw: str, *, default: str = "Feature") -> str:
    return _canonical_from_vocab(raw, TASK_TYPE_TAGS, default)


def canonical_task_priority(raw: str, *, default: str = "Medium") -> str:
    t = (raw or "").strip()
    if not t:
        return default
    c = _canonical_from_vocab(t, TASK_PRIORITY_TAGS, "")
    if c:
        return c
    tcf = t.casefold()
    legacy = {
        "low": "Low",
        "medium": "Medium",
        "med": "Medium",
        "normal": "Medium",
        "p2": "Medium",
        "2": "Medium",
        "high": "High",
        "urgent": "High",
        "major": "High",
        "p1": "High",
        "1": "High",
        "p3": "Low",
        "3": "Low",
        "minor": "Low",
    }
    return legacy.get(tcf, default)


def plaky_create_legacy_priority_param(canonical_priority: str) -> str:
    """Lowercase bucket for PlakyClient legacy /tasks body and internal create paths."""
    m = {
        "Medium": "medium",
        "Low": "low",
        "High": "high",
        "Very Important": "high",
    }
    return m.get(canonical_priority, "medium")


def status_field_patch_candidates(canonical_status: str) -> tuple[str, ...]:
    s = (canonical_status or "").strip()
    if not s:
        return ()
    parts: list[str] = [s]
    if "/" in s:
        for seg in s.split("/"):
            seg = seg.strip()
            if seg:
                parts.append(seg)
        parts.append(s.replace("/", "-").strip())
        parts.append(s.replace("/", " ").strip())
    if s.casefold() == "in progress":
        parts.extend(["in-progress", "doing", "active", "wip"])
    return _unique_cf(tuple(parts))


# Ordered fallbacks per canonical Type. Boards differ (one has "Feature", another still
# has "Task"; few have "Refactoring"), so each type degrades to the nearest label a board
# is likely to actually have instead of silently leaving Type unset.
_TYPE_FALLBACKS: dict[str, tuple[str, ...]] = {
    "feature": ("story", "task", "enhancement"),
    "story": ("feature", "task"),
    "bug": ("issue", "defect", "fix"),
    "issue": ("bug", "defect"),
    "refactoring": ("refactor", "chore", "task", "story"),
    "refactor": ("refactoring", "chore", "task", "story"),
    "research": ("spike", "investigation", "story", "task"),
    # The board vocabulary the team agreed on is Feature/Bug/Refactor/Research/Story,
    # so these degrade to Story rather than reaching "task" first.
    "documentation": ("docs", "story", "chore", "task"),
    "chore": ("refactor", "story", "task"),
    "tests": ("test", "qa", "refactor", "story", "task"),
    "task": ("feature", "story"),
}


def type_field_patch_candidates(canonical_type: str) -> tuple[str, ...]:
    s = (canonical_type or "").strip()
    if not s:
        return ()
    parts: list[str] = [s]
    parts.extend(_TYPE_FALLBACKS.get(s.casefold(), ()))
    return _unique_cf(tuple(parts))


def priority_field_patch_candidates(canonical_priority: str) -> tuple[str, ...]:
    s = (canonical_priority or "").strip()
    if not s:
        return ()
    parts: list[str] = [s, s.casefold()]
    cf = s.casefold()
    if cf == "medium":
        parts.extend(["med", "normal", "p2", "2"])
    elif cf == "low":
        parts.extend(["minor", "p3", "3"])
    elif cf == "high":
        parts.extend(["urgent", "major", "p1", "1"])
    elif cf == "very important":
        parts.extend(["very_important", "veryimportant", "critical", "p0"])
    return _unique_cf(tuple(parts))
