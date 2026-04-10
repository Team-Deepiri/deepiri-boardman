"""
QA tier ↔ repo eligibility (Tier 3 = all repos, Tier 2 = exclude AI/heavy set, Tier 1 = web/core only).

Patterns are fnmatch (case-insensitive) against the GitHub full name `owner/repo`.
Keep defaults in sync with `worker/src/qaTierRules.ts`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import List


@dataclass
class QaRepoRules:
    tier2_excluded_patterns: List[str] = field(default_factory=list)
    tier1_only_patterns: List[str] = field(default_factory=list)


def default_qa_repo_rules() -> QaRepoRules:
    """Product defaults; override via team_assignments.yml `qa_repo_rules`."""
    return QaRepoRules(
        tier2_excluded_patterns=[
            "*diva*",
            "*diri-cyrex*",
            "*diri_cyrex*",
            "*persola*",
            "*cyrex*",
            "*uqe*",
            "*mudspeed*",
            "*agent-testing-utils*",
            "*modelkit*",
            "*agent-toolbox*",
            "*training-orchestrator*",
            "*helox*",
            "*agent-guardrails*",
            "*emotion*desktop*",
            "*emotion-desktop*",
            "*sorge*",
            "*norozo*",
            "*prismpipe*",
            "*boardman*",
        ],
        tier1_only_patterns=[
            "*deepiriweb-frontend*",
            "*deepiriweb_frontend*",
            "*/landing",
            "*-landing",
            "*api-gateway*",
            "*api_gateway*",
            "*apigateway*",
            "*/auth",
            "*-auth",
            "*shared-utils*",
            "*shared_utils*",
            "*deepiri-platform*",
            "*deepiri_platform*",
            "*axiom*",
        ],
    )


def _norm_fn(full: str) -> str:
    return (full or "").strip().lower()


def repo_matches_any_pattern(full_name: str, patterns: List[str]) -> bool:
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


def qa_tier_allows_repo(qa_tier: int, full_name: str, rules: QaRepoRules) -> bool:
    """
    Tier 3: any repo.
    Tier 2: repos that match tier2_excluded_patterns are not allowed.
    Tier 1: only repos matching tier1_only_patterns.
    """
    t = qa_tier if qa_tier in (1, 2, 3) else 3
    fn = _norm_fn(full_name)
    if not fn:
        return False
    if t == 3:
        return True
    if t == 2:
        return not repo_matches_any_pattern(fn, rules.tier2_excluded_patterns)
    return repo_matches_any_pattern(fn, rules.tier1_only_patterns)
