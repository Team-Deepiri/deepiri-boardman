"""
QA tier ↔ repo eligibility (Tier 3 = all repos, Tier 2 = exclude AI/heavy set, Tier 1 = web/core only).

Patterns are fnmatch (case-insensitive) against the GitHub full name `owner/repo`.
Keep defaults in sync with `worker/src/qaTierRules.ts`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase


@dataclass
class QaRepoRules:
    tier2_excluded_patterns: list[str] = field(default_factory=list)
    tier1_only_patterns: list[str] = field(default_factory=list)


def default_qa_repo_rules() -> QaRepoRules:
    """
    Empty defaults — all patterns must be configured via `qa_repo_rules` in team_assignments.yml.

    Example team_assignments.yml:
        qa_repo_rules:
          tier2_excluded_patterns:
            - "*some-heavy-repo*"
          tier1_only_patterns:
            - "*frontend*"
            - "*-landing"
    """
    return QaRepoRules(
        tier2_excluded_patterns=[],
        tier1_only_patterns=[],
    )


def _norm_fn(full: str) -> str:
    return (full or "").strip().lower()


def repo_matches_any_pattern(full_name: str, patterns: list[str]) -> bool:
    fn = _norm_fn(full_name)
    if not fn or not patterns:
        return False
    for p in patterns:
        pat = (p or "").strip().lower()
        if not pat:
            continue
        if fnmatchcase(fn, pat):
            return True
    return False


def qa_tier_allows_repo(qa_tier: float, full_name: str, rules: QaRepoRules) -> bool:
    """
    Tier 3: any repo.
    Tier 2: repos that match tier2_excluded_patterns are not allowed.
    Tier 1: only repos matching tier1_only_patterns.

    qa_tier may be fractional (e.g. 1.7, from weighted-evidence or cold-start bucketing
    -- see boardman/github/repo_capability_mining.py and
    qa_tier_cold_start_default). The pattern rules below are inherently three discrete
    buckets, so a fractional value is FLOORED, not rounded, to decide which bucket
    applies: someone sitting at 2.9 is genuinely not yet proven at tier 3 and must not
    get tier-3 access on the strength of rounding alone.
    """
    # Clamp before flooring, not after: an out-of-range value (defensive only -- every
    # producer of qa_tier already clamps to [1.0, 3.0]) falls back to the SAFEST bucket
    # (1), never the most permissive one, since unknown must never mean "grant more."
    t = int(min(max(qa_tier, 1.0), 3.0))
    fn = _norm_fn(full_name)
    if not fn:
        return False
    if t == 3:
        return True
    if t == 2:
        return not repo_matches_any_pattern(fn, rules.tier2_excluded_patterns)
    return repo_matches_any_pattern(fn, rules.tier1_only_patterns)
